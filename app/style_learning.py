"""Learn how the owner texts from the owner's own WhatsApp history.

data/style_examples.md is the only thing in the whole prompt that teaches the
assistant its owner's voice, and it shipped full of placeholder lines with a
header asking for them to be replaced. Nobody replaced them, so the assistant
has been imitating a demo fixture. This module reads the owner's own recent
conversations through the bridge, turns them into (their message -> your reply)
pairs, and rewrites both that file and the pairs file app.style_retrieval reads.

That walk only happens when the owner asks for it. learn_from_edit() is the
other way in, and it needs nothing asked of him: when he rewrites a held draft
before approving it, what he sent is kept as a pair too — his own words, for
that situation, for that person, at the moment he cared enough to fix it. See
app.approvals.send_held.

Privacy: this reads the owner's own account on the owner's own server, which is
the premise of the whole project — but a pair contains words somebody else
wrote. So nothing is kept beyond what the prompt and the retrieval actually
use, the ceilings below bound how much is ever held at once, and no message
text, name or number reaches a log line. Counts, flags and a hashed chat id
only, which is the rule app.log sets out.
"""
import difflib
import json
import os
import threading
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


def _read_pairs() -> tuple[list[dict], float | None]:
    """The pairs stored for retrieval, and when the walk last rewrote the voice.

    The two are read apart rather than together: a correction appends a pair
    without claiming the style file was rewritten, so a file can hold pairs and
    carry no learned_at at all, and reading them as one would have reported
    "nothing learned" for a file full of the owner's own corrections.

    Anything unreadable — not written yet, half written, not ours — is no
    pairs, never an exception. The same bargain app.style_retrieval makes: the
    voice is a bonus on a reply, not a precondition for one.
    """
    try:
        stored = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
        pairs = [p for p in (stored.get("pairs") or []) if isinstance(p, dict)]
    except (OSError, ValueError, AttributeError, TypeError):
        return [], None
    try:
        return pairs, float(stored["learned_at"])
    except (KeyError, TypeError, ValueError):
        return pairs, None


# --------------------------------- learning from the owner's own corrections ----

# What marks a pair as the owner's own correction rather than something read
# off his history. Pairs from the walk carry no source at all, which is also
# what every pair written before this existed looks like.
CORRECTION = "correction"

# How alike the draft and what he actually sent may be before the difference
# stops being a correction worth keeping. difflib's ratio is the share of
# characters the two have in common, so this is a share rather than a count:
# nine characters changed in a sixty-character reply is a different sentence,
# and the same nine in a four-hundred-character one is a typo fixed. At 0.85 a
# dropped comma, a fixed capital and a corrected spelling all stay out, while
# swapping a word for another one — the smallest thing that is actually a
# choice of his — comes through.
EDIT_SIMILARITY_CEILING = 0.85

# This file is read on every single message, so corrections cannot be allowed
# to grow with the number of drafts he has ever approved. Two hundred is the
# same order as the couple of hundred pairs the walk itself produces, so the
# two together are what the retrieval was already sized for: tens of kilobytes,
# re-read only when the file changes, scored in microseconds. It is also more
# corrections than months of use will reach.
MAX_CORRECTIONS = 200

# Two approvals in the dashboard land as two threads of one process, and the
# append below is a read-modify-write: without this one of the two pairs is
# simply lost, and both threads name their temp file after the same process id,
# so they would also write over each other's half-finished file and rename the
# result into place. One lock covers reading, deciding and writing.
_WRITE_LOCK = threading.Lock()


def _shape(text: str) -> str:
    """What makes two messages the same words: spacing and case do not count."""
    return " ".join((text or "").split()).casefold()


def is_an_edit(draft: str, sent: str) -> bool:
    """Did the owner rewrite this draft, or only press send on it?

    Three cases, and only the last is worth keeping:

      - the same words, however they are spaced or capitalised. Nothing was
        corrected; this is the draft, approved.
      - a nudge: a typo, a dropped comma, a capital letter. The words are still
        the model's own, so there is no choice of his in it to learn from, and
        keeping it would spend a slot in the retrieval handing the model its
        own output back.
      - a real change, up to a complete rewrite, which is the valuable case and
        scores lowest of all.

    autojunk is off: it starts treating common characters as noise at 200
    elements, which would quietly change how a long draft scores against a
    short rewrite of it — the very case this most wants to catch.
    """
    draft, sent = _shape(draft), _shape(sent)
    if not sent or draft == sent:
        return False
    return difflib.SequenceMatcher(None, draft, sent, autojunk=False).ratio() < EDIT_SIMILARITY_CEILING


