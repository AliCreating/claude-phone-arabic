"""Controller daemon: owns every call. Talks to Asterisk over ARI and serves call_<owner> on localhost."""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from pathlib import Path

from aiohttp import web

from . import config, hold, housekeeping, inject, keychain, log, push, registry, stt, tts
from .ari import ARI, Call, Hangup

lg = log.get("daemon")

MAILBOX = "owner@claude"
VOICEMAIL_MESSAGE = ("{owner} didn't answer. A voicemail has been left. Carry on with other work or your best "
                     "assumption; the answer will arrive later as a new message in this session.")
ANSWER_PREFIX = "[Phone answer to your earlier question]"
ALLHANDS_PREFIX = "[All-hands message from {owner}, sent to every session]"
# Claude Code's spinner line while a turn runs, e.g. "· Generating… (1m 51s · ↓ 4.3k tokens)".
SPINNER = re.compile(r"…\s*\(\d+[hms]|esc to interrupt")
HOLD_MAX = 600  # seconds on hold before the message is taken anyway
# Hangup causes that mean "didn't pick up": no answer, busy, DND (480/486/603), unreachable.
NOT_ANSWERED = "not answered"
# Missed-call reasons where a push would only repeat what the owner knows.
PUSH_SKIP = {"hung up before answering", "answered but nothing was said"}


def recording_path(name: str) -> Path:
    return config.ASTERISK_PREFIX / "var/spool/asterisk/recording" / f"{name}.wav16"


