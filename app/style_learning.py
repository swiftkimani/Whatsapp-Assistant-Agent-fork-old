"""Learn how the owner texts from the owner's own WhatsApp history.

data/style_examples.md is the only thing in the whole prompt that teaches the
assistant its owner's voice, and it shipped full of placeholder lines with a
header asking for them to be replaced. Nobody replaced them, so the assistant
has been imitating a demo fixture. This module reads the owner's own recent
conversations through the bridge, turns them into (their message -> your reply)
pairs, and rewrites both that file and the pairs file app.style_retrieval reads.

Privacy: this reads the owner's own account on the owner's own server, which is
the premise of the whole project — but a pair contains words somebody else
wrote. So nothing is kept beyond what the prompt and the retrieval actually
use, the ceilings below bound how much is ever held at once, and no message
text, name or number reaches a log line. Counts, flags and a hashed chat id
only, which is the rule app.log sets out.
"""
import json
import os
import time
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import bridge, llm
from app.dashboard import require_auth
from app.export_parser import build_pairs
from app.log import anon, log_event

router = APIRouter()
AUTH = [Depends(require_auth)]

# Read by app.style_retrieval. Spelled out here as well as there on purpose:
# that module is imported on the reply path of every message and must not pull
# in FastAPI, and it is stubbed for the whole test run, so neither module can
# import the constant from the other.
PAIRS_FILE = Path(__file__).resolve().parent.parent / "data" / "style_pairs.json"

# The owner's timezone, so the date in the file header is the date they had.
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Africa/Nairobi"))

# Ceilings. The bridge drives one real WhatsApp Web session, so every thread
# read is a browser round trip: a walk is slow, and walking a 310-conversation
# account end to end is also the kind of traffic WhatsApp itself notices.
# Twenty-five conversations at fifty messages each is a couple of thousand
# messages and a few hundred pairs — already more voice than the prompt can
# use — and they are the bridge's own page sizes (DEFAULT_CHATS,
# DEFAULT_MESSAGES in wa_read.js), so nothing here asks for more than the
# dashboard's WhatsApp pane already asks for on every open.
DEFAULT_MAX_CHATS = 25
DEFAULT_MAX_MESSAGES_PER_CHAT = 50      # the bridge hard-caps /thread at 100
# Every line written to the examples file is in the system prompt of every
# single message, so this is a cost paid per reply rather than once. Voice
# comes from variety across people, not from volume.
MAX_EXAMPLES = 40
# Groups and chats the owner has left are dropped after the read, so ask for
# more conversations than are wanted: on an account whose most recent activity
# is all group traffic, asking for exactly 25 would learn from almost nothing.
# One call either way — the bridge reads every chat out of the page regardless,
# and the limit only decides how many are shaped and sent on.
CHAT_OVERREAD = 3
BRIDGE_CHAT_CEILING = 600               # the bridge's own cap on /chats

# build_pairs tells the owner's messages from the other person's by name,
# because a pasted export carries nothing else. A thread from the bridge
# carries a from_me flag instead, so the two sides are handed over under these
# sentinels. They never meet a real contact name: both sides are built here.
_OWNER = "me"
_THEM = "them"

# The demo lines data/style_examples.md has carried since the project was set
# up. All of data/ is gitignored, so this list is the only record of them left,
# and it is what lets "has this been filled in yet" be answered by comparing
# the file rather than guessing from its length.
SHIPPED_PLACEHOLDERS = frozenset({
    "niaje, uko aje",
    "sawa nitakuambia baadaye",
    "wacha nicheki kwanza",
    "niko job saa hii, nitakupigia nikitoka",
    "eeh fiti, tuonane hapo",
    "manze nimechelewa kiasi, traffic ni noma",
    "si uniambie mapema next time",
    "poa, nimeshika",
})

# This module names itself in the header of what it writes, so a second learn
# run can tell its own output from something the owner typed.
LEARNED_MARKER = "Learned from your own WhatsApp"


