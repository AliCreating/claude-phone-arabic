# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor…) working in this repo. Humans: see README.md.

## What this repo is

claude-phone lets Claude Code sessions phone the user's iPhone and get spoken answers back, with Saudi Arabic
voices from ElevenLabs. It runs entirely on one Apple Silicon Mac: Asterisk (phone exchange), whisper.cpp
(speech to text), a Python daemon, and an MCP server for Claude Code. Architecture: docs/how-it-works.md.

## If the user asked you to set it up

Work through these in order. After each step, `.venv/bin/phone doctor` (once the venv exists) shows what is
done and prints the fix for anything missing. Done means `phone doctor` exits 0 and a test call works.

1. **Location.** If this repo is under `~/Documents` or `~/Desktop`, tell the user iCloud may offload files and
   make the tools hang; recommend moving it to `~/claude-phone` before continuing.
2. **Install.** Run `scripts/setup.sh`. It installs Homebrew packages, builds Asterisk (~10 minutes; run it in
   the background), creates `.venv`, copies `config.example.toml` to `~/.claude-phone/config.toml` and
   `.env.example` to `.env`, and starts the launchd services. It is safe to re-run.
3. **Owner name.** Ask the user what name calls should use. Set `owner_name` in `~/.claude-phone/config.toml`.
   Ask whether calls should be Saudi Arabic (`language = "ar"`, the default) or English (`"en"`).
4. **ElevenLabs key — human step.** The user creates a key at elevenlabs.io (Profile → API keys) and puts it in
   `.env` as `ELEVENLABS_API_KEY=...` themselves. Never ask for the key in chat, never print `.env`.
   Check it with `phone doctor` (it reports presence only). Without a key, calls use the Mac's voice.
   The default voice (Raed, Saudi) needs a paid ElevenLabs plan; on a free plan calls fall back to Brian
   automatically.
5. **iPhone — human step.** The user installs Linphone (or Zoiper Lite) and adds a SIP account. Give them:
   username `201`, domain = the Mac's IP (`ipconfig getifaddr en0`), transport UDP, password = output of
   `.venv/bin/phone secret sip` (they type or paste it into the app). The iPhone must be on the same
   network, with the app open. `phone doctor` shows "iPhone registered" when it worked.
6. **Restart** after config or `.env` changes: `.venv/bin/phone restart`.
7. **Test call.** Tell the user to keep the SIP app open, then either open `http://127.0.0.1:<daemon_port>/`
   (default 8765) and click the button, or run:
   `curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8765/test-call`
   The JSON reply says `answered` (with the transcript) or why not.
8. **Claude Code.** `.venv/bin/phone connect-claude` registers the MCP server and adds four hooks to
   `~/.claude/settings.json` (backs it up first). Then offer to add docs/claude-md-snippet.md to
   `~/.claude/CLAUDE.md` with the user's name. New Claude Code sessions in iTerm2 get the tools.
9. **macOS permission — human step.** The first time a call types into a session, macOS asks to let the
   daemon control iTerm2. The user must click Allow.

Things only the user can do: create accounts and API keys, put secrets in `.env`, set up the iPhone app,
answer macOS permission prompts. Ask; don't work around them.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| SIP app stuck on "connecting" | Mac changed network/IP | `phone restart asterisk`; user updates the domain in the app |
| Calls go to voicemail at once | iPhone not registered or app in background | user opens the app; check `phone status` |
| `No module named claude_phone`, hangs | repo in iCloud-synced folder | move to `~/claude-phone`, recreate `.venv` |
| Daemon not answering | port taken by another app | set `daemon_port` in config, `phone restart` |
| Answer transcript wrong language | `whisper_language` mismatch | set it in config, `phone install-services` (whisper's args are baked into launchd) |
| Name mispronounced | voice guesses Arabic vowels | add it to `[pronunciations]` with tashkeel |

Logs: `~/.claude-phone/logs/phone.log` (calls, transcripts), `~/.claude-phone/logs/*.out` (services).

## If you are changing the code

- Tests: `.venv/bin/pip install -e '.[dev]' && .venv/bin/pytest`. They need no Asterisk, audio or iTerm2.
  Keep them passing; add a test for new behaviour.
- Layout: `claude_phone/` (Python package), `asterisk/` (config templates with `@@VARS@@`, rendered by
  `phone config`), `scripts/`, `docs/`, `tests/`.
- Settings live in `claude_phone/config.py` (`Settings` dataclass). A new setting needs a default there, a line
  in `config.example.toml`, and a row in README's configuration table if users will touch it.
- System phrases live in `claude_phone/tts.py` in both `*_EN` and `*_AR` tables with the same keys
  (a test enforces this).
- Never commit secrets, personal names, phone numbers or IP addresses. `.env` is git-ignored; keep it that way.
- The daemon must never leave a call silent or lose a question: speech falls back to `say`, and any failure in
  `call_owner` becomes a voicemail.
