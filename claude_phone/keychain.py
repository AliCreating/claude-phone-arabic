"""Secrets live in the macOS login keychain under service 'claude-phone'."""

from __future__ import annotations

import os
import secrets as _secrets
import subprocess

SERVICE = "claude-phone"


def get(name: str) -> str | None:
    env = os.environ.get(f"CLAUDE_PHONE_{name.upper().replace('-', '_')}")
    if env:
        return env
    r = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def put(name: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
        check=True, capture_output=True,
    )


def get_or_create(name: str, nbytes: int = 12) -> str:
    v = get(name)
    if not v:
        v = _secrets.token_hex(nbytes)
        put(name, v)
    return v


def dotenv(name: str) -> str | None:
    """A value from the repo's .env (or ~/.claude-phone/.env): plain KEY=value lines, # comments."""
    from . import config
    for path in (config.REPO / ".env", config.HOME / ".env"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            k, sep, v = line.strip().partition("=")
            if sep and k.strip().removeprefix("export ").strip() == name:
                v = v.strip().strip('"').strip("'")
                if v:
                    return v
    return None


def elevenlabs_key() -> str | None:
    """ELEVENLABS_API_KEY from the environment, then .env, then the keychain (account 'elevenlabs')."""
    return os.environ.get("ELEVENLABS_API_KEY") or dotenv("ELEVENLABS_API_KEY") or get("elevenlabs")