class StyleLearningError(Exception):
    """Carries a sentence fit to show the owner, and the status to show it with."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------- reading WhatsApp ----

async def _read(path: str, payload: dict, timeout: float) -> dict:
    """One read through the bridge, with every failure turned into a sentence."""
    try:
        response = await bridge.post(path, payload, timeout)
    except httpx.HTTPError as err:
        raise StyleLearningError(bridge.UNREACHABLE) from err
    if response.status_code != 200:
        raise StyleLearningError(
            f"WhatsApp could not be read ({response.status_code}). Is the bridge still linked?")
    try:
        return response.json()
    except ValueError as err:
        raise StyleLearningError("The bridge answered with something this could not read.") from err


def own_conversations(chats: list[dict], max_chats: int) -> list[str]:
    """The chats worth learning from, in the order the bridge already sorted them.

    Groups are left out. The assistant never answers one, and in a group the
    message before yours was as likely to be addressed to somebody else, so
    pairing them would teach replies to conversations the owner was not in.
    Chats they have left are out for the same reason, plus they claim a recent
    timestamp they have not earned. The bridge returns pinned chats first, then
    live ones newest first, then archived — so taking from the front takes the
    conversations the owner is actually having.
    """
    wanted = []
    for chat in chats:
        chat_id = chat.get("id") or ""
        if not chat_id or chat.get("is_group") or chat.get("left"):
            continue
        wanted.append(chat_id)
        if len(wanted) == max_chats:
            break
    return wanted


def pairs_from_thread(messages: list[dict]) -> list[dict]:
    """(their message -> your reply) pairs out of one conversation.

    Media, system labels and forwards go before build_pairs sees them: an
    export spells a photo out as "<Media omitted>", which build_pairs skips by
    its text, but the bridge hands a photo over as an empty body with a `kind`
    label, which no text filter can catch. A forwarded message is somebody
    else's words, so it would teach somebody else's voice.

    Everything else is build_pairs' own judgement, left in the one place it is
    already written and tested: links are skipped, long replies and long
    incoming messages are dropped, duplicates are dropped, and any reply
    containing digits is dropped — times, prices and numbers go stale, and the
    model would copy them back out as fact.
    """
    shaped = []
    for message in messages:
        body = (message.get("body") or "").strip()
        if not body or message.get("has_media") or message.get("kind") or message.get("forwarded"):
            continue
        shaped.append((_OWNER if message.get("from_me") else _THEM, body))
    return build_pairs(shaped, owner=_OWNER)


# ------------------------------------------------------------- the files ----

def parse_lines(text: str) -> list[str]:
    """The example lines of a style file, by the same rule app.llm reads it with:
    one message per line starting with "-", and nothing else in the file read."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-"):
            example = line.lstrip("-").strip()
            if example:
                lines.append(example)
    return lines


def is_placeholder(text: str) -> bool:
    """True when this file still holds only the lines that shipped with the project."""
    lines = parse_lines(text)
    return bool(lines) and set(lines) <= SHIPPED_PLACEHOLDERS


def is_hand_written(text: str) -> bool:
    """True when the file holds lines the owner put there, which must never be lost.

    Three honest cases rather than a guess: an empty file has nothing to lose,
    our own output names itself in its header, and a file holding only shipped
    placeholders was never filled in. Anything else is his, and is copied aside
    before it is replaced. Only "-" lines are weighed, because the file's own
    contract is that only those are ever read.
    """
    return bool(text.strip()) and LEARNED_MARKER not in text and not is_placeholder(text)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""       # not written yet, which is the normal first state


def _write_atomically(path: Path, text: str) -> None:
    """Write a temp file beside the target, then rename over it.

    Both of these files live on a Docker volume that app.llm re-reads on every
    message, so a half-written one would be read as the owner's voice mid-write.
    os.replace is atomic within a filesystem; the pid keeps two writers apart.
    The same pattern as app.contacts.add.
    """
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _header(when: str) -> str:
    return (
        "# How I text\n"
        "\n"
        f"{LEARNED_MARKER} on {when}.\n"
        "\n"
        "These are messages you actually sent, taken from your most recent\n"
        "one-to-one conversations. Run the learn step again whenever this stops\n"
        "sounding like you.\n"
        "\n"
        "You can edit these by hand. A file you have edited is copied aside before a\n"
        "later learn run replaces it, so nothing you write here is lost.\n"
        "\n"
        'Keep one message per line, starting with "-". Nothing else in this file is read.\n'
        "\n"
    )


def pick_examples(per_chat: list[list[dict]], limit: int) -> list[str]:
    """The replies to put in the prompt: newest first, one conversation at a time.

    Round-robin rather than in bulk because forty lines out of the one chat the
    owner is in all day teaches one register, and the voice the assistant needs
    is the one he uses with everybody. Newest first because that is the voice
    he has now.
    """
    picked: list[str] = []
    seen: set[str] = set()
    for across_chats in zip_longest(*(reversed(chat_pairs) for chat_pairs in per_chat)):
        for pair in across_chats:
            reply = (pair or {}).get("reply", "").strip()
            if not reply or reply.casefold() in seen:
                continue
            seen.add(reply.casefold())
            picked.append(reply)
            if len(picked) == limit:
                return picked
    return picked


