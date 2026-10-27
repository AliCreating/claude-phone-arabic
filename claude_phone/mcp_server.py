"""stdio MCP server exposing call_<owner>, notify_<owner> and transfer_call. One instance per Claude Code session; Claude Code passes
CLAUDE_CODE_SESSION_ID and ITERM_SESSION_ID in the env, which identify the calling session."""

from __future__ import annotations

import os

import aiohttp
from mcp.server.mcpserver import MCPServer

from . import config
from .hooks import iterm_unique_id

mcp = MCPServer("claude-phone")


def _body(**kw) -> dict:
    return {
        **kw,
        "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "iterm_id": iterm_unique_id(),
        "cwd": os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
    }


async def _post(path: str, body: dict) -> dict:
    s = config.load()
    async with aiohttp.ClientSession() as http:
        # Ring 25 s + answer 60 s + transcription stays under Claude Code's 120 s auto-background.
        async with http.post(f"{s.daemon_url}{path}", json=body, timeout=aiohttp.ClientTimeout(total=115)) as r:
            return await r.json()


async def call_owner(question: str, context: str | None = None, from_agent: str | None = None) -> str:
    """Ring {owner}'s iPhone, ask one question out loud, and return the spoken answer as text.

    Use only when you are blocked on a decision that is genuinely {owner}'s to make. For news that
    needs no reply, use notify_{slug} instead.

    Keep it short; {owner} is listening, not reading. The question should be one sentence of about 20
    words or fewer, phrased so it can be answered out loud ("yes or no", "A or B"). Never read out
    code, paths, URLs, PR numbers or step-by-step instructions; put detail in your chat reply instead.
    You may add ElevenLabs audio tags such as [happy] or [thoughtful]. If nobody answers, a voicemail is left and the answer arrives later as a
    new message in this session, so carry on with other work.

    Args:
        question: The single question, written to be spoken aloud. One sentence, about 20 words or fewer.
        context: Optional background, only if the question makes no sense without it. About 10 words.
        from_agent: If you are a subagent, a short name for yourself (e.g. "reviewer").
    """
    try:
        res = await _post("/call", _body(question=question, context=context, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). Carry on with your best assumption."
    if res.get("status") == "answered":
        return f'{S.owner} answered by phone: "{res["answer"]}"'
    if res.get("status") == "voicemail":
        return f"{res['message']} (Reason: {res['reason']}.)"
    return res.get("message", "The call failed.")


async def notify_owner(message: str, from_agent: str | None = None) -> str:
    """Ring {owner}'s iPhone and read a short announcement, then hang up. No reply is expected.

    Use for news {owner} would want to hear now without being asked anything: a long job finished,
    something is ready, or something broke. If nobody answers, nothing is left behind, so put
    anything that must not be missed in your chat reply as well.

    Args:
        message: What {owner} needs to know, written to be spoken. One or two short sentences, about 25
            words or fewer. Never read out code, paths, URLs or PR numbers.
        from_agent: If you are a subagent, a short name for yourself (e.g. "reviewer").
    """
    try:
        res = await _post("/notify", _body(message=message, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). The notification was not delivered."
    if res.get("status") == "delivered":
        return f"{S.owner} heard the notification." + (f" ({res['note']}.)" if res.get("note") else "")
    if res.get("status") == "not_delivered":
        return (f"{S.owner} didn't pick up, so the notification was not delivered (reason: {res['reason']}). "
                "No voicemail was left.")
    return res.get("message", "The notification failed.")


async def transfer_call(to: str, reason: str, handover: str | None = None, from_agent: str | None = None) -> str:
    """Put {owner} through by phone to another live Claude Code session.

    Use when {owner} has asked you something that belongs to a different repo or session (for example a
    backend question asked in a frontend session). The phone rings, {owner} hears your reason and is
    connected to the other session; whatever {owner} says is typed into that session with your handover note.

    Args:
        to: The session to reach, as a repo/branch name ("site-be-v2", "the backend") or its extension
            ("303"). If it doesn't match exactly one session, the live sessions are returned so you can retry.
        reason: One short spoken sentence, e.g. "That's a backend question."
        handover: Optional note for the receiving session (not spoken), e.g. what {owner} originally asked.
        from_agent: If you are a subagent, a short name for yourself.
    """
    try:
        res = await _post("/transfer", _body(to=to, reason=reason, handover=handover, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). No transfer was made."
    st = res.get("status")
    if st == "transferred":
        return f'{S.owner} was put through to {res["to"]} (ext {res["ext"]}) and told it: "{res["owner_said"]}"'
    if st == "no_match":
        return res["message"] + " Live sessions: " + "; ".join(res["sessions"])
    if st == "connected_no_message":
        return res["message"]
    return f"The transfer didn't connect (reason: {res.get('reason')}). Tell {S.owner} in chat which session to use."


# Tool names and descriptions carry the owner's name: call_sam, notify_sam for owner_name = "Sam".
S = config.load()
SPEAK = {
    "ar": ("\n\n    Language: write everything that is spoken (question, context, message, reason) in Saudi Arabic "
           "dialect (اللهجة السعودية), casual and warm, e.g. \"خلصت الشغل طال عمرك، تامر على شي ثاني؟\". "
           "{owner} answers in Arabic; the answer you get back is an Arabic transcript."),
    "en": "",
}.get(S.language, "")
for fn, name in ((call_owner, f"call_{S.owner_slug}"), (notify_owner, f"notify_{S.owner_slug}"),
                 (transfer_call, "transfer_call")):
    doc = fn.__doc__.format(owner=S.owner_name, slug=S.owner_slug)
    head, sep, args = doc.partition("\n\n    Args:")
    mcp.add_tool(fn, name=name, description=head + SPEAK.format(owner=S.owner_name) + sep + args)


def run() -> None:
    mcp.run()
