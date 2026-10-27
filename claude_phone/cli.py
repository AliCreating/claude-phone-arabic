"""`phone` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config, keychain, registry

RUN = config.HOME / "run"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{}"


# --- Asterisk config ---

def render_asterisk_config() -> None:
    s = config.load()
    subs = {
        "ETC": str(config.ASTERISK_ETC), "PREFIX": str(config.ASTERISK_PREFIX), "HOME": str(config.HOME),
        "RUN": str(config.ASTERISK_RUN), "ARI_PORT": str(s.ari_port), "LAN_IP": s.lan_ip,
        "LAN_CIDR": s.lan_cidr, "PHONE_EXT": s.phone_ext,
        "SIP_PASSWORD": keychain.get_or_create("sip"), "ARI_PASSWORD": keychain.get_or_create("ari"),
    }
    config.ASTERISK_ETC.mkdir(parents=True, exist_ok=True)
    (config.HOME / "logs" / "asterisk").mkdir(parents=True, exist_ok=True)
    for tmpl in sorted((config.REPO / "asterisk").glob("*.conf.tmpl")):
        text = tmpl.read_text()
        for k, v in subs.items():
            text = text.replace(f"@@{k}@@", v)
        out = config.ASTERISK_ETC / tmpl.name.removesuffix(".tmpl")
        out.write_text(text)
        out.chmod(0o600)


def asterisk_cmd(*args: str) -> list[str]:
    return [str(config.ASTERISK_BIN), "-C", str(config.ASTERISK_ETC / "asterisk.conf"), *args]


def asterisk_rx(cmd: str) -> str:
    r = subprocess.run(asterisk_cmd("-rx", cmd), capture_output=True, text=True, timeout=10)
    return r.stdout


# --- processes ---

def _pid(name: str) -> int | None:
    f = RUN / f"{name}.pid"
    try:
        pid = int(f.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _spawn(name: str, argv: list[str]) -> None:
    if _pid(name):
        print(f"{name}: already running")
        return
    RUN.mkdir(parents=True, exist_ok=True)
    out = open(config.LOGS / f"{name}.out", "a")
    p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    (RUN / f"{name}.pid").write_text(str(p.pid))
    print(f"{name}: started (pid {p.pid})")


def _stop(name: str) -> None:
    pid = _pid(name)
    if not pid:
        print(f"{name}: not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    (RUN / f"{name}.pid").unlink(missing_ok=True)
    print(f"{name}: stopped")


def model_path() -> Path:
    return config.MODELS / config.load().whisper_model


def ensure_model() -> None:
    p = model_path()
    if not p.exists():
        print(f"downloading {p.name} ...")
        config.MODELS.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL.format(p.name), p.with_suffix(".part"))
        p.with_suffix(".part").rename(p)


def py_argv(*args: str) -> list[str]:
    return [sys.executable, "-m", "claude_phone", *args]


SERVICES = {
    "asterisk": lambda s: asterisk_cmd("-f", "-n"),
    "whisper": lambda s: [shutil.which("whisper-server") or "/opt/homebrew/bin/whisper-server", "-m", str(model_path()), "--host", "127.0.0.1",
                          "--port", str(s.whisper_port), "-t", "4", "--no-timestamps", "-l", s.whisper_language],
    "daemon": lambda s: py_argv("daemon"),
}


# --- launchd (user agents; no sudo) ---

AGENTS = Path.home() / "Library" / "LaunchAgents"


def _label(name: str) -> str:
    return f"com.claude-phone.{name}"


def _plist(name: str) -> Path:
    return AGENTS / f"{_label(name)}.plist"


def _launchd(name: str) -> bool:
    return _plist(name).exists()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _agent_pid(name: str) -> int | None:
    r = _launchctl("print", f"{_domain()}/{_label(name)}")
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            return int(line.split("=")[1])
    return None


def write_plist(name: str, argv: list[str]) -> None:
    import plistlib
    AGENTS.mkdir(parents=True, exist_ok=True)
    out = str(config.LOGS / f"{name}.out")
    plistlib.dump({
        "Label": _label(name),
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": out,
        "StandardErrorPath": out,
        "WorkingDirectory": str(config.HOME),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }, open(_plist(name), "wb"))


def cmd_install_services(a) -> None:
    config.ensure_dirs()
    s = config.load()
    render_asterisk_config()
    ensure_model()
    for n in SERVICES:
        _stop(n)  # anything started by hand
        _launchctl("bootout", f"{_domain()}/{_label(n)}")
        write_plist(n, SERVICES[n](s))
        r = _launchctl("bootstrap", _domain(), str(_plist(n)))
        print(f"{n}: {'installed' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")


def cmd_uninstall_services(a) -> None:
    for n in SERVICES:
        _launchctl("bootout", f"{_domain()}/{_label(n)}")
        _plist(n).unlink(missing_ok=True)
        print(f"{n}: removed")


def cmd_up(a) -> None:
    config.ensure_dirs()
    s = config.load()
    render_asterisk_config()
    ensure_model()
    names = [a.only] if a.only else list(SERVICES)
    for n in names:
        if _launchd(n):
            r = _launchctl("bootstrap", _domain(), str(_plist(n)))
            print(f"{n}: {'started' if r.returncode == 0 else 'already loaded'} (launchd)")
            continue
        _spawn(n, SERVICES[n](s))
        if n == "asterisk":
            time.sleep(3)
        if n == "whisper":
            time.sleep(2)


def cmd_down(a) -> None:
    names = [a.only] if a.only else list(reversed(list(SERVICES)))
    for n in names:
        if _launchd(n):
            _launchctl("bootout", f"{_domain()}/{_label(n)}")
            print(f"{n}: stopped (launchd; `phone up` to start)")
            continue
        _stop(n)


def cmd_restart(a) -> None:
    names = [a.only] if a.only else list(SERVICES)
    if all(_launchd(n) for n in names):
        render_asterisk_config()
        for n in names:
            r = _launchctl("kickstart", "-k", f"{_domain()}/{_label(n)}")
            print(f"{n}: {'restarted' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")
        return
    cmd_down(a)
    cmd_up(a)


def cmd_status(a) -> None:
    w = config.ip_warning()
    if w:
        print("WARNING:", w)
    for n in SERVICES:
        pid = _agent_pid(n) if _launchd(n) else _pid(n)
        how = " (launchd)" if _launchd(n) else ""
        print(f"{n:9} {'running pid ' + str(pid) if pid else 'stopped'}{how}")
    try:
        with urllib.request.urlopen(f"{config.load().daemon_url}/health", timeout=3) as r:
            print("health   ", json.loads(r.read()))
    except Exception as e:
        print("health    unavailable:", e)
    if _pid("asterisk") or _agent_pid("asterisk"):
        print(asterisk_rx("pjsip show contacts").strip())


def cmd_list(a) -> None:
    rows = registry.all_sessions()
    if not rows:
        print("No live sessions.")
        return
    print(f"{'EXT':4} {'REPO':22} {'BRANCH':28} {'STATUS':8} SINCE")
    for s in rows:
        b = s.branch or ""
        if s.worktree:
            b = f"{b} [{s.worktree}]"
        since = datetime.fromtimestamp(s.started_at).strftime("%H:%M")
        print(f"{s.ext:<4} {s.repo[:22]:22} {b[:28]:28} {s.status:8} {since}")


def cmd_vm(a) -> None:
    for v in registry.pending_voicemails():
        t = datetime.fromtimestamp(v.created_at).strftime("%H:%M")
        print(f"#{v.id} {t} {v.label}: {v.question}")


def cmd_hook(a) -> None:
    from . import hooks
    sys.exit(hooks.run(a.event))


def cmd_daemon(a) -> None:
    from . import daemon
    daemon.run()


def cmd_mcp(a) -> None:
    from . import mcp_server
    mcp_server.run()


def cmd_secret(a) -> None:
    print(keychain.get_or_create(a.name))


def cmd_push(a) -> None:
    from . import push
    if a.action == "test":
        print("sent" if push.send("Claude Phone", "Test push: missed calls will show up here.") else "failed (see phone.log)")
        return
    t = push.create_topic()
    print(f"In the ntfy app, subscribe to this topic on {config.load().ntfy_server}:\n\n  {t}\n\n"
          "Treat it like a password. Then run: phone push test")


HOOK_EVENTS = {"SessionStart": "session-start", "SessionEnd": "session-end", "UserPromptSubmit": "prompt", "Stop": "stop"}


def cmd_connect_claude(a) -> None:
    """Register the MCP server with Claude Code and add the four hooks to ~/.claude/settings.json (backed up first)."""
    phone = str(Path(sys.executable).parent / "phone")
    r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "claude-phone", "--", phone, "mcp"],
                       capture_output=True, text=True)
    print("MCP server:", "added" if r.returncode == 0 else (r.stderr or r.stdout).strip())
    path = Path.home() / ".claude" / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    if path.exists():
        shutil.copy(path, path.with_name("settings.json.bak-claude-phone"))
    hooks = data.setdefault("hooks", {})
    added = 0
    for event, arg in HOOK_EVENTS.items():
        cmd = f'"{phone}" hook {arg}'
        groups = hooks.setdefault(event, [])
        if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": cmd}]})
            added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"hooks: {added} added to {path}" if added else "hooks: already there")
    print("Next: add docs/claude-md-snippet.md to ~/.claude/CLAUDE.md, then start a new Claude Code session in iTerm2.")


def doctor_checks() -> list[tuple[str, bool, str]]:
    """(check, passed, how to fix). Shared by `phone doctor`; ordered like the setup steps."""
    import platform
    s = config.load()
    repo = str(config.REPO)
    out: list[tuple[str, bool, str]] = []
    add = lambda name, ok, fix: out.append((name, bool(ok), fix))
    add("Apple Silicon Mac", platform.machine() == "arm64", "claude-phone needs an M1 or later Mac")
    add("repo outside iCloud Desktop/Documents",
        not any(repo.startswith(str(Path.home() / d)) for d in ("Documents", "Desktop")),
        "move the repo to ~/claude-phone (iCloud offloads files and the tools hang)")
    add("Homebrew", shutil.which("brew"), "install from https://brew.sh")
    add("whisper.cpp (whisper-server)", shutil.which("whisper-server") or Path("/opt/homebrew/bin/whisper-server").exists(),
        "brew install whisper.cpp")
    add("iTerm2", Path("/Applications/iTerm.app").exists(), "brew install --cask iterm2")
    add("Asterisk built", config.ASTERISK_BIN.exists(), "scripts/build-asterisk.sh")
    add("config file", config.CONFIG_FILE.exists(), f"cp config.example.toml {config.CONFIG_FILE}")
    add("owner_name set", s.owner_name != "the user", f"set owner_name in {config.CONFIG_FILE}")
    add("ElevenLabs key", keychain.elevenlabs_key(), "put ELEVENLABS_API_KEY=... in .env (human: elevenlabs.io → API keys)")
    add("speech model downloaded", model_path().exists(), "phone install-services (downloads it)")
    for n in SERVICES:
        add(f"service {n} running", _agent_pid(n) if _launchd(n) else _pid(n), "phone install-services")
    health = {}
    try:
        with urllib.request.urlopen(f"{s.daemon_url}/health", timeout=3) as r:
            health = json.loads(r.read())
    except Exception:
        pass
    add(f"daemon answering on 127.0.0.1:{s.daemon_port}", health.get("ok"),
        f"phone restart daemon; if the port is taken, set daemon_port in {config.CONFIG_FILE}")
    add("Mac address unchanged", health and not health.get("ip_warning"), "phone restart asterisk, then update the domain in the iPhone app")
    add(f"iPhone registered as {s.phone_ext}", health.get("phone_online"),
        f"human: SIP app on the iPhone, user {s.phone_ext}, domain {s.lan_ip}, password from `phone secret sip`, app open")
    mcp = subprocess.run(["claude", "mcp", "get", "claude-phone"], capture_output=True, text=True) if shutil.which("claude") else None
    add("Claude Code MCP server", mcp and mcp.returncode == 0, "phone connect-claude")
    settings = Path.home() / ".claude" / "settings.json"
    hooks = json.loads(settings.read_text()).get("hooks", {}) if settings.exists() else {}
    add("Claude Code hooks", all(any("phone\" hook" in h.get("command", "") or "phone hook" in h.get("command", "")
                                     for g in hooks.get(e, []) for h in g.get("hooks", [])) for e in HOOK_EVENTS),
        "phone connect-claude")
    return out


def cmd_doctor(a) -> None:
    checks = doctor_checks()
    for name, ok, fix in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n     fix: {fix}"))
    failed = sum(not ok for _, ok, _ in checks)
    print("\nAll good. Test call: http://127.0.0.1:%s/" % config.load().daemon_port if not failed else f"\n{failed} to fix.")
    sys.exit(1 if failed else 0)


def cmd_say(a) -> None:
    from . import tts
    print(tts.render(a.text))


def main() -> None:
    p = argparse.ArgumentParser(prog="phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("up", cmd_up), ("down", cmd_down), ("restart", cmd_restart)):
        sp = sub.add_parser(name)
        sp.add_argument("only", nargs="?", choices=list(SERVICES))
        sp.set_defaults(fn=fn)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("install-services", help="run everything under launchd").set_defaults(fn=cmd_install_services)
    sub.add_parser("uninstall-services").set_defaults(fn=cmd_uninstall_services)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("vm", help="pending voicemail questions").set_defaults(fn=cmd_vm)
    sp = sub.add_parser("hook")
    sp.add_argument("event", choices=["session-start", "session-end", "prompt", "stop"])
    sp.set_defaults(fn=cmd_hook)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    sp = sub.add_parser("secret", help="print (creating if needed) a keychain secret: sip (the iPhone's password) | ari")
    sp.add_argument("name", choices=["sip", "ari"])
    sp.set_defaults(fn=cmd_secret)
    sp = sub.add_parser("push", help="set up (or test) missed-call pushes to the ntfy app")
    sp.add_argument("action", nargs="?", choices=["setup", "test"], default="setup")
    sp.set_defaults(fn=cmd_push)
    sp = sub.add_parser("say", help="render text to speech, print the media URI")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_say)
    sub.add_parser("doctor", help="check every setup step; prints a fix for each failure").set_defaults(fn=cmd_doctor)
    sub.add_parser("connect-claude", help="add the MCP server and hooks to Claude Code").set_defaults(fn=cmd_connect_claude)
    sub.add_parser("config", help="regenerate Asterisk config").set_defaults(fn=lambda a: render_asterisk_config())
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
