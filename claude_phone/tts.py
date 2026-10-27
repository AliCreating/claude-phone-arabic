"""Text to speech with swappable providers. Output is 16 kHz mono 16-bit WAV saved as *.wav16,
which Asterisk plays natively (no transcoding for G.722 calls). Results are cached by content hash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import config, keychain


class Provider:
    name = "base"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        raise NotImplementedError


def _to_wav16(src: Path, dst: Path) -> None:
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dst)],
                   check=True, capture_output=True)


class Say(Provider):
    name = "say"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        # "system" (or empty): the Mac's System Voice, the only way to use a Siri voice
        # (System Settings → Accessibility → Spoken Content → System Voice).
        voice = voice or config.load().say_voice
        pick = [] if voice in ("", "system") else ["-v", voice]
        with tempfile.TemporaryDirectory() as td:
            aiff = Path(td) / "t.aiff"
            subprocess.run(["say", *pick, "-o", str(aiff), text], check=True, capture_output=True)
            _to_wav16(aiff, out_wav)


class ElevenLabs(Provider):
    name = "elevenlabs"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        key = keychain.elevenlabs_key()
        if not key:
            raise RuntimeError("no ElevenLabs key (set ELEVENLABS_API_KEY or keychain item claude-phone/elevenlabs)")
        s = config.load()
        voice = voice or s.elevenlabs_voice_id
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
            data=json.dumps({"text": text if s.elevenlabs_model == "eleven_v3" else strip_tags(text),
                             "model_id": s.elevenlabs_model,
                             **({"voice_settings": s.elevenlabs_settings} if s.elevenlabs_settings else {})}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                pcm = r.read()
        except urllib.error.HTTPError as e:
            # 402/403: the voice needs a paid plan (Voice Library voices). Try the free fallback voice once.
            fallback = s.elevenlabs_fallback_voice_id
            if e.code in (402, 403) and fallback and voice != fallback:
                return self.render(text, fallback, out_wav)
            raise
        _write_wav(out_wav, pcm, 16000)


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


PROVIDERS: dict[str, Provider] = {p.name: p for p in (Say(), ElevenLabs())}


def voice_for(repo: str | None) -> tuple[str, str | None]:
    s = config.load()
    rv = s.repo_voices.get(repo or "", {}) if repo else {}
    return rv.get("provider", s.tts_provider), rv.get("voice")


def strip_tags(text: str) -> str:
    """Drop [audio tags] for models that would read them aloud (only eleven_v3 acts them out)."""
    import re
    return " ".join(re.sub(r"\[[^\]]{1,40}\]", " ", text).split())


def pronounce(text: str, fixes: dict) -> str:
    """Respell words the voice gets wrong. Whole words only; already-fixed text is left alone."""
    import re
    for word, said in fixes.items():
        text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", said, text)
    return text


def render(text: str, repo: str | None = None, voice: tuple[str, str | None] | None = None) -> str:
    """Render (or reuse) speech for text. Returns the Asterisk media URI ('sound:/abs/path' without extension).
    `voice` is (provider, voice) and overrides the repo's voice."""
    provider, voice = voice or voice_for(repo)
    text = pronounce(text, config.load().pronunciations)
    if not voice:  # name the default voice in the cache key, so changing it in config isn't masked by old speech
        s = config.load()
        voice = s.say_voice if provider == "say" else s.elevenlabs_voice_id
    s = config.load()
    style = [s.elevenlabs_model, s.elevenlabs_settings] if provider == "elevenlabs" else []
    key = hashlib.sha256(json.dumps([provider, voice, text, *style], sort_keys=True).encode()).hexdigest()[:20]
    base = config.SOUNDS / "tts" / key
    out = base.with_suffix(".wav16")
    if out.exists():
        out.touch()  # housekeeping expires speech by last use
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = base.with_suffix(".tmp.wav")
        try:
            PROVIDERS[provider].render(text, voice, tmp)
        except Exception:
            if provider == "say":
                raise
            PROVIDERS["say"].render(text, None, tmp)  # never leave a call silent
        tmp.rename(out)
    return f"sound:{base}"


async def render_async(text: str, repo: str | None = None, voice: tuple[str, str | None] | None = None) -> str:
    return await asyncio.to_thread(render, text, repo, voice)


def operator_voice() -> tuple[str, str | None]:
    s = config.load()
    return s.operator_provider, s.operator_voice or None


def op(text: str) -> str:
    return render(text, voice=operator_voice())


async def op_async(text: str) -> str:
    return await asyncio.to_thread(op, text)


def beep() -> str:
    """A short 880 Hz beep, generated once."""
    import math
    import struct
    base = config.SOUNDS / "beep"
    out = base.with_suffix(".wav16")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        rate, dur = 16000, 0.25
        n = int(rate * dur)
        frames = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 880 * i / rate) * min(1, i / 400, (n - i) / 400)))
            for i in range(n)
        )
        tmp = base.with_suffix(".tmp.wav")
        _write_wav(tmp, frames, rate)
        tmp.rename(out)
    return f"sound:{base}"


