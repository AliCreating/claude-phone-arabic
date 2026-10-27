"""Paths and settings. Settings come from ~/.claude-phone/config.toml (see config.example.toml)."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_PHONE_HOME", Path.home() / ".claude-phone"))
REPO = Path(__file__).resolve().parent.parent

ASTERISK_PREFIX = HOME / "asterisk"
ASTERISK_BIN = ASTERISK_PREFIX / "sbin" / "asterisk"
ASTERISK_ETC = HOME / "etc" / "asterisk"
# macOS unix socket paths are capped at 104 chars, so the run dir must be short.
ASTERISK_RUN = Path("/tmp/cphone")
DB_PATH = HOME / "registry.db"
SOUNDS = HOME / "sounds"
RECORDINGS = HOME / "recordings"
MODELS = HOME / "models"
LOGS = HOME / "logs"
LOG_FILE = LOGS / "phone.log"
CONFIG_FILE = HOME / "config.toml"


@dataclass
class Settings:
    # "ar": Saudi-dialect system phrases, Arabic speech recognition, Claude told to speak Saudi Arabic. "en": English.
    language: str = "ar"
    # Who the phone is for. Spoken and typed as this name; the MCP tools become call_<name> and notify_<name>.
    owner_name: str = "the user"
    # Empty: this Mac's address on its default route, and that /24. Pin both once the Mac has a DHCP reservation.
    lan_ip: str = ""
    lan_cidr: str = ""
    # The iPhone's extension: sign in to a SIP app (Linphone, Zoiper) as this, with `phone secret sip`.
    phone_ext: str = "201"
    # Caller name the iPhone shows. Empty: the calling session's repo/branch.
    caller_name: str = "Claude"
    voicemail_ext: str = "500"
    allhands_ext: str = "9"
    operator_ext: str = "0"
    first_session_ext: int = 301  # not 1xx: UK 101 is the police, 111 the NHS
    last_session_ext: int = 399
    ari_port: int = 8088
    daemon_port: int = 8765
    whisper_port: int = 8910
    whisper_language: str = ""  # what you speak on calls: "ar", "en", ... or "auto". Empty: same as `language`
    whisper_model: str = "ggml-large-v3-turbo-q5_0.bin"  # vs base.en on 23 real clips: same or better, 0.3 s each
    ring_seconds: int = 25
    answer_delay: float = 3  # seconds of silence after you pick up, before Claude speaks
    end_pause: float = 3  # seconds of silence after Claude speaks, before the beep (or hanging up)
    say_caller: bool = False  # start each call by naming the calling project ("lemon stand. ...")
    record_seconds: int = 60
    # Speech: ElevenLabs (free account works with its built-in voices) when ELEVENLABS_API_KEY is set, else the
    # Mac's own voice. eleven_v3 acts out audio tags like [happy] or [short pause] written into the text.
    tts_provider: str = "elevenlabs"
    # Raed, a Saudi voice from the ElevenLabs Voice Library (paid plans). Free plans can't use library
    # voices through the API, so those calls fall back to elevenlabs_fallback_voice_id (Brian, built in).
    elevenlabs_voice_id: str = "IK7YYZcSpmlkjKrQxbSn"
    elevenlabs_fallback_voice_id: str = "nPczCjzI2devNBz1zQrb"
    elevenlabs_model: str = "eleven_v3"
    # ElevenLabs voice_settings. v3 stability: 0.0 creative, 0.5 natural, 1.0 robust. Empty: the voice's own.
    elevenlabs_settings: dict = field(default_factory=lambda: {"stability": 0.5})
    say_voice: str = "Majed"  # last resort, the Mac's own voice (Majed: Arabic). "system" = the System Voice
    # The operator on 0. Empty operator_voice: the same voice as calls.
    operator_provider: str = "elevenlabs"
    operator_voice: str = ""
    # repo name -> {"provider": ..., "voice": ...}
    repo_voices: dict = field(default_factory=dict)
    # word -> how to spell it for the voice, e.g. {"محمد" = "مُحَمَّد"} (tashkeel) or {"nginx" = "engine x"}
    pronunciations: dict = field(default_factory=dict)
    # Push to the ntfy app when a call is missed. The topic is a secret in the keychain (`phone push`).
    ntfy_server: str = "https://ntfy.sh"
    push_text: bool = True  # include the question/announcement; False sends only who called

    @property
    def owner(self) -> str:
        """owner_name for the start of a sentence."""
        return self.owner_name[:1].upper() + self.owner_name[1:]

    @property
    def owner_slug(self) -> str:
        """'Sam Lee' -> 'sam_lee', 'the user' -> 'user': the suffix of the MCP tool names."""
        name = re.sub(r"^the\s+", "", self.owner_name.strip().lower())
        return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "user"

    @property
    def ari_url(self) -> str:
        return f"http://127.0.0.1:{self.ari_port}/ari"

    @property
    def daemon_url(self) -> str:
        return f"http://127.0.0.1:{self.daemon_port}"

    @property
    def whisper_url(self) -> str:
        return f"http://127.0.0.1:{self.whisper_port}"


def load() -> Settings:
    s = Settings()
    if CONFIG_FILE.exists():
        data = tomllib.loads(CONFIG_FILE.read_text())
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
    if not s.lan_ip:
        s.lan_ip = detect_lan_ip() or ""
    if not s.whisper_language:
        s.whisper_language = s.language
    if s.lan_ip and not s.lan_cidr:
        s.lan_cidr = s.lan_ip.rsplit(".", 1)[0] + ".0/24"
    return s


def bound_ip() -> str | None:
    """The address the generated Asterisk config listens on, which stays put when a detected lan_ip moves."""
    conf = ASTERISK_ETC / "pjsip.conf"
    m = re.search(r"^bind=([\d.]+):", conf.read_text(), re.M) if conf.exists() else None
    return m.group(1) if m else None


def detect_lan_ip() -> str | None:
    """IPv4 address of the interface on this Mac's default route."""
    try:
        route = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        iface = next(line.split(":", 1)[1].strip() for line in route.splitlines() if "interface:" in line)
        ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=5).stdout.strip()
        return ip or None
    except (StopIteration, OSError, subprocess.SubprocessError):
        return None


def current_lan_ips() -> set[str]:
    """IPv4 addresses on this Mac, to spot the Wi-Fi address drifting away from lan_ip."""
    out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    return {line.split()[1] for line in out.splitlines() if line.strip().startswith("inet ") and "127.0.0.1" not in line}


def ip_warning() -> str | None:
    s = load()
    ip = bound_ip() or s.lan_ip
    if not ip:
        return "This Mac has no network address, so the iPhone can't reach it."
    if ip in current_lan_ips():
        return None
    return (f"This Mac doesn't have {ip} any more, so the iPhone can't reach it: calls go to voicemail and "
            f"missed-call pushes. If the address changed for good, run `phone restart asterisk` (or set lan_ip in "
            f"{CONFIG_FILE}) and update the domain in the iPhone's SIP app.")


def ensure_dirs() -> None:
    for d in (HOME, ASTERISK_ETC, SOUNDS, RECORDINGS, MODELS, LOGS):
        d.mkdir(parents=True, exist_ok=True)
