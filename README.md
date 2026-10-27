# Claude Phone Arabic

**Claude Code calls your iPhone, in Saudi Arabic.** When a Claude Code session finishes a task or needs a
decision, it rings your phone, asks one short question in a natural Saudi voice, and types your spoken answer
back into the session. You can also call any session and talk to it.

```
Claude Code ──MCP──▶ daemon ──▶ Asterisk ──SIP over Wi-Fi──▶ iPhone (Linphone)
     ▲                 │   ▲                                      │
     └── your answer ──┘   └──── whisper.cpp (speech to text) ◀───┘
                       └──▶ ElevenLabs (Saudi voice)
```

> «هلا والله، خلصت الشغل طال عمرك، تامر على شي ثاني؟»

## For AI agents

If you are an AI agent asked to set this up or change it, read **[AGENTS.md](AGENTS.md)**: ordered setup steps,
the steps only the human can do, the `phone doctor` check, and code conventions. Summary:

| Item | Value |
|---|---|
| Platform | Apple Silicon Mac only (macOS, Homebrew, Python 3.12+), Claude Code running in iTerm2 |
| Install | `scripts/setup.sh` (idempotent; builds Asterisk, ~10 min) |
| Verify | `.venv/bin/phone doctor` (exit 0 = ready; prints a fix per failure) |
| Config | `~/.claude-phone/config.toml` (template: `config.example.toml`) |
| Secrets | `.env` → `ELEVENLABS_API_KEY` (user adds it; never ask for it in chat). Other secrets: macOS keychain |
| Human-only steps | ElevenLabs key, iPhone SIP app, macOS "control iTerm2" prompt |
| Test call | `curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8765/test-call` |
| Wire Claude Code | `.venv/bin/phone connect-claude` |
| Tests | `.venv/bin/pytest` |

## Features

- **Claude calls you:** the `call_<name>` tool asks a question out loud and returns your spoken answer as text.
- **Announcements:** `notify_<name>` says a short message ("the build finished") and hangs up.
- **Saudi Arabic first:** Saudi voice (Raed) on ElevenLabs `eleven_v3`, Saudi-dialect system phrases, Arabic
  speech recognition, and Claude is told to speak Saudi dialect on calls. English is one setting away.
- **You call Claude:** each session has an extension (301-399). Dial it, speak, and your words are typed in.
- **Voicemail:** missed questions wait on 500; answers go back to the sessions that asked.
- **Operator and all-hands:** dial 0 and say which project you want; dial 9 to message every session.
- **Test page:** `http://127.0.0.1:8765/` has one button that calls you.
- **Private by design:** speech recognition runs on your Mac. Only the text Claude says goes to ElevenLabs.

## Requirements

