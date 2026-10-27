"""Type text into an iTerm2 session via AppleScript (works with the tab unfocused and another app in front).
Only ever types into sessions from the registry; every attempt is logged."""

from __future__ import annotations

import subprocess

from . import log, registry

# Text then a separate CR: Claude Code's input treats a pasted trailing newline as part of the text.
_SCRIPT = r'''
on run argv
  set sid to item 1 of argv
  set mode to item 2 of argv
  -- Never launch iTerm2 just to look for a session.
  if application "iTerm2" is not running then
    if mode is "list" then return ""
    return "notfound"
  end if
  tell application "iTerm2"
    if mode is "list" then
      set ids to {}
      repeat with w in windows
        repeat with t in tabs of w
          repeat with s in sessions of t
            set end of ids to (unique id of s)
          end repeat
        end repeat
      end repeat
      set AppleScript's text item delimiters to linefeed
      return ids as text
    end if
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is sid then
            if mode is "read" then return contents of s
            tell s to write text (item 3 of argv) newline no
            delay 0.3
            tell s to write text (ASCII character 13) newline no
            return "sent"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''


def _osa(*args: str, timeout: float = 10) -> str:
    r = subprocess.run(["osascript", "-e", _SCRIPT, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.rstrip("\n")


def live_iterm_ids() -> set[str]:
    out = _osa("-", "list")
    return {x for x in out.splitlines() if x}


def read_screen(iterm_id: str) -> str:
    return _osa(iterm_id, "read")


def type_into(session: registry.Session, text: str, source: str) -> str:
    """Returns 'sent', 'notfound' or 'error:<msg>'."""
    lg = log.get("inject")
    # A single line: newlines inside would submit early.
    text = " ".join(text.split())
    if not session.iterm_id:
        result = "error:no iterm id"
    else:
        try:
            result = _osa(session.iterm_id, "type", text)
        except Exception as e:
            result = f"error:{e}"
    registry.log_injection(session, source, text, result)
    lg.info("inject ext=%s %s source=%s result=%s text=%r", session.ext, session.label, source, result, text)
    return result
