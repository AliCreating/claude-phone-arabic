# How it works

claude-phone turns your Mac into a small phone exchange. Claude Code sessions reach you through it, and
you reach them. Everything runs locally except the ElevenLabs voice (text out, audio back).

```
                        ┌──────────────────────────── your Mac ─────────────────────────────┐
 Claude Code session    │                                                                    │
 (iTerm2 tab)           │   phone mcp ──HTTP──▶ daemon ──ARI (REST + WebSocket)──▶ Asterisk  │
   │  tool call         │   (stdio MCP)          │  ▲        │                       │ SIP/RTP │──Wi-Fi──▶ iPhone
   └───────────────────▶│                        │  │        ├──▶ ElevenLabs (TTS) ──┘         │          SIP app
                        │   hooks ──▶ registry ◀─┘  │        └──▶ whisper-server (STT, local)  │
   typed answer ◀───────│── inject (AppleScript) ◀──┘                                         │
                        └────────────────────────────────────────────────────────────────────┘
```

## Components

| Piece | File | Job |
|---|---|---|
| CLI | `claude_phone/cli.py` | `phone …`: install/start services, render Asterisk config, `doctor`, `connect-claude` |
| MCP server | `claude_phone/mcp_server.py` | One per Claude Code session (stdio). Exposes `call_<owner>`, `notify_<owner>`, `transfer_call`; forwards each to the daemon over HTTP on 127.0.0.1 |
| Daemon | `claude_phone/daemon.py` | Owns every call. Talks to Asterisk through ARI, renders speech, records answers, transcribes, types into sessions, keeps voicemail. Serves the test page |
| Asterisk | `asterisk/*.tmpl` → `~/.claude-phone/etc/asterisk/` | The phone exchange. The iPhone registers to it as extension `201`; every dialled number goes to the daemon's Stasis app |
| Speech out | `claude_phone/tts.py` | ElevenLabs (main voice → free fallback voice → Mac `say`), cached as 16 kHz WAV in `~/.claude-phone/sounds/tts/` |
| Speech in | `claude_phone/stt.py` + `whisper-server` | whisper.cpp large-v3-turbo on the Mac, prompted with live repo names; silence and noise transcripts are dropped |
| Hooks | `claude_phone/hooks.py` | Claude Code hooks register each session (repo, branch, iTerm2 tab) and track working/idle |
| Registry | `claude_phone/registry.py` | SQLite (`~/.claude-phone/registry.db`): sessions and their extensions (301-399), voicemail, an audit log of every injection |
| Injection | `claude_phone/inject.py` | AppleScript types text into the session's iTerm2 tab and presses Return, so answers arrive as real user turns |
| Operator | `claude_phone/operator.py` | Dial 0: matches spoken requests ("the backend") to a session |
| Push | `claude_phone/push.py` | Optional ntfy notification when a call is missed |
| Test page | `claude_phone/web/call.html` | `http://127.0.0.1:<daemon_port>/`: one button that calls you |

Services run as launchd user agents (`com.claude-phone.{asterisk,whisper,daemon}`), restarted if they crash and
started at login.

## Claude calls you (`call_<owner>`)

1. The session calls the MCP tool with one spoken question. The MCP server adds the session id, the iTerm2 tab
   and the working directory, and POSTs it to the daemon (`/call`).
2. The daemon starts rendering the speech (ElevenLabs) while it asks Asterisk, over ARI, to ring `PJSIP/201`
   with the caller name `Claude`.
3. You pick up. After `answer_delay` seconds the question plays, then `end_pause` seconds, then a beep.
4. Asterisk records your answer until you press `#`, hang up, or `record_seconds` pass.
5. whisper-server transcribes it on the Mac. The text goes back to the tool call as its result:
   `Sam answered by phone: "…"`.
6. No answer, busy, iPhone offline, or nothing said: the question becomes a **voicemail** and the tool
   returns at once so Claude carries on. With ntfy set up, your phone gets a notification.

The whole call fits inside Claude Code's 120-second MCP limit: ring 25 s + answer 60 s + transcription < 1 s.

`notify_<owner>` is the same without the recording: ring, speak, hang up. `transfer_call` rings you, explains,
then connects you to another session as in "You call a session".

## You call a session

1. Dial its extension (`phone list`; 301-399). Asterisk hands the call to the daemon.
2. If the session is mid-task (its tab shows Claude Code's spinner), you hear hold music until its turn ends,
   or press `#` to leave your message now.
3. Speak after the beep, then hang up. The transcript is typed into that tab and submitted.

Dial **0** to ask the operator by name, **9** to send one message to every session, **500** for voicemail
(answers are typed into the sessions that asked), **600** for an echo test.

## Voices

`tts.render()` respells words from `[pronunciations]`, hashes the text, voice, model and settings, and
reuses the cached audio if it exists. Otherwise it asks ElevenLabs for 16 kHz PCM:

1. the main voice (`elevenlabs_voice_id`, default Raed, Saudi);
2. if ElevenLabs refuses it (402/403: Voice Library voices need a paid plan), the free fallback voice
   (`elevenlabs_fallback_voice_id`, default Brian);
3. if ElevenLabs fails entirely (no key, no credit, offline), the Mac's own voice (`say_voice`).

A call is never silent. `eleven_v3` acts out audio tags like `[happy]`; for other models tags are stripped.
System phrases ("وصل", "الخط مشغول", the operator…) come in Saudi Arabic or English, chosen by `language`,
and are pre-rendered in the background when the daemon starts.

## Language

`language = "ar"` (default) does three things: system phrases are Saudi dialect, whisper listens for
Arabic, and the MCP tool descriptions tell Claude to speak Saudi Arabic. `language = "en"` switches all three.
`whisper_language` can override recognition alone (for example `auto`).

## Security model

- **Secrets:** the ElevenLabs key lives in `.env` (git-ignored) or the environment. The SIP password, the
  internal ARI password and the ntfy topic are generated into the macOS keychain (service `claude-phone`).
  Nothing secret is written to the repo.
- **Network:** Asterisk listens on the Mac's LAN address and only accepts phones from that subnet (`lan_cidr`).
  The daemon and test page listen on 127.0.0.1 only.
- **Browsers:** every POST to the daemon must be `application/json`. A website can't send that to another
  origin without a CORS preflight, which the daemon never answers, so pages you visit can't ring your phone.
- **Data:** recordings, transcripts and the injection log stay in `~/.claude-phone/`. Old recordings and unused
  speech are expired hourly; the injection audit log is kept.

## Files on disk

| Path | What |
|---|---|
| `~/.claude-phone/config.toml` | your settings |
| `~/.claude-phone/registry.db` | sessions, voicemail, injection log |
| `~/.claude-phone/logs/phone.log` | calls, transcripts, injections: the log to tail |
| `~/.claude-phone/logs/{asterisk,whisper,daemon}.out` | service output |
| `~/.claude-phone/etc/asterisk/` | generated Asterisk config (`phone config` re-renders it) |
| `~/.claude-phone/asterisk/` | the Asterisk build |
| `~/.claude-phone/models/` | the whisper model (~550 MB) |
| `~/.claude-phone/sounds/` | cached speech, hold music, beep |

Build details and the reason behind each workaround: [build-notes.md](build-notes.md).
