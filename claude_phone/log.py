"""One append-only log for calls, transcripts and injections: tail -f ~/.claude-phone/logs/phone.log"""

from __future__ import annotations

import logging
import logging.handlers

from . import config

_configured = False


def get(name: str = "phone") -> logging.Logger:
    global _configured
    if not _configured:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        # Reopens the file after housekeeping rotates it, so long-lived processes follow the rotation.
        h = logging.handlers.WatchedFileHandler(config.LOG_FILE)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s"))
        root = logging.getLogger("phone")
        root.addHandler(h)
        root.setLevel(logging.INFO)
        _configured = True
    return logging.getLogger("phone" if name == "phone" else f"phone.{name}")