def _write_examples(lines: list[str], now: float) -> Path | None:
    """Rewrite the style file, returning the backup it had to make, if any."""
    path = llm.STYLE_EXAMPLES_PATH      # read per call: mounted in Docker, replaced in tests
    current = _read_text(path)
    backup = None
    if is_hand_written(current):
        backup = path.with_name(f"{path.stem}.backup-{int(now)}{path.suffix}")
        _write_atomically(backup, current)
    when = datetime.fromtimestamp(now, LOCAL_TZ).strftime("%d %b %Y, %H:%M")
    _write_atomically(path, _header(when) + "".join(f"- {line}\n" for line in lines))
    return backup


def _stored_pairs() -> tuple[int, float | None]:
    """How many pairs are stored for retrieval, and when they were learned."""
    try:
        stored = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
        return len(stored["pairs"]), float(stored["learned_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0, None      # never learned, or the file is not ours to read


# ----------------------------------------------------------- the two jobs ----

async def learn_style(
    max_chats: int = DEFAULT_MAX_CHATS,
    max_messages_per_chat: int = DEFAULT_MAX_MESSAGES_PER_CHAT,
    now: float | None = None,
) -> dict:
    """Walk the owner's recent conversations and rewrite their voice from them.

    Raises StyleLearningError, whose message is fit to show the owner.
    """
    now = time.time() if now is None else now
    listing = await _read("/chats", {"limit": min(max_chats * CHAT_OVERREAD, BRIDGE_CHAT_CEILING)}, timeout=60)
    wanted = own_conversations(listing.get("chats") or [], max_chats)
    if not wanted:
        raise StyleLearningError(
            "WhatsApp came back with no one-to-one conversations, so there was nothing to learn from.", 422)

    per_chat: list[list[dict]] = []
    chats_read = 0
    for chat_id in wanted:
        # One at a time on purpose: these are browser round trips through a
        # single WhatsApp Web session, and firing them off together is both
        # slower in practice and the pattern WhatsApp notices.
        try:
            thread = await _read("/thread", {"chat_id": chat_id, "limit": max_messages_per_chat}, timeout=90)
        except StyleLearningError:
            # One conversation that has since been deleted must not cost the
            # whole walk. If every one of them fails, the check below says so.
            log_event("style_learn_chat_skipped", level="warning", chat=anon(chat_id))
            continue
        chats_read += 1
        found = pairs_from_thread(thread.get("messages") or [])
        if found:
            per_chat.append(found)

    seen, pairs = set(), []
    for pair in (p for chat_pairs in per_chat for p in chat_pairs):
        key = (pair["incoming"], pair["reply"])
        if key not in seen:        # build_pairs dedupes within a chat, not across them
            seen.add(key)
            pairs.append(pair)
    if not pairs:
        raise StyleLearningError(
            "Nothing in those conversations looked like a reply in your own words. "
            "Try again asking for more messages per chat.", 422)

    lines = pick_examples(per_chat, MAX_EXAMPLES)
    _write_atomically(PAIRS_FILE, json.dumps({"learned_at": now, "pairs": pairs}, ensure_ascii=False) + "\n")
    backup = _write_examples(lines, now)
    log_event("style_learned", chats_read=chats_read, pairs=len(pairs),
              lines=len(lines), backed_up=bool(backup))    # counts only, never text
    return {"pairs": len(pairs), "lines": len(lines), "chats_read": chats_read,
            "backed_up": backup.name if backup else ""}


def style_status() -> dict:
    text = _read_text(llm.STYLE_EXAMPLES_PATH)
    pairs, learned_at = _stored_pairs()
    return {
        "is_placeholder": is_placeholder(text),
        "lines": len(parse_lines(text)),
        "pairs": pairs,
        "learned_at": learned_at,
    }


# -------------------------------------------------------------- the routes ----

class LearnRequest(BaseModel):
    max_chats: int = Field(default=DEFAULT_MAX_CHATS, ge=1, le=100)
    max_messages_per_chat: int = Field(default=DEFAULT_MAX_MESSAGES_PER_CHAT, ge=5, le=100)


@router.post("/api/style/learn", dependencies=AUTH)
async def learn(body: LearnRequest | None = None):
    """Read the owner's own recent WhatsApp and rewrite the style examples from it."""
    asked = body or LearnRequest()
    try:
        return await learn_style(asked.max_chats, asked.max_messages_per_chat)
    except StyleLearningError as err:
        raise HTTPException(err.status, str(err)) from err


@router.get("/api/style/status", dependencies=AUTH)
def status():
    """Whether the assistant is still learning the owner's voice from a demo fixture."""
    return style_status()
