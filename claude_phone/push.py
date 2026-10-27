"""Push notifications via ntfy when a call doesn't reach the owner. The topic name is the only access control
on the public ntfy.sh server, so it's a random secret kept in the keychain (`phone push` creates it)."""

from __future__ import annotations

import asyncio
import urllib.request

from . import config, keychain, log

lg = log.get("push")


def topic() -> str | None:
    return keychain.get("ntfy")


def create_topic() -> str:
    t = topic()
    if not t:
        import secrets
        t = f"cphone-{secrets.token_urlsafe(18)}"
        keychain.put("ntfy", t)
    return t


def send(title: str, message: str, priority: str = "high", tags: str = "telephone_receiver") -> bool:
    t = topic()
    if not t:
        return False
    s = config.load()
    req = urllib.request.Request(f"{s.ntfy_server.rstrip('/')}/{t}", data=message.encode(), method="POST",
                                 headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags})
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        lg.info("push sent: %s", title)
        return True
    except Exception as e:
        lg.warning("push failed: %r", e)
        return False


def missed(kind: str, label: str, text: str, reason: str) -> None:
    """Fire and forget from the daemon's event loop."""
    s = config.load()
    body = f"{text}\n({reason})" if s.push_text else f"({reason})"
    asyncio.get_running_loop().run_in_executor(None, send, f"{kind} from {label}", body)