- Apple Silicon Mac (M1 or later) with [Homebrew](https://brew.sh), Xcode command line tools, Python 3.12+
- [Claude Code](https://claude.com/claude-code) running in [iTerm2](https://iterm2.com)
- iPhone with a free SIP app: [Linphone](https://apps.apple.com/app/linphone/id360065638) or Zoiper Lite
- iPhone and Mac on the same network (Wi-Fi, or the Mac on the iPhone's hotspot)
- [ElevenLabs](https://elevenlabs.io) account. The Saudi voice needs a paid plan (Starter, about $5/month);
  a free plan works with a built-in voice (Brian) automatically
- About 2 GB of disk

## Setup

### 1. Install

```sh
git clone https://github.com/AliCreating/claude-phone-arabic.git ~/claude-phone
cd ~/claude-phone
scripts/setup.sh
```

Clone into your home folder, not Documents or Desktop: if those sync to iCloud, macOS offloads files and the
tools hang. `setup.sh` installs the tools, builds Asterisk, creates `.venv`, copies the config and `.env`
templates, and starts three login services (Asterisk, whisper, the daemon).

### 2. Configure

- `~/.claude-phone/config.toml`: set `owner_name` (your name; the tools become `call_<name>`).
- `.env`: paste your ElevenLabs key: `ELEVENLABS_API_KEY=...`
- Apply: `.venv/bin/phone restart`

### 3. Connect the iPhone

In Linphone choose **Use SIP account** (Zoiper: skip the account screens):

| Field | Value |
|---|---|
| Username | `201` |
| Password | output of `.venv/bin/phone secret sip` |
| Domain | the Mac's IP: `ipconfig getifaddr en0` |
| Transport | UDP |

Keep the app open. Dial **600** to hear yourself (echo test).

### 4. Check and test

```sh
.venv/bin/phone doctor
```

Every line should say `OK`. Then open http://127.0.0.1:8765/ and press **Call me**.

### 5. Connect Claude Code

```sh
.venv/bin/phone connect-claude
```

This registers the MCP server and adds four hooks to `~/.claude/settings.json` (backed up first). Add
[docs/claude-md-snippet.md](docs/claude-md-snippet.md) to `~/.claude/CLAUDE.md` with your name, open a new Claude
Code session in iTerm2, and ask it to call you. The first time a call types into a session, macOS asks to let
the daemon control iTerm2: allow it.

## Keep the SIP app open

iOS freezes SIP apps in the background, and waking them needs a push service that a Mac doesn't have. So the
iPhone rings only while the app is open. Missed calls become voicemail (dial **500**). For a notification on a
locked phone, install the free [ntfy](https://ntfy.sh) app, run `.venv/bin/phone push`, subscribe to the topic
it prints (treat it as a password), then `.venv/bin/phone push test`.

## Configuration

`~/.claude-phone/config.toml`; every key is optional ([config.example.toml](config.example.toml) has them all).
Run `phone restart` after changes.

| Key | Default | What |
|---|---|---|
| `owner_name` | `"the user"` | your name, spoken on calls and in the tool names |
| `language` | `"ar"` | `"ar"` Saudi Arabic or `"en"` English: phrases, recognition, what Claude speaks |
| `caller_name` | `"Claude"` | name shown on the iPhone; `""` shows the session's repo/branch |
| `phone_ext` | `"201"` | the iPhone's SIP username |
| `answer_delay` | `3` | seconds after you pick up before Claude speaks |
| `end_pause` | `3` | seconds after Claude speaks, before the beep |
| `say_caller` | `false` | start each call by naming the calling project |
| `elevenlabs_voice_id` | Raed (Saudi) | main voice |
| `elevenlabs_fallback_voice_id` | Brian | used when the main voice needs a plan you don't have |
| `elevenlabs_model` | `"eleven_v3"` | `eleven_v3` acts out tags like `[happy]` |
| `elevenlabs_settings` | `{stability = 0.5}` | v3: 0.0 creative, 0.5 natural, 1.0 robust |
| `say_voice` | `"Majed"` | Mac voice if ElevenLabs fails; `"system"` = System Voice (can be Siri) |
| `whisper_language` | same as `language` | the language you answer in, or `"auto"` |
| `[pronunciations]` | none | respell words the voice gets wrong, e.g. `"محمد" = "مُحَمَّد"` |
| `daemon_port` | `8765` | local port for the daemon and test page |

Other Saudi voices (paid plan): Jeddawi `yXEnnEln9armDCyhkXcA`, Nasser AlJubaily `cFUFIbKkO2iZFwS8cRnY`,
Fahad `rpGHcNQJvO8dFNNFNj1v`, Abdulrahman `KfYUTAF49N2CPU3iKuMi`.

## Dial plan

| Ext | What |
|---|---|
| 301-399 | live Claude Code sessions (`phone list`). Mid-task: hold music until its turn ends; `#` leaves your message now |
| 0 | operator: say which project you want |
| 9 | all-hands: message every live session |
| 500 | voicemail (`#` skips, `7` deletes) |
| 600 | echo test |
| 601 | 1 kHz tone |

## Commands

```sh
phone doctor          # check every setup step, with fixes
phone status          # services, health, registered iPhone
phone list            # live sessions and their extensions
phone vm              # questions waiting in voicemail
phone restart         # after config changes
phone secret sip      # the iPhone's SIP password
phone connect-claude  # wire Claude Code (MCP server + hooks)
phone push            # set up missed-call notifications (ntfy)
tail -f ~/.claude-phone/logs/phone.log
```

`phone` is `.venv/bin/phone`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| SIP app stuck on "connecting" | The Mac's IP changed. `phone restart asterisk`, then update the domain in the app |
| Calls go straight to voicemail | The app isn't registered or was in the background: open it, check `phone status` |
| Call connects, no audio | iPhone and Mac must be on the same network |
| Answer transcript empty or wrong | Speak after the beep. Wrong language: set `whisper_language`, run `phone install-services` |
| A name is mispronounced | Add it to `[pronunciations]` with tashkeel |
| Text isn't typed into the session | The tab must be in `phone list`; allow iTerm2 automation (System Settings → Privacy & Security → Automation) |
| Tools hang / `No module named claude_phone` | Repo is in an iCloud folder: move it to `~/claude-phone` |
| Daemon not answering | Port 8765 taken: set `daemon_port`, `phone restart` |

## Documentation

- [docs/how-it-works.md](docs/how-it-works.md): architecture, call flows, voices, security model, files on disk
- [docs/build-notes.md](docs/build-notes.md): how Asterisk and the rest were made to work on macOS, and why
- [docs/claude-md-snippet.md](docs/claude-md-snippet.md): instructions that teach Claude when and how to call you
- [AGENTS.md](AGENTS.md): setup and contribution guide for AI agents

## Security and privacy

- No secrets in the repo: the ElevenLabs key lives in `.env` (git-ignored); the SIP, internal and ntfy secrets
  are generated into the macOS keychain.
- Asterisk accepts phones only from your local subnet. The daemon and test page listen on 127.0.0.1 only and
  accept JSON POSTs only, so websites you visit can't make your phone ring.
- Recordings, transcripts and logs stay in `~/.claude-phone/`.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Issues and pull requests welcome. See [AGENTS.md](AGENTS.md) for conventions.

## Credits

Based on [claude-phone](https://github.com/risingpower/claude-phone) by
[@risingpower](https://www.instagram.com/risingpower/), built for a Yealink desk phone. This version targets the
iPhone and Saudi Arabic voices.

## License

MIT. See [LICENSE](LICENSE).