# Fixed phrases, pre-rendered on daemon start. `language` in config.toml picks the set.
PHRASES_EN = {
    "no_session": "There's no session on that extension.",
    "vm_none": "You have no messages. Back to work.",
    "vm_intro": "You have {n} {questions} waiting.",
    "vm_done": "That's everything.",
    "vm_from": "Question from {label}.",
    "vm_keys": "Answer after the beep. Press hash to skip, or seven to delete.",
    "vm_saved": "Got it.",
    "vm_skipped": "Skipped.",
    "vm_deleted": "Deleted.",
    "vm_session_gone": "That session has closed, so I've kept your answer in the log.",
    "connected": "You're through to {label}.",
    "sent": "Sent.",
    "nothing_heard": "I didn't catch anything, so nothing was sent.",
    "transcribe_failed": "Sorry, I couldn't transcribe that. Nothing was sent.",
    "inject_failed": "I couldn't reach that tab. Nothing was sent.",
    "busy": "The line's busy.",
    "goodbye": "That's all. Bye!",
    "allhands_none": "There are no live sessions to talk to.",
    "allhands_intro": "All hands. Your message goes to {n} {sessions}.",
    "allhands_sent": "Sent to {n} {sessions}.",
}

# Saudi dialect. Templates may ignore the English plural words passed to .format().
PHRASES_AR = {
    "no_session": "ما فيه جلسة على هالرقم.",
    "vm_none": "ما عندك رسايل، كمّل شغلك.",
    "vm_intro": "عندك {n} أسئلة تنتظرك.",
    "vm_done": "خلصنا، هذا كل شي.",
    "vm_from": "سؤال من {label}.",
    "vm_keys": "جاوب بعد الصافرة. اضغط مربع عشان تتخطى، أو سبعة عشان تحذف.",
    "vm_saved": "وصل.",
    "vm_skipped": "تخطيناه.",
    "vm_deleted": "انحذف.",
    "vm_session_gone": "الجلسة انقفلت، بس حفظت جوابك في السجل.",
    "connected": "أنت الحين مع {label}.",
    "sent": "وصلت.",
    "nothing_heard": "ما سمعت شي، فما أرسلت شي.",
    "transcribe_failed": "المعذرة، ما قدرت أفهم الكلام. ما أرسلت شي.",
    "inject_failed": "ما قدرت أوصل للتبويب. ما أرسلت شي.",
    "busy": "الخط مشغول.",
    "goodbye": "هذا كل شي، مع السلامة!",
    "allhands_none": "ما فيه جلسات شغالة الحين.",
    "allhands_intro": "رسالة للكل، بتوصل لـ {n} جلسات.",
    "allhands_sent": "وصلت لـ {n} جلسات.",
}


def _language() -> str:
    return "ar" if config.load().language == "ar" else "en"


def phrase(key: str, repo: str | None = None, **kw) -> str:
    return render(PHRASES[key].format(**kw), repo)


def spoken_label(label: str) -> str:
    """'lemon-stand/feat/ags-2' -> 'lemon stand, feat ags 2' so say/ElevenLabs read it naturally."""
    repo, _, branch = label.partition("/")
    clean = lambda x: " ".join(x.replace("-", " ").replace("_", " ").replace("/", " ").split())
    return f"{clean(repo)}, {clean(branch)}" if branch else clean(repo)


OPERATOR_EN = {
    "greeting": "Operator, how can I help?",
    "connecting": "Putting you through to {label}. Hold the line.",
    "which": "I've got {options}. Which one would you like?",
    "option": "{label}, extension {ext}",
    "or": " or ",
    "unplaced": "Sorry caller, I can't place that one. Try dialling the extension directly. Goodbye!",
    "again": "Sorry caller, I didn't catch that. Who was it you wanted?",
    "nobody": "Sorry caller, there's nobody on the switchboard right now. Goodbye!",
    "else": "Anything else?",
    "bye": "Righto. Goodbye!",
    "hold": "{label} is busy on a task. Please hold, and I'll put you through the moment it's done. "
            "Or press hash to leave your message now.",
    "important": "Your call is important to us. Please continue to hold, or press hash to leave your message now.",
}

OPERATOR_AR = {
    "greeting": "السنترال، وش أقدر أخدمك؟",
    "connecting": "أحولك على {label}، لحظة.",
    "which": "عندي {options}. أي واحد تبي؟",
    "option": "{label}، رقم {ext}",
    "or": " أو ",
    "unplaced": "المعذرة، ما قدرت أوصلك. جرب تتصل على الرقم مباشرة. مع السلامة!",
    "again": "المعذرة، ما سمعتك زين. مين تبي؟",
    "nobody": "ما فيه أحد على السنترال الحين. مع السلامة!",
    "else": "تبي شي ثاني؟",
    "bye": "تمام، مع السلامة!",
    "hold": "{label} مشغول بمهمة. انتظر شوي وأحولك أول ما يخلص، أو اضغط مربع وخل رسالتك الحين.",
    "important": "اتصالك يهمنا. انتظر شوي، أو اضغط مربع وخل رسالتك الحين.",
}


class _ByLanguage(dict):
    """PHRASES / OPERATOR: the table for config.toml's `language`, looked up on every access."""
    def __init__(self, en: dict, ar: dict):
        super().__init__(en)
        self._tables = {"en": en, "ar": ar}

    def _table(self) -> dict:
        return self._tables[_language()]

    def __getitem__(self, key):
        return self._table()[key]

    def items(self):
        return self._table().items()

    def values(self):
        return self._table().values()


PHRASES = _ByLanguage(PHRASES_EN, PHRASES_AR)
OPERATOR = _ByLanguage(OPERATOR_EN, OPERATOR_AR)


def prerender() -> int:
    from . import hold
    beep()
    hold.render()
    n = 0
    for v in OPERATOR.values():
        if "{" not in v:
            op(v)
    for k, v in PHRASES.items():
        if "{" not in v:
            render(v)
            n += 1
    return n
