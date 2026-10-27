import math
import struct
import wave

from claude_phone import stt, tts
from claude_phone.daemon import SPINNER
from claude_phone.config import Settings


def test_spoken_label():
    assert tts.spoken_label("lemon-stand/feat/ags-2") == "lemon stand, feat ags 2"
    assert tts.spoken_label("site-be-v2") == "site be v2"


def test_spinner_detects_running_turn():
    assert SPINNER.search("· Generating… (51s · ↓ 4.3k tokens · thought for 1s)")
    assert SPINNER.search("✻ Pondering… (1m 3s · esc to interrupt)")
    assert not SPINNER.search("❯ \n  ⏵⏵ auto mode on")


def _wav(path, seconds, amp):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 16000)))
                               for i in range(int(16000 * seconds))))


def test_speech_gate(tmp_path):
    _wav(tmp_path / "quiet.wav", 1, 50)
    _wav(tmp_path / "voice.wav", 1, 800)  # quiet handset speech: RMS ~565
    assert stt.speech_seconds(tmp_path / "quiet.wav") == 0
    assert stt.speech_seconds(tmp_path / "voice.wav") > 0.9


def test_noise_transcripts_discarded():
    for t in ("Thank you.", "[BLANK_AUDIO]", "(silence)", "you", "..."):
        assert stt._NOISE.match(t)
    assert not stt._NOISE.match("Yes, ship it.")


def test_owner_name_sets_tool_suffix_and_sentence_case():
    assert Settings().owner_slug == "user" and Settings().owner == "The user"
    s = Settings(owner_name="Sam Lee")
    assert s.owner_slug == "sam_lee" and s.owner == "Sam Lee"


def test_lan_cidr_follows_detected_ip(monkeypatch):
    from claude_phone import config
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.1.2.33")
    s = config.load()
    assert (s.lan_ip, s.lan_cidr) == ("10.1.2.33", "10.1.2.0/24")


def test_changing_the_default_voice_renders_new_speech(monkeypatch):
    from claude_phone import config
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["say"], "render", lambda text, voice, out: (rendered.append(voice), out.write_bytes(b"")))
    monkeypatch.setattr(config, "load", lambda: Settings(tts_provider="say", say_voice="Daniel"))
    a = tts.render("Hello there.")
    monkeypatch.setattr(config, "load", lambda: Settings(tts_provider="say", say_voice="Fiona"))
    b = tts.render("Hello there.")
    assert a != b and rendered == ["Daniel", "Fiona"]


def test_elevenlabs_key_from_dotenv(tmp_path, monkeypatch):
    from claude_phone import config, keychain
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(config, "REPO", tmp_path)
    monkeypatch.setattr(keychain, "get", lambda name: None)
    (tmp_path / ".env").write_text('# comment\nOTHER=1\nexport ELEVENLABS_API_KEY="sk_test"\n')
    assert keychain.elevenlabs_key() == "sk_test"


def test_pronunciations_respell_whole_words():
    fixes = {"محمد": "مُحَمَّد", "nginx": "engine x"}
    assert tts.pronounce("هلا أبو محمد!", fixes) == "هلا أبو مُحَمَّد!"
    assert tts.pronounce("restart nginx now, not nginxes", fixes) == "restart engine x now, not nginxes"


def test_strip_tags_for_models_without_audio_tags():
    assert tts.strip_tags("[happy] Done! [short pause] Push it?") == "Done! Push it?"


def test_language_picks_phrase_table(monkeypatch):
    from claude_phone import config
    monkeypatch.setattr(config, "load", lambda: Settings(language="en"))
    assert tts.PHRASES["sent"] == "Sent." and tts.OPERATOR["or"] == " or "
    monkeypatch.setattr(config, "load", lambda: Settings(language="ar"))
    assert tts.PHRASES["sent"] == "وصلت." and set(tts.PHRASES_AR) == set(tts.PHRASES_EN)
    assert set(tts.OPERATOR_AR) == set(tts.OPERATOR_EN)


def test_paid_voice_falls_back_to_free_voice(monkeypatch, tmp_path):
    import io, urllib.error, urllib.request
    from claude_phone import config, keychain
    monkeypatch.setattr(config, "load", lambda: Settings(elevenlabs_voice_id="paid", elevenlabs_fallback_voice_id="free"))
    monkeypatch.setattr(keychain, "elevenlabs_key", lambda: "k")
    asked = []

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def urlopen(req, timeout=0):
        asked.append(req.full_url.split("/")[-1].split("?")[0])
        if "paid" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 402, "paid", {}, None)
        return R(b"\0\0" * 160)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    tts.PROVIDERS["elevenlabs"].render("hi", None, tmp_path / "o.wav")
    assert asked == ["paid", "free"]