def correction_pair(incoming: str, draft: str, sent: str) -> dict | None:
    """The pair to keep from one approved send, or None when there is none.

    build_pairs is the filter, rather than a second set of rules written out
    here: it is what the learn walk and a pasted export both already go
    through, and it is where the judgement calls live — a link or a media
    placeholder on either side is not text to learn from, a reply over 120
    characters or an incoming over 300 is not a texting example, and a reply
    with a digit in it is dropped because a time or a price copied back out as
    fact goes stale. An edit has to be dropped by those same rules, or the
    pairs he corrected himself would be the only ones in the file that nobody
    filtered.
    """
    incoming, sent = (incoming or "").strip(), (sent or "").strip()
    if not incoming or not is_an_edit(draft, sent):
        return None
    pairs = build_pairs([(_THEM, incoming), (_OWNER, sent)], owner=_OWNER)
    return {**pairs[0], "source": CORRECTION} if pairs else None


def keep_newest_distinct(corrections: list[dict], limit: int) -> list[dict]:
    """The corrections to keep, oldest first, within the cap.

    Newest first, because the voice he has now is the one to imitate. Most
    distinct, because the retrieval hands the model up to five of these at once
    and five slots filled with one sentence teach one sentence: two corrections
    whose reply is the same words, however spaced or capitalised, are one
    correction, and the newer wins. Dropping the oldest is what is left over
    once that has been done rather than the rule itself, so a cap reached by
    repeating himself costs him none of the distinct things he has said.
    """
    kept, seen = [], set()
    for pair in reversed(corrections):
        shape = _shape(pair.get("reply"))
        if not shape or shape in seen:
            continue
        seen.add(shape)
        kept.append(pair)
        if len(kept) == limit:
            break
    kept.reverse()      # back to oldest first, so the next append is still newest-last
    return kept


def learn_from_edit(incoming: str, draft: str, sent: str) -> bool:
    """Keep what the owner actually sent as a style pair, if he changed the draft.

    Called once a held reply has already gone out, so nothing here may cost him
    that reply: every failure is logged and swallowed, the same bargain
    app.main._copy_to_owner makes for its own best-effort work. The return
    value says whether a pair was stored, which is what the tests read; no
    caller needs it.

    Only the pairs file is written. data/style_examples.md is a whole-file
    rewrite with a date header and a rule about backing up anything he typed
    himself, and appending to it on every send would churn both — the pair
    reaches the prompt through app.style_retrieval instead, from the very next
    message, because that module re-reads this file whenever it changes.
    """
    try:
        pair = correction_pair(incoming, draft, sent)
        if pair is None:
            return False
        with _WRITE_LOCK:
            stored, learned_at = _read_pairs()
            walked = [p for p in stored if p.get("source") != CORRECTION]
            corrections = keep_newest_distinct(
                [p for p in stored if p.get("source") == CORRECTION] + [pair], MAX_CORRECTIONS)
            # learned_at is carried through untouched: it is when the walk last
            # rewrote his voice, and the dashboard prints it beside the number
            # of lines in the style file, which a correction does not touch.
            _write_atomically(PAIRS_FILE, json.dumps(
                {"learned_at": learned_at, "pairs": walked + corrections}, ensure_ascii=False) + "\n")
        log_event("style_correction_learned", corrections=len(corrections))      # counts only, never text
        return True
    except Exception as err:
        # Said out loud, because corrections silently not being kept is the
        # whole feature quietly not happening — but the class only, because the
        # thing that failed to be written is a message somebody else wrote.
        log_event("style_correction_failed", level="warning", error=type(err).__name__)
        return False


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
    # Read and written under the one lock, so an approval landing mid-walk is
    # either in this file or in the next write, never lost between the two. The
    # section is a small read and a rename; it blocks the loop for no longer
    # than the stat() every message already pays.
    with _WRITE_LOCK:
        # A pair he corrected himself outranks anything this walk can find, and
        # the walk cannot find it: the draft he rewrote was never a message in
        # his history. So corrections survive the rewrite, and a walked pair
        # saying the same thing gives way to his own marked copy rather than
        # shadowing it and dropping the count the status reports back to him.
        his = keep_newest_distinct(
            [p for p in _read_pairs()[0] if p.get("source") == CORRECTION], MAX_CORRECTIONS)
        mine = {(p.get("incoming"), p.get("reply")) for p in his}
        kept = [p for p in pairs if (p["incoming"], p["reply"]) not in mine] + his
        _write_atomically(PAIRS_FILE, json.dumps({"learned_at": now, "pairs": kept}, ensure_ascii=False) + "\n")
    pairs = kept
    backup = _write_examples(lines, now)
    log_event("style_learned", chats_read=chats_read, pairs=len(pairs),
              lines=len(lines), backed_up=bool(backup))    # counts only, never text
    return {"pairs": len(pairs), "lines": len(lines), "chats_read": chats_read,
            "backed_up": backup.name if backup else ""}


def style_status() -> dict:
    text = _read_text(llm.STYLE_EXAMPLES_PATH)
    pairs, learned_at = _read_pairs()
    return {
        "is_placeholder": is_placeholder(text),
        "lines": len(parse_lines(text)),
        "pairs": len(pairs),
        # Counted apart from the rest, because it is the one number that says
        # whether the assistant is getting better rather than only set up: a
        # pair here exists because he rewrote a draft, not because something
        # walked his history.
        "corrections": sum(1 for p in pairs if p.get("source") == CORRECTION),
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
