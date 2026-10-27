# CLAUDE.md snippet

Paste into `~/.claude/CLAUDE.md` (all repos) or a repo's `CLAUDE.md`. Written for `owner_name = "Sam"`,
whose tools are `call_sam` and `notify_sam`: replace `Sam`/`sam` with your name and tool suffix
(`phone doctor` and the tool list in Claude Code show the exact names).

```markdown
## Phoning Sam (`call_sam`, `notify_sam`, `transfer_call`)

- `call_sam` asks one question and waits for a spoken answer. Use it only when you are blocked
  on a decision that is genuinely Sam's: product calls, unsettled trade-offs, or approval for something
  risky or irreversible. Never for things you can work out from the code, docs or sensible defaults.
- `notify_sam` reads a short announcement and hangs up. Use it for news Sam would want now with no
  reply needed: a long job finished, something is ready, something broke.
- `transfer_call` puts Sam through by phone to another live session. Use it when you're asked something
  that belongs to another repo; give a one-sentence spoken `reason` and a `handover` note for the target.
- Speak Saudi Arabic dialect on calls (اللهجة السعودية), casual and warm:
  "خلصت الشغل طال عمرك، تامر على شي ثاني؟". Sam's answer comes back as an Arabic transcript.
- Sam is listening, not reading. One sentence of about 20 words; phrase questions so they can be
  answered out loud ("نعم أو لا", "هذا ولا هذا"). Add `context` only if the question makes no sense
  without it.
- Never read out code, paths, URLs, PR numbers or step-by-step instructions. Put detail in your chat
  reply. Audio tags like [happy] or [thoughtful] make it sound natural.
- If `call_sam` isn't answered, a voicemail is left. Carry on with other work or your best
  reversible assumption; the answer arrives later as a message starting
  `[Phone answer to your earlier question]`. A missed `notify_sam` leaves nothing behind.
- Subagents: pass `from_agent` with a short name for yourself.
```

For English calls (`language = "en"`), drop the Saudi Arabic line.
