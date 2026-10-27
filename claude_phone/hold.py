"""Hold music: Greensleeves (traditional, public domain) synthesised to a 16 kHz *.wav16, so no audio lives in the repo."""

from __future__ import annotations

import math
import re
import struct

from . import config

# (note, length in beats); 3 beats a bar, after a one-beat pickup.
_VERSE = [("C5", 2), ("D5", 1), ("E5", 1.5), ("F5", .5), ("E5", 1), ("D5", 2), ("B4", 1), ("G4", 1.5), ("A4", .5), ("B4", 1)]
_CHORUS = [("G5", 3), ("G5", 1.5), ("F#5", .5), ("E5", 1), ("D5", 2), ("B4", 1), ("G4", 1.5), ("A4", .5), ("B4", 1)]
_HALF = [("C5", 2), ("A4", 1), ("A4", 1.5), ("G#4", .5), ("A4", 1), ("B4", 2), ("G#4", 1)]
_HOME = [("C5", 1.5), ("B4", .5), ("A4", 1), ("G#4", 1.5), ("F#4", .5), ("G#4", 1), ("A4", 3), ("A4", 3)]
MELODY = ([("A4", 1)] + _VERSE + _HALF + [("E4", 2), ("A4", 1)] + _VERSE + _HOME
          + _CHORUS + _HALF + [("E4", 3)] + _CHORUS + _HOME)
# One bass root per bar.
BASS = ("A2 C3 G2 E2 A2 A2 E2 E2  A2 C3 G2 E2 A2 E2 A2 A2  "
        "C3 C3 G2 E2 A2 A2 E2 E2  C3 C3 G2 E2 A2 E2 A2 A2").split()

RATE = 16000
BEAT = 0.55  # seconds


def _freq(note: str) -> float:
    name, octave = re.fullmatch(r"([A-G]#?)(\d)", note).groups()
    semis = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"].index(name)
    return 440.0 * 2 ** ((semis - 9) / 12 + int(octave) - 4)


def _tone(buf: list[float], start: float, freq: float, dur: float, amp: float, decay: float) -> None:
    """Electric-piano-ish: fundamental plus a little 2nd and 3rd harmonic, quick attack, exponential decay."""
    i0, n = int(start * RATE), int((dur + 0.25) * RATE)
    for i in range(n):
        if i0 + i >= len(buf):
            break
        t = i / RATE
        env = min(1.0, i / 80) * math.exp(-t * decay) * min(1.0, (n - i) / 400)
        w = 2 * math.pi * freq * t
        buf[i0 + i] += amp * env * (math.sin(w) + 0.3 * math.sin(2 * w) + 0.1 * math.sin(3 * w))


def render() -> str:
    """Asterisk media URI for the hold music, synthesised on first use."""
    base = config.SOUNDS / "hold-greensleeves"
    out = base.with_suffix(".wav16")
    if out.exists():
        return f"sound:{base}"
    total = sum(b for _, b in MELODY) * BEAT
    buf = [0.0] * int((total + 0.5) * RATE)
    t = 0.0
    for note, beats in MELODY:
        if note != "rest":
            _tone(buf, t, _freq(note), beats * BEAT, 0.32, 2.2)
        t += beats * BEAT
    for bar, note in enumerate(BASS):
        _tone(buf, (1 + bar * 3) * BEAT, _freq(note), 3 * BEAT, 0.22, 1.2)
    peak = max(abs(x) for x in buf) or 1.0
    frames = b"".join(struct.pack("<h", int(x / peak * 12000)) for x in buf)
    from .tts import _write_wav
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = base.with_suffix(".tmp.wav")
    _write_wav(tmp, frames, RATE)
    tmp.rename(out)
    return f"sound:{base}"
