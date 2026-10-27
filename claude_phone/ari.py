"""Minimal async ARI client: REST calls plus an event stream that call flows can await on."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import aiohttp

from . import log

APP = "claude-phone"


class Hangup(Exception):
    """The channel left Stasis (caller hung up) while a flow was waiting on it."""


class NotFound(RuntimeError):
    """ARI 404: usually a channel that hung up a moment ago."""


class ARI:
    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(user, password)
        self.http: aiohttp.ClientSession | None = None
        self._waiters: list[tuple[Callable[[dict], bool], asyncio.Future]] = []
        self._handlers: list[Callable[[dict], Any]] = []
        self.on_connect: list[Callable[[], Any]] = []
        self.connected = asyncio.Event()
        self.lg = log.get("ari")

    async def start(self) -> None:
        self.http = aiohttp.ClientSession(auth=self.auth)

    async def close(self) -> None:
        if self.http:
            await self.http.close()

    # --- REST ---

    async def req(self, method: str, path: str, ok404: bool = False, body: dict | None = None, **params) -> Any:
        params = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items() if v is not None}
        async with self.http.request(method, f"{self.base}{path}", params=params, json=body,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 404:
                if ok404:
                    return None
                raise NotFound(f"ARI {method} {path} -> 404: {await r.text()}")
            if r.status >= 400:
                raise RuntimeError(f"ARI {method} {path} -> {r.status}: {await r.text()}")
            if r.content_type == "application/json":
                return await r.json()
            return None

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, **kw):
        return self.req("POST", path, **kw)

    def put(self, path, **kw):
        return self.req("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.req("DELETE", path, **kw)

    # --- events ---

    def on_event(self, handler: Callable[[dict], Any]) -> None:
        self._handlers.append(handler)

    def wait_for(self, pred: Callable[[dict], bool]) -> asyncio.Future:
        """Future resolving with the first event matching pred. Register before triggering the action."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    def cancel_wait(self, fut: asyncio.Future) -> None:
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
        if not fut.done():
            fut.cancel()

    def _dispatch(self, ev: dict) -> None:
        keep = []
        for pred, fut in self._waiters:
            if fut.done():
                continue
            try:
                hit = pred(ev)
            except Exception:
                hit = False
            if hit:
                fut.set_result(ev)
            else:
                keep.append((pred, fut))
        self._waiters = keep
        for h in self._handlers:
            try:
                r = h(ev)
                if asyncio.iscoroutine(r):
                    asyncio.create_task(r)
            except Exception as e:
                self.lg.exception("event handler failed: %r", e)

    async def run_events(self) -> None:
        """Connect the event websocket and reconnect forever (Asterisk may restart)."""
        ws_url = self.base.replace("http", "ws", 1) + "/events"
        while True:
            try:
                async with self.http.ws_connect(ws_url, params={"app": APP, "subscribeAll": "false"},
                                                heartbeat=20) as ws:
                    self.lg.info("ARI events connected")
                    self.connected.set()
                    for cb in self.on_connect:
                        asyncio.create_task(cb())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._dispatch(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except (aiohttp.ClientError, OSError) as e:
                self.lg.warning("ARI events unavailable: %r", e)
            self.connected.clear()
            await asyncio.sleep(2)


class Call:
    """Helpers bound to one channel. Any wait raises Hangup if the channel leaves Stasis."""

    def __init__(self, ari: ARI, channel_id: str):
        self.ari = ari
        self.id = channel_id
        self.gone = ari.wait_for(lambda e: e.get("type") in ("StasisEnd", "ChannelDestroyed")
                                 and e.get("channel", {}).get("id") == channel_id)
        self.digits: asyncio.Queue[str] = asyncio.Queue()
        self._last_dtmf: tuple | None = None
        ari.on_event(self._dtmf)
        self._n = 0

    def _dtmf(self, ev: dict) -> None:
        if ev.get("type") == "ChannelDtmfReceived" and ev.get("channel", {}).get("id") == self.id:
            # The phone endpoint subscription delivers each of its channel events a second time.
            key = (ev.get("timestamp"), ev["digit"])
            if key == self._last_dtmf:
                return
            self._last_dtmf = key
            self.digits.put_nowait(ev["digit"])

    def close(self) -> None:
        if self._dtmf in self.ari._handlers:
            self.ari._handlers.remove(self._dtmf)
        self.ari.cancel_wait(self.gone)

    @property
    def up(self) -> bool:
        return not self.gone.done()

    async def next_digit(self, timeout: float) -> str | None:
        """The next key pressed, or None after `timeout` seconds."""
        task = asyncio.ensure_future(self.digits.get())
        done, _ = await asyncio.wait({task, self.gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        if self.gone in done:
            raise Hangup()
        return None

    def drain_digits(self) -> None:
        while not self.digits.empty():
            self.digits.get_nowait()

    async def _race(self, fut: asyncio.Future, timeout: float | None = None):
        done, _ = await asyncio.wait({fut, self.gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            return fut.result()
        self.ari.cancel_wait(fut)
        if self.gone in done:
            raise Hangup()
        raise asyncio.TimeoutError()

    async def _chan(self, method: str, path: str, **params):
        """A channel request; 404 means the caller has just hung up."""
        try:
            return await self.ari.req(method, path, **params)
        except NotFound:
            raise Hangup()

    async def answer(self) -> None:
        await self._chan("POST", f"/channels/{self.id}/answer")

    async def hangup(self) -> None:
        if not self.gone.done():
            await self.ari.delete(f"/channels/{self.id}", ok404=True)

    async def play(self, *media: str, interrupt: str = "", until: asyncio.Future | None = None) -> str | None:
        """Play media in order. If a digit in `interrupt` is pressed, stop and return it.
        If `until` completes first, stop and return "until"."""
        self._n += 1
        pb_id = f"{self.id}-pb{self._n}"
        finished = self.ari.wait_for(lambda e: e.get("type") == "PlaybackFinished"
                                     and e.get("playback", {}).get("id") == pb_id)
        try:
            await self._chan("POST", f"/channels/{self.id}/play/{pb_id}", media=",".join(media))
        except Hangup:
            self.ari.cancel_wait(finished)
            raise
        if not interrupt and until is None:
            await self._race(finished)
            return None
        while True:
            digit_task = asyncio.ensure_future(self.digits.get())
            waits = {finished, digit_task, self.gone} | ({until} if until is not None else set())
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if until is not None and until in done:
                digit_task.cancel()
                self.ari.cancel_wait(finished)
                await self.ari.delete(f"/playbacks/{pb_id}", ok404=True)
                return "until"
            if digit_task in done and digit_task.result() in interrupt:
                self.ari.cancel_wait(finished)
                await self.ari.delete(f"/playbacks/{pb_id}", ok404=True)
                return digit_task.result()
            if digit_task not in done:
                digit_task.cancel()
            if finished in done:
                return None
            if self.gone in done:
                self.ari.cancel_wait(finished)
                raise Hangup()

    async def record(self, name: str, max_seconds: int, terminate_on: str = "#",
                     max_silence: int | None = None) -> tuple[str, str | None]:
        """Record to <spool>/recording/<name>.wav16. Returns (how_it_ended, digit):
        'digit' (with the digit pressed), 'hangup', 'finished' (hangup or max length) or 'timeout'."""
        self.drain_digits()
        finished = self.ari.wait_for(lambda e: e.get("type") in ("RecordingFinished", "RecordingFailed")
                                     and e.get("recording", {}).get("name") == name)
        try:
            await self._chan("POST", f"/channels/{self.id}/record", name=name, format="wav16",
                             maxDurationSeconds=max_seconds, ifExists="overwrite", beep=False,
                             terminateOn=terminate_on, maxSilenceSeconds=max_silence)
        except Hangup:
            self.ari.cancel_wait(finished)
            raise
        try:
            await self._race(finished, timeout=max_seconds + 5)
        except Hangup:
            # Asterisk still writes what was captured before the hangup.
            return "hangup", None
        except asyncio.TimeoutError:
            await self.ari.post(f"/recordings/live/{name}/stop", ok404=True)
            return "timeout", None
        digit = None if self.digits.empty() else self.digits.get_nowait()
        # Without a digit, the recording finished because the caller hung up or the time limit hit.
        return ("digit", digit) if digit else ("finished", None)
