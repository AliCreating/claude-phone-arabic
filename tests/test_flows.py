"""Call flows against a scripted fake call: no Asterisk, audio, whisper or iTerm."""

import asyncio

import pytest

from claude_phone import daemon, inject, push, registry, tts


class FakeCall:
    def __init__(self, digits=(), recordings=()):
        self.id = "c1"
        self.up = True
        self.played = []
        self.digits = list(digits)  # what each interruptible play returns
        self.recordings = list(recordings)

    async def play(self, *media, interrupt="", until=None):
        self.played.extend(media)
        return self.digits.pop(0) if interrupt and self.digits else None

    async def record(self, name, max_seconds, terminate_on="#", max_silence=None):
        return self.recordings.pop(0) if self.recordings else ("finished", None)

    def drain_digits(self):
        pass

    async def hangup(self):
        self.up = False

    def close(self):
        pass


@pytest.fixture
def ctl(monkeypatch):
    """A Controller with speech, transcription, typing and the message light stubbed out."""
    c = daemon.Controller.__new__(daemon.Controller)
    c.s = daemon.config.load()
    c.line = asyncio.Lock()
    c.typed, c.heard, c.pushed = [], [], []

    async def render_async(text, repo=None, voice=None):
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None: f"tts:{text}")
    monkeypatch.setattr(tts, "render_async", render_async)
    monkeypatch.setattr(tts, "beep", lambda: "beep")

    async def transcribe(name):
        return c.heard.pop(0) if c.heard else ""
    monkeypatch.setattr(c, "transcribe", transcribe)

    async def nothing(*a, **k):
        return False
    monkeypatch.setattr(c, "update_mwi", nothing)
    monkeypatch.setattr(c, "mid_task", nothing)

    def type_into(sess, text, source):
        c.typed.append((sess.ext, text))
        return "sent"
    monkeypatch.setattr(inject, "type_into", type_into)
    monkeypatch.setattr(push, "missed", lambda *a: c.pushed.append(a))
    return c


def run(coro):
    return asyncio.run(coro)


def test_dial_in_types_what_was_said(ctl):
    registry.register("s", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["What colour should the button be?"]
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) == "What colour should the button be?"
    assert ctl.typed == [(301, "What colour should the button be?")]
    assert "tts:" + tts.PHRASES["sent"] in call.played


def test_dial_in_silence_sends_nothing(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) is None
    assert ctl.typed == []
    assert "tts:" + tts.PHRASES["nothing_heard"] in call.played


def test_dial_in_empty_extension(ctl):
    call = FakeCall()
    run(ctl.dial_session(call, 399))
    assert call.played == ["tts:" + tts.PHRASES["no_session"]]


def test_transfer_prefix_reaches_target(ctl):
    registry.register("t", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["Testing."]
    run(ctl.dial_session(FakeCall(), 301, prefix="[Phone transfer from app-fe-v2] Sam says:"))
    assert ctl.typed == [(301, "[Phone transfer from app-fe-v2] Sam says: Testing.")]


def test_voicemail_answer_skip_delete(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("s", 301, "repo/main", "Merge it?", None, None)
    b = registry.add_voicemail("s", 301, "repo/main", "Which colour?", None, None)
    registry.add_voicemail("s", 301, "repo/main", "Delete me", None, None)
    ctl.heard = ["Yes, merge it."]
    call = FakeCall(digits=[None, "#", "7"])  # answer the first, skip the second, delete the third
    run(ctl.voicemail(call))
    assert ctl.typed == [(301, '[Phone answer to your earlier question] Yes, merge it. (Your question was: "Merge it?")')]
    assert [v.id for v in registry.pending_voicemails()] == [b]


def test_answer_for_closed_session_is_kept(ctl):
    vm = registry.add_voicemail("gone", None, "repo/main", "Ship?", None, None)
    ctl.heard = ["Yes."]
    call = FakeCall()
    run(ctl.voicemail(call))
    assert ctl.typed == [] and registry.pending_voicemails() == []
    assert "tts:" + tts.PHRASES["vm_session_gone"] in call.played


def test_unanswered_call_leaves_voicemail_and_pushes(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")

    async def online():
        return True

    async def ring(c, prefix):
        return None, "not answered"
    monkeypatch.setattr(ctl, "phone_online", online)
    monkeypatch.setattr(ctl, "ring", ring)
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "iterm_id": "tab"}))
    assert res["status"] == "voicemail"
    assert [v.question for v in registry.pending_voicemails()] == ["Merge it."]
    assert ctl.pushed == [("Missed call", "repo/main", "Merge it.", "not answered")]


def test_hanging_up_on_a_question_does_not_push(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "hung up before answering"}))
    assert res["status"] == "voicemail" and ctl.pushed == []


def test_voicemail_message_names_the_owner(ctl):
    ctl.s.owner_name = "Sam"
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "line busy"}))
    assert res["message"].startswith("Sam didn't answer.")


def test_test_page_call_uses_default_question(ctl, monkeypatch):
    asked = []

    async def call_owner(body):
        asked.append(body["question"])
        return {"status": "answered", "answer": "No, thanks."}
    monkeypatch.setattr(ctl, "call_owner", call_owner)

    class Req:
        content_type = "application/json"

        async def json(self):
            return {"question": "  "}
    res = run(ctl.http_test_call(Req()))
    assert asked and "خلصت الشغل" in asked[0] and res.status == 200


def test_post_must_be_json():
    from aiohttp.test_utils import make_mocked_request

    async def handler(request):
        return daemon.web.Response(text="ok")
    form = make_mocked_request("POST", "/call", headers={"Content-Type": "text/plain"})
    with pytest.raises(daemon.web.HTTPUnsupportedMediaType):
        run(daemon.json_only(form, handler))
    ok = make_mocked_request("POST", "/call", headers={"Content-Type": "application/json"})
    assert run(daemon.json_only(ok, handler)).status == 200
