#!/usr/bin/env bash
# One-shot setup on an Apple Silicon Mac: tools, Asterisk, Python env, config, services.
# Safe to re-run: every step skips what is already done.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${CLAUDE_PHONE_HOME:-$HOME/.claude-phone}"
cd "$REPO"

case "$REPO" in
  "$HOME/Documents"*|"$HOME/Desktop"*)
    echo "Warning: $REPO may sync to iCloud, which offloads files and makes the tools hang."
    echo "Move the repo to ~/claude-phone if Desktop & Documents sync is on." ;;
esac

[ "$(uname -m)" = arm64 ] || { echo "Needs an Apple Silicon Mac."; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
xcode-select -p >/dev/null 2>&1 || { echo "Run: xcode-select --install"; exit 1; }

echo "==> Homebrew packages"
brew list openssl@3 >/dev/null 2>&1 || brew install openssl@3
brew list pkg-config >/dev/null 2>&1 || brew install pkg-config
command -v whisper-server >/dev/null || brew install whisper.cpp
[ -d /Applications/iTerm.app ] || brew install --cask iterm2

echo "==> Asterisk (about 10 minutes the first time)"
[ -x "$HOME_DIR/asterisk/sbin/asterisk" ] || scripts/build-asterisk.sh

echo "==> Python environment"
PY=""
for p in python3.13 python3.12 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "Needs Python 3.12+: brew install python@3.12"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q -e .

echo "==> Config"
mkdir -p "$HOME_DIR"
[ -f "$HOME_DIR/config.toml" ] || { cp config.example.toml "$HOME_DIR/config.toml"; echo "Created $HOME_DIR/config.toml: set owner_name."; }
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created .env: paste your ElevenLabs key (optional)."; }

echo "==> Services"
.venv/bin/phone install-services

cat <<EOF

Done. Next:
  0. Set owner_name in $HOME_DIR/config.toml and ELEVENLABS_API_KEY in $REPO/.env, then: .venv/bin/phone restart
  1. iPhone: install Linphone (or Zoiper Lite), add a SIP account:
       username 201, domain $(ipconfig getifaddr en0 2>/dev/null || echo "<this Mac's IP>"), transport UDP,
       password: $REPO/.venv/bin/phone secret sip
  2. Claude Code: $REPO/.venv/bin/phone connect-claude
  3. Check: $REPO/.venv/bin/phone doctor
  4. Test: open http://127.0.0.1:8765/ and click "Call me".
EOF