class Controller:
    def __init__(self):
        self.s = config.load()
        self.ari = ARI(self.s.ari_url, "phone", keychain.get_or_create("ari"))
        self.line = asyncio.Lock()  # one call with the owner at a time
        self.calls: dict[str, asyncio.Task] = {}
        self.endpoint_state: dict[str, str] = {}

    # --- lifecycle ---

    async def start(self) -> None:
        await self.ari.start()
        self.ari.on_event(self.on_event)
        self.ari.on_connect.append(self.subscribe_phones)
        asyncio.create_task(self.ari.run_events())
        # In the background: the first start (or a voice change) renders every fixed phrase, which takes a while.
        asyncio.create_task(asyncio.to_thread(tts.prerender))
        asyncio.create_task(self.sweeper())
        await asyncio.wait_for(self.ari.connected.wait(), 30)
        await self.update_mwi()

    async def sweeper(self) -> None:
        missing = sweeps = 0
        while True:
            # Only after two sweeps in a row: on wake from sleep the sweep can beat Wi-Fi reconnecting.
            warning = config.ip_warning()
            missing = missing + 1 if warning else 0
            if missing == 2:
                lg.warning(warning)
            try:
                live = await asyncio.to_thread(inject.live_iterm_ids)
                for s in registry.remove_missing(live):
                    lg.info("removed stale session ext=%s %s", s.ext, s.label)
            except Exception as e:
                lg.warning("stale sweep failed: %r", e)
            if sweeps % 60 == 0:
                try:
                    done = await asyncio.to_thread(housekeeping.run)
                    if any(done.values()):
                        lg.info("housekeeping: %s", done)
                except Exception as e:
                    lg.warning("housekeeping failed: %r", e)
            sweeps += 1
            await asyncio.sleep(60)

    async def subscribe_phones(self) -> None:
        """Watch the phones' registration. Asterisk only sends MWI when the count changes, so a phone
        that was offline when a voicemail arrived never lights up unless we resend on its return."""
        try:
            await self.ari.post("/applications/claude-phone/subscription", eventSource=f"endpoint:PJSIP/{self.s.phone_ext}")
        except Exception as e:
            lg.warning("endpoint subscription for %s failed: %r", self.s.phone_ext, e)

    async def update_mwi(self) -> None:
        n = len(registry.pending_voicemails())
        try:
            await self.ari.put(f"/mailboxes/{MAILBOX}", oldMessages=0, newMessages=n)
        except Exception as e:
            lg.warning("MWI update failed: %r", e)

    async def phone_online(self) -> bool:
        ep = await self.ari.get(f"/endpoints/PJSIP/{self.s.phone_ext}", ok404=True)
        return bool(ep) and ep.get("state") == "online"

    # --- events ---

    def on_event(self, ev: dict) -> None:
        if ev.get("type") == "EndpointStateChange":
            # Also fires when a call starts or ends; only a return from offline needs the light resent.
            ep = ev.get("endpoint", {})
            res, state = ep.get("resource"), ep.get("state")
            was, self.endpoint_state[res] = self.endpoint_state.get(res), state
            if state == "online" and was != "online":
                lg.info("phone %s online; resending message light", res)
                asyncio.create_task(self.update_mwi())
            return
        if ev.get("type") != "StasisStart":
            return
        args = ev.get("args") or []
        if args and args[0] == "outbound":
            return  # handled by the call_owner flow waiting on it
        ch = ev["channel"]
        exten = args[0] if args else ch.get("dialplan", {}).get("exten", "")
        self.calls[ch["id"]] = asyncio.create_task(self.inbound(ch, exten))

    async def inbound(self, ch: dict, exten: str) -> None:
        call = Call(self.ari, ch["id"])
        who = ch.get("caller", {}).get("number")
        lg.info("inbound call from %s to %s", who, exten)
        try:
            if self.line.locked():
                await call.answer()
                await call.play(tts.phrase("busy"))
                return
            async with self.line:
                await call.answer()
                if exten == self.s.voicemail_ext:
                    await self.voicemail(call)
                elif exten == self.s.operator_ext:
                    await self.operator(call)
                elif exten == self.s.allhands_ext:
                    await self.all_hands(call)
                elif exten.isdigit() and self.s.first_session_ext <= int(exten) <= self.s.last_session_ext:
                    await self.dial_session(call, int(exten))
                else:
                    await call.play(tts.phrase("no_session"))
        except Hangup:
            pass
        except Exception as e:
            lg.exception("inbound call failed: %r", e)
        finally:
            try:
                await call.hangup()
            except Exception:
                pass
            call.close()
            self.calls.pop(ch["id"], None)

    # --- me ringing a session ---

    async def operator(self, call: Call) -> None:
        """Dial 0: say who you want ("the backend on the AGS branch") and the operator puts you through."""
        from . import operator
        sessions = registry.all_sessions()
        await call.play(tts.op(tts.OPERATOR["greeting"]), tts.beep())
        candidates, misses = sessions, 0
        while misses < 2:
            name = f"op-{int(time.time())}-{misses}"
            # Stops after a 2 s pause: nobody hangs up on the operator.
            await call.record(name, 10, max_silence=2)
            heard = await self.transcribe(name) or ""
            hit, candidates = operator.match(heard, candidates) if candidates else (None, [])
            lg.info("operator heard %r -> %s (candidates %s)", heard, hit and hit.ext, [c.ext for c in candidates])
            if hit:
                said = await tts.op_async(tts.OPERATOR["connecting"].format(label=tts.spoken_label(hit.label)))
                await call.play(said)
                await self.dial_session(call, hit.ext)
                return
            misses += 1
            if misses == 2:
                break
            if not candidates:
                candidates = sessions
                await call.play(tts.op(tts.OPERATOR["again"]), tts.beep())
                continue
            options = tts.OPERATOR["or"].join(tts.OPERATOR["option"].format(label=tts.spoken_label(c.label), ext=c.ext)
                                              for c in candidates[:3])
            await call.play(await tts.op_async(tts.OPERATOR["which"].format(options=options)), tts.beep())
        await call.play(tts.op(tts.OPERATOR["unplaced" if sessions else "nobody"]))

    async def dial_session(self, call: Call, ext: int, prefix: str = "", source: str = "") -> str | None:
        """Greet, record, transcribe, type into the session. Returns what was sent (None if nothing)."""
        source = source or f"dial-in {ext}"
        sess = registry.by_ext(ext)
        if not sess:
            lg.info("dialled %s: no session", ext)
            await call.play(tts.phrase("no_session"))
            return
        await self.hold(call, sess)
        greeting = await tts.render_async(tts.PHRASES["connected"].format(label=tts.spoken_label(sess.label)), sess.repo)
        await call.play(greeting, tts.beep())
        name = f"in-{ext}-{int(time.time())}"
        ended, d = await call.record(name, self.s.record_seconds)
        lg.info("dial-in %s recording ended: %s %s", ext, ended, d or "")
        text = await self.transcribe(name)
        if text is None:
            if call.up:
                await call.play(tts.phrase("transcribe_failed"))
            return
        if not text:
            lg.info("dial-in %s: nothing said, nothing sent", ext)
            if call.up:
                await call.play(tts.phrase("nothing_heard"))
            return
        sess = registry.by_ext(ext) or sess  # may have been re-registered during the call
        result = await asyncio.to_thread(inject.type_into, sess, f"{prefix} {text}" if prefix else text, source)
        if call.up:
            await call.play(tts.phrase("sent" if result == "sent" else "inject_failed"))
        return text if result == "sent" else None

    async def mid_task(self, ext: int) -> bool:
        """The hooks say it's working and Claude Code's spinner is on screen. The hooks alone can stick on
        'working': an Esc interrupt fires no Stop."""
        sess = registry.by_ext(ext)
        if not sess or sess.status != "working" or not sess.iterm_id:
            return False
        try:
            screen = await asyncio.to_thread(inject.read_screen, sess.iterm_id)
        except Exception:
            return False
        tail = "\n".join([l for l in screen.splitlines() if l.strip()][-15:])
        return bool(SPINNER.search(tail))

    async def hold(self, call: Call, sess: registry.Session) -> None:
        """If the session is mid-task, play hold music until its turn ends. Hash skips the wait; the
        message then queues in Claude Code behind the running turn."""
        if not await self.mid_task(sess.ext):
            return
        lg.info("dial-in %s: session mid-task, holding", sess.ext)
        free = asyncio.get_running_loop().create_future()

        async def watch() -> None:
            while True:
                await asyncio.sleep(1)
                if not await self.mid_task(sess.ext):
                    free.set_result(True)
                    return

        watcher = asyncio.create_task(watch())
        started = time.monotonic()
        try:
            intro = await tts.op_async(tts.OPERATOR["hold"].format(label=tts.spoken_label(sess.label)))
            got = await call.play(intro, interrupt="#", until=free)
            music, important = await asyncio.to_thread(hold.render), tts.op(tts.OPERATOR["important"])
            while got is None and time.monotonic() - started < HOLD_MAX:
                got = await call.play(music, important, interrupt="#", until=free)
        finally:
            watcher.cancel()
        lg.info("dial-in %s: off hold after %ds (%s)", sess.ext, time.monotonic() - started,
                {"until": "turn ended", "#": "skipped"}.get(got, "timed out"))

    async def all_hands(self, call: Call) -> None:
        """Dial 9: one message typed into every live session."""
        sessions = registry.all_sessions()
        n = len(sessions)
        if not n:
            await call.play(tts.phrase("allhands_none"))
            return
        intro = await tts.render_async(tts.PHRASES["allhands_intro"].format(n=n, sessions="session" if n == 1 else "sessions"))
        await call.play(intro, tts.beep())
        name = f"all-{int(time.time())}"
        ended, d = await call.record(name, self.s.record_seconds)
        lg.info("all-hands recording ended: %s %s", ended, d or "")
        text = await self.transcribe(name)
        if not text:
            lg.info("all-hands: nothing said, nothing sent")
            if call.up:
                await call.play(tts.phrase("nothing_heard" if text == "" else "transcribe_failed"))
            return
        sent = 0
        for sess in registry.all_sessions():  # re-read: sessions may have come or gone during the call
            result = await asyncio.to_thread(inject.type_into, sess, f"{ALLHANDS_PREFIX.format(owner=self.s.owner)} {text}", "all-hands")
            sent += result == "sent"
        lg.info("all-hands sent to %s sessions", sent)
        if call.up:
            await call.play(await tts.render_async(
                tts.PHRASES["allhands_sent"].format(n=sent, sessions="session" if sent == 1 else "sessions")))

    async def transcribe(self, name: str, extra: str = "") -> str | None:
        """Transcript ('' if silent), or None on failure."""
        path = recording_path(name)
        for _ in range(20):  # the file is finalised just after hangup
            if path.exists():
                break
            await asyncio.sleep(0.1)
        if not path.exists():
            lg.warning("recording %s missing", path)
            return ""
        try:
            text = await stt.transcribe(path, extra)
            lg.info("transcript %s: %r", name, text)
            return text
        except Exception as e:
            lg.exception("transcription failed for %s: %r", name, e)
            return None

    # --- Claude ringing me ---

    def caller(self, body: dict) -> dict:
        """Who is calling: the registered session, registering it if its tab predates the hooks."""
        agent = (body.get("from_agent") or "").strip() or None
        sess = registry.find(body.get("session_id"), body.get("iterm_id"))
        if not sess and body.get("session_id") and body.get("iterm_id"):
            from .hooks import repo_info
            cwd = body.get("cwd") or os.getcwd()
            try:
                sess = registry.register(body["session_id"], *repo_info(cwd), cwd, body["iterm_id"])
                lg.info("registered unknown caller ext=%s %s", sess.ext, sess.label)
            except Exception as e:
                lg.warning("could not register caller: %r", e)
        if sess:
            label, ext, sid, repo, branch = sess.label, sess.ext, sess.session_id, sess.repo, sess.worktree or sess.branch
        else:
            from .hooks import repo_info
            repo, b, w = repo_info(body.get("cwd") or os.getcwd())
            branch = w or b
            label, ext, sid = (f"{repo}/{branch}" if branch else repo), None, body.get("session_id") or ""
        # Spoken intro: just the repo, plus the branch only when it isn't the default one.
        intro = tts.spoken_label(f"{repo}/{branch}" if branch and branch not in ("main", "master") else repo)
        if agent:
            label, intro = f"{label}@{agent}", f"{intro}, {agent}"
        if not self.s.say_caller or body.get("_no_intro"):
            intro = ""
        return {"label": label, "ext": ext, "sid": sid, "repo": repo, "intro": intro}

    @staticmethod
    def opening(c: dict) -> str:
        return f"{c['intro']}. " if c["intro"] else ""

    async def ring(self, c: dict, prefix: str) -> tuple[Call | None, str]:
        """Ring the phone. Returns (answered call, '') or (None, why not). Caller holds self.line."""
        chan_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        answered = self.ari.wait_for(lambda e: e.get("type") == "StasisStart" and e["channel"]["id"] == chan_id)
        call = Call(self.ari, chan_id)
        name = self.s.caller_name or c["label"]
        await self.ari.post("/channels", channelId=chan_id, endpoint=f"PJSIP/{self.s.phone_ext}",
                            app="claude-phone", appArgs="outbound",
                            callerId=f'"{name}" <{c["ext"] or ""}>', timeout=self.s.ring_seconds)
        done, _ = await asyncio.wait({answered, call.gone}, timeout=self.s.ring_seconds + 5,
                                     return_when=asyncio.FIRST_COMPLETED)
        if answered in done:
            lg.info("call answered for %s", c["label"])
            await asyncio.sleep(self.s.answer_delay)  # time to lift the phone to your ear
            return call, ""
        self.ari.cancel_wait(answered)
        cause = call.gone.result().get("cause_txt") if call.gone in done else "timeout"
        await call.hangup()
        call.close()
        lg.info("call to %s not answered: %s", self.s.phone_ext, cause)
        return None, f"{NOT_ANSWERED} ({cause})"

    @staticmethod
    def sentence(text: str | None) -> str:
        text = (text or "").strip()
        return text if not text or text[-1] in ".?!" else text + "."

    async def call_owner(self, body: dict) -> dict:
        question = self.sentence(body.get("question"))
        context = self.sentence(body.get("context")) or None
        c = self.caller(body)
        label, ext, sid = c["label"], c["ext"], c["sid"]
        body["_label"] = label
        script = self.opening(c) + (f"{context} " if context else "") + question
        lg.info("call_owner from %s ext=%s: %r", label, ext, question)

        # Speech renders while the phone rings.
        speech = asyncio.create_task(tts.render_async(script, c["repo"]))

        async def to_voicemail(reason: str) -> dict:
            audio = await speech
            registry.add_voicemail(sid, ext, label, f"{context} {question}" if context else question, context, audio)
            await self.update_mwi()
            lg.info("voicemail left by %s (%s)", label, reason)
            if reason not in PUSH_SKIP:
                push.missed("Missed call", label, question, reason)
            return {"status": "voicemail", "reason": reason, "message": VOICEMAIL_MESSAGE.format(owner=self.s.owner)}

        if not question:
            speech.cancel()
            return {"status": "error", "message": "question is required"}
        if body.get("_force_voicemail"):
            return await to_voicemail(body["_force_voicemail"])
        if self.line.locked():
            return await to_voicemail("line busy")
        if not await self.phone_online():
            return await to_voicemail("phone offline")

        async with self.line:
            call, why = await self.ring(c, "cj")
            if not call:
                return await to_voicemail(why)
            name = f"cj-{call.id}"
            try:
                await call.play(await speech)
                await asyncio.sleep(self.s.end_pause)
                await call.play(tts.beep())
                ended, digit = await call.record(name, self.s.record_seconds)
                lg.info("call_owner recording ended: %s %s", ended, digit or "")
                if call.up and ended == "digit":
                    await call.play(tts.phrase("vm_saved"))
                await call.hangup()
            except Hangup:
                lg.info("%s hung up during the question", self.s.owner)
                return await to_voicemail("hung up before answering")
            finally:
                call.close()
            text = await self.transcribe(name)
            if not text:
                return await to_voicemail("answered but nothing was said" if text == "" else "transcription failed")
            return {"status": "answered", "answer": text}

    async def notify_owner(self, body: dict) -> dict:
        """An announcement: ring, say it, hang up. No reply, and no voicemail if it doesn't land."""
        message = self.sentence(body.get("message"))
        if not message:
            return {"status": "error", "message": "message is required"}
        c = self.caller(body)
        body["_label"] = c["label"]
        lg.info("notify_owner from %s ext=%s: %r", c["label"], c["ext"], message)
        speech = asyncio.create_task(tts.render_async(self.opening(c) + message, c["repo"]))

        def missed(reason: str) -> dict:
            speech.cancel()
            lg.info("notification from %s not delivered (%s)", c["label"], reason)
            push.missed("Announcement", c["label"], message, reason)
            return {"status": "not_delivered", "reason": reason}

        if self.line.locked():
            return missed("line busy")
        if not await self.phone_online():
            return missed("phone offline")
        async with self.line:
            call, why = await self.ring(c, "nj")
            if not call:
                return missed(why)
            try:
                await call.play(await speech, tts.phrase("goodbye"))
                await asyncio.sleep(self.s.end_pause)
                await call.hangup()
            except Hangup:
                lg.info("%s hung up during the notification", self.s.owner)
                return {"status": "delivered", "note": f"{self.s.owner} hung up before the end"}
            finally:
                call.close()
            return {"status": "delivered"}

    async def transfer(self, body: dict) -> dict:
        """A session hands the owner to another session: ring, explain, then connect them to the target,
        whose tab receives their words with the handover note."""
        from . import operator
        c = self.caller(body)
        others = [x for x in registry.all_sessions() if x.session_id != c["sid"]]
        target, candidates = operator.match(body.get("to") or "", others)
        if not target:
            listing = [f"{x.ext} {x.label}" for x in (candidates or others)]
            return {"status": "no_match", "message": "No single live session matches that. Pass one of these as `to`.",
                    "sessions": listing}
        reason = self.sentence(body.get("reason"))
        handover = " ".join((body.get("handover") or "").split())
        script = (self.opening(c) + (f"{reason} " if reason else "")
                  + f"Putting you through to {tts.spoken_label(target.label)}.")
        lg.info("transfer from %s to ext=%s %s: %r", c["label"], target.ext, target.label, reason)
        speech = asyncio.create_task(tts.render_async(script, c["repo"]))

        def missed(why: str) -> dict:
            speech.cancel()
            lg.info("transfer from %s not connected (%s)", c["label"], why)
            return {"status": "not_connected", "reason": why}

        if self.line.locked():
            return missed("line busy")
        if not await self.phone_online():
            return missed("phone offline")
        async with self.line:
            call, why = await self.ring(c, "tx")
            if not call:
                return missed(why)
            try:
                await call.play(await speech)
                prefix = f"[Phone transfer from {c['label']}" + (f": {handover}" if handover else "") + f"] {self.s.owner} says:"
                said = await self.dial_session(call, target.ext, prefix=prefix, source=f"transfer {c['ext']}->{target.ext}")
            except Hangup:
                return missed(f"{self.s.owner} hung up before the transfer completed")
            finally:
                await call.hangup()
                call.close()
        if not said:
            return {"status": "connected_no_message", "to": target.label,
                    "message": f"{self.s.owner} was put through to {target.label} but didn't say anything."}
        return {"status": "transferred", "to": target.label, "ext": target.ext, "owner_said": said}

    # --- voicemail ---

    async def voicemail(self, call: Call) -> None:
        items = registry.pending_voicemails()
        if not items:
            await call.play(tts.phrase("vm_none"))
            return
        n = len(items)
        intro = tts.render(tts.PHRASES["vm_intro"].format(n=n, questions="question" if n == 1 else "questions"))
        await call.play(intro, tts.phrase("vm_keys"))
        for vm in items:
            if not call.up:
                break
            frm = await tts.render_async(tts.PHRASES["vm_from"].format(label=tts.spoken_label(vm.label.split("@")[0])))
            media = [frm] + ([vm.audio] if vm.audio else [await tts.render_async(vm.question)]) + [tts.beep()]
            call.drain_digits()
            digit = await call.play(*media, interrupt="#7")
            if digit == "#":
                await call.play(tts.phrase("vm_skipped"))
                continue
            if digit == "7":
                registry.delete_voicemail(vm.id)
                lg.info("voicemail %s deleted (%s)", vm.id, vm.label)
                await self.update_mwi()
                await call.play(tts.phrase("vm_deleted"))
                continue
            name = f"vm-{vm.id}-{int(time.time())}"
            try:
                ended, d = await call.record(name, self.s.record_seconds, terminate_on="any")
            except Hangup:
                ended, d = "hangup", None
            lg.info("voicemail %s recording ended: %s %s", vm.id, ended, d or "")
            if d == "7":
                registry.delete_voicemail(vm.id)
                lg.info("voicemail %s deleted (%s)", vm.id, vm.label)
                await self.update_mwi()
                await call.play(tts.phrase("vm_deleted"))
                continue
            text = await self.transcribe(name)
            if not text:
                if call.up:
                    await call.play(tts.phrase("vm_skipped"))
                continue
            delivered = await self.deliver_answer(vm, text)
            await self.update_mwi()
            if call.up:
                await call.play(tts.phrase("vm_saved" if delivered else "vm_session_gone"))
        if call.up and not registry.pending_voicemails():
            await call.play(tts.phrase("vm_done"))

    async def deliver_answer(self, vm: registry.Voicemail, answer: str) -> bool:
        sess = registry.find(vm.session_id)
        if not sess:
            registry.answer_voicemail(vm.id, answer, "session-ended")
            lg.info("voicemail %s answered but session ended; kept: %r", vm.id, answer)
            return False
        q = vm.question if len(vm.question) <= 160 else vm.question[:157] + "..."
        text = f'{ANSWER_PREFIX} {answer} (Your question was: "{q}")'
        result = await asyncio.to_thread(inject.type_into, sess, text, f"voicemail {vm.id}")
        registry.answer_voicemail(vm.id, answer, "sent" if result == "sent" else f"failed:{result}")
        return result == "sent"

    # --- HTTP (localhost) ---

    async def http_transfer(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            return web.json_response(await self.transfer(body))
        except Exception as e:
            lg.exception("transfer failed: %r", e)
            return web.json_response({"status": "not_connected", "reason": f"error: {e.__class__.__name__}"})

    async def http_notify(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            return web.json_response(await self.notify_owner(body))
        except Exception as e:
            lg.exception("notify_owner failed: %r", e)
            return web.json_response({"status": "not_delivered", "reason": f"error: {e.__class__.__name__}"})

    async def http_call(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            return web.json_response(await self.call_owner(body))
        except Exception as e:
            # Never lose the question: whatever broke, it becomes a voicemail.
            lg.exception("call_owner failed: %r", e)
            try:
                return web.json_response(await self.call_owner({**body, "_force_voicemail": f"error: {e.__class__.__name__}"}))
            except Exception:
                return web.json_response({"status": "error", "message": "The phone system failed. Carry on with your best assumption."})

    async def http_page(self, request: web.Request) -> web.Response:
        """A test page: one button that rings the iPhone (http://127.0.0.1:<daemon_port>/)."""
        return web.FileResponse(Path(__file__).parent / "web" / "call.html")

    async def http_test_call(self, request: web.Request) -> web.Response:
        body = await request.json()
        question = (body.get("question") or "").strip() or "هلا والله، خلصت الشغل طال عمرك، تامر على شي ثاني؟"
        return web.json_response(await self.call_owner({"question": question, "cwd": str(config.REPO), "_no_intro": True}))

    async def http_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ip_warning": config.ip_warning(), "ari": self.ari.connected.is_set(),
                                  "phone_online": await self.phone_online(), "line_busy": self.line.locked()})


@web.middleware
async def json_only(request: web.Request, handler):
    """Every POST must be JSON. Browsers can't send that cross-site without a CORS preflight, which this server
    never answers, so other websites can't make the phone ring."""
    if request.method == "POST" and request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    return await handler(request)


async def main() -> None:
    config.ensure_dirs()
    c = Controller()
    await c.start()
    app = web.Application(middlewares=[json_only])
    app.router.add_post("/call", c.http_call)
    app.router.add_post("/notify", c.http_notify)
    app.router.add_post("/transfer", c.http_transfer)
    app.router.add_get("/health", c.http_health)
    app.router.add_get("/", c.http_page)
    app.router.add_post("/test-call", c.http_test_call)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", c.s.daemon_port).start()
    lg.info("daemon up on 127.0.0.1:%s, ringing PJSIP/%s", c.s.daemon_port, c.s.phone_ext)
    await asyncio.Event().wait()


def run() -> None:
    asyncio.run(main())
