"""Approval queue for held replies.

The browser only ever sends an item id (and optionally edited text). The chat
id comes from the database, so this page can't be used to message arbitrary
numbers.
"""
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import contacts, db
from app.controls import readable_who
from app.log import log_event
from app.dashboard import require_auth
from app.memory import add_assistant_message

router = APIRouter()
AUTH = [Depends(require_auth)]


class SendRequest(BaseModel):
    text: str | None = Field(default=None, max_length=4000)


GROUPED_LIMIT = 200     # enough held drafts to cover a backlog the owner has let build up
INCOMING_PREVIEW = 160  # characters of the message being answered that fit a card
WHY_PREVIEW = 120       # the model's reason is one line; anything longer is an essay


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _shorten(text: str, limit: int) -> str:
    """`text` cut to about `limit` characters on a word boundary, with an ellipsis.

    Python indexes a str by code point, so Kiswahili, Sheng and Arabic come
    through the cut whole; slicing the bytes would halve a character and leave
    the card showing a replacement glyph. Text with no whitespace in the first
    half of the budget — a long URL, a script that does not space its words —
    is cut where the limit falls, because the alternative is either the whole
    thing or almost none of it.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    boundary = max((i for i, ch in enumerate(head) if ch.isspace()), default=-1)
    if boundary >= limit // 2:
        head = head[:boundary].rstrip()
    return f"{head}…"


def _reply_key(reply: str) -> str:
    """What makes two drafts the same answer: the words, however they are spaced
    or capitalised. A burst of three messages draws three drafts that differ by
    a line break or a capital letter and say one thing."""
    return " ".join((reply or "").split()).casefold()


def _who(row: dict, name: str, book: dict[str, str]) -> str:
    """Who this draft is from, in words the owner recognises.

    readable_who() resolves a chat id on its own, so an "@lid" privacy id read
    "Unknown contact" even though the bridge had resolved the phone number and
    the row has been carrying it all along. The ladder, best first:

      1. contacts.json, matched with the number as well as the chat id.
      2. the sender's own WhatsApp display name. Ranked below the address book
         deliberately: it is self-chosen, a stranger's claim about who they
         are, and a name the owner saved himself must always win over it.
      3. "+digits" — not a name, but a number he can dial back, which beats
         being told the conversation is with nobody in particular.
      4. readable_who's own wording for a group, for this dashboard, and for a
         chat we really do know nothing about.
    """
    chat_id = row.get("chat_id") or ""
    if name:
        return name
    chosen = (row.get("push_name") or "").strip()
    # In a group the display name belongs to one sender and not to the
    # conversation, so "A group" stays more accurate than whoever wrote last.
    if chosen and not chat_id.endswith("@g.us"):
        return chosen
    digits = _digits(row.get("number") or "")
    if digits:
        return f"+{digits}"
    return readable_who(chat_id, "", book)


def _card(row: dict, name: str, who: str) -> dict:
    """One held draft in the shape the dashboard's card renderer reads.

    The name and the readable fallback are arguments rather than looked up here
    because a caller holding a whole conversation's drafts resolves them once
    for the lot — see held_by_contact.

    `incoming` and `why` come back whole beside their shortened forms: the card
    shows the short one and the owner can open the rest, which a card that had
    thrown the tail away could not offer.
    """
    return {
        "id": row["id"], "chat_id": row["chat_id"] or "", "ts": row["ts"],
        "name": name, "from": who,
        "incoming": row["incoming"], "incoming_short": _shorten(row["incoming"], INCOMING_PREVIEW),
        "reply": row["reply"],
        "reason": row.get("reason") or "", "why": row.get("why") or "",
        "why_short": _shorten(row.get("why") or "", WHY_PREVIEW),
        # One row, standing for itself, until _collapse_duplicates says otherwise.
        "repeats": 1, "duplicate_ids": [],
    }


def _collapse_duplicates(cards: list[dict]) -> list[dict]:
    """One answer, one card.

    People text in bursts, and three messages about one thing draw three drafts
    that say nearly the same words. Reading the same sentence three times tells
    the owner nothing, and sending it three times would be worse.

    Drafts are compared on their reply text alone (whitespace collapsed,
    casefolded): two drafts answering two different messages with the same
    words are still one answer to send. The comparison covers the whole
    conversation rather than only drafts next to each other, because an odd
    draft in between does not make two identical ones different.

    The newest of each set stays — the drafts arrive newest first, so that is
    the first one seen — and carries `repeats` and `duplicate_ids`. Those rows
    are still pending in the database, so sending or dismissing the card that
    stands for them resolves them too; see send_held and dismiss_held.
    """
    kept: dict[str, dict] = {}
    cards_out = []
    for card in cards:
        key = _reply_key(card["reply"])
        first = kept.get(key)
        if first is None:
            kept[key] = card
            cards_out.append(card)
            continue
        first["repeats"] += 1
        first["duplicate_ids"].append(card["id"])
    return cards_out


def _duplicates_of(item: dict) -> list[int]:
    """The other waiting drafts in this conversation that say the same thing.

    Recomputed from the database rather than taken from the request: the
    browser is only ever trusted with an item id (see this module's docstring),
    and the queue may have moved since the card was drawn. The comparison uses
    the draft as stored, not the text the owner may have edited, because that
    is what the card stood for when it was collapsed.
    """
    key = _reply_key(item.get("reply"))
    return [r["id"] for r in db.pending_in_chat(item.get("chat_id") or "")
            if r["id"] != item.get("id") and _reply_key(r["reply"]) == key]


def _dismiss_duplicates(item_ids: list[int]) -> int:
    """Clear the rows a kept card stood for, and say how many moved.

    db.transition() one row at a time rather than one UPDATE over the lot: it
    is the same compare-and-set the owner's own clicks go through, so a draft
    they happened to handle in another tab is left alone instead of being
    overwritten.

    No audit line each. They are not separate decisions — they are the one
    decision the feed has already recorded — and a burst of fifty would bury
    everything else in it. The count goes to the structured log instead.
    """
    return sum(1 for item_id in item_ids if db.transition(item_id, "pending", "dismissed"))


@router.get("/api/held", dependencies=AUTH)
def list_held():
    items = []
    book = contacts.load()          # one read for the whole queue
    for r in db.pending():
        chat_id = r["chat_id"] or ""
        name = contacts.name_for(chat_id, r.get("number"), book=book) or ""
        items.append(_card(r, name, _who(r, name, book)))
    return {"items": items}


@router.get("/api/held/by-contact", dependencies=AUTH)
def held_by_contact(limit: int = GROUPED_LIMIT):
    """The same queue as one row per conversation instead of one per draft.

    Fifty cards in a flat list is not a backlog anyone can work through, and it
    is not how the owner thinks about it: they think about Mum, then about the
    landlord. Counts, names and the drafts themselves all come back together so
    opening a person costs no second request.

    Near-identical drafts inside a conversation come back as one card — see
    _collapse_duplicates — so a burst of "are you there?" messages reads as the
    one answer it is.
    """
    book = contacts.load()          # one read for the whole request
    groups: dict[str, dict] = {}
    for r in db.pending(limit):
        chat_id = r["chat_id"] or ""
        # A draft with no chat id belongs to no conversation, and two of them
        # are not evidence of one person, so each stands alone under its own key.
        # Keying them all on "" would build a group the owner cannot read.
        key = chat_id or f"#{r['id']}"
        group = groups.get(key)
        if group is None:
            # Resolved once per conversation and copied onto its drafts below. A
            # lookup per draft re-walks the whole address book for a chat whose
            # answer is already known.
            name = contacts.name_for(chat_id, r.get("number"), book=book) or ""
            group = groups[key] = {"chat_id": chat_id, "name": name,
                                   "from": _who(r, name, book),
                                   "count": 0, "latest_ts": 0.0, "items": []}
        # pending() hands them back id DESC, so appending keeps each group newest first.
        group["items"].append(_card(r, group["name"], group["from"]))
        # Rows, not cards: `count` and `shown` are how far behind the owner is,
        # and collapsing three repetitions into one card does not answer two of
        # them. The cards say how many each stands for, in `repeats`.
        group["count"] += 1
        group["latest_ts"] = max(group["latest_ts"], r["ts"] or 0.0)

    for group in groups.values():
        # Per conversation, never across two of them: the same words waiting for
        # two different people are two answers that both have to go out.
        group["items"] = _collapse_duplicates(group["items"])

    ordered = sorted(groups.values(), key=lambda g: g["latest_ts"], reverse=True)
    shown = sum(g["count"] for g in ordered)
    # total can exceed shown when the backlog is longer than `limit`, which is
    # what lets the page say "50 of 73" rather than quietly losing the rest.
    return {"groups": ordered, "total": db.pending_count(), "shown": shown,
            "contacts": len(ordered)}


@router.post("/api/held/{item_id}/send", dependencies=AUTH)
def send_held(item_id: int, body: SendRequest):
    item = db.get(item_id)
    if not item:
        raise HTTPException(404, "No such item.")
    text = (body.text if body.text is not None else item["reply"]).strip()
    if not text:
        raise HTTPException(400, "The reply is empty.")
    if not db.transition(item_id, "pending", "sending"):
        raise HTTPException(409, "Already handled.")
    # Read once this row has left 'pending', so it can never be found among its
    # own duplicates, and before the send, so the set is the one the card the
    # owner clicked stood for.
    duplicates = _duplicates_of(item)

    # An approved draft is the case that needs the quote most: it goes out
    # long after the message it answers, often after that person has said
    # something else, so without it the reply arrives attached to nothing.
    # ignoreQuoteErrors is left at the bridge's default here rather than being
    # forced: a message that has since been deleted must not cost the owner
    # the reply they just approved.
    payload = {"chat_id": item["chat_id"], "text": text,
               # The bridge refuses a group for anything typed into the
               # dashboard. This is not that: the owner chose this group, read
               # it, and pressed send on one answer. Without the opt-in a group
               # could produce drafts that can never be sent.
               "allow_group": True}
    if item.get("msg_id"):
        payload["quoted_msg_id"] = item["msg_id"]

    try:
        r = httpx.post(
            f"{os.getenv('BRIDGE_API_URL', 'http://127.0.0.1:3001')}/send-reply",
            json=payload,
            headers={"x-bridge-token": os.getenv("BRIDGE_API_TOKEN", "")},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        db.transition(item_id, "sending", "pending")  # nothing was sent: put it back
        raise HTTPException(502, "Couldn't send through the bridge. Is it running?")

    db.transition(item_id, "sending", "sent")
    # One answer, one WhatsApp message: the rows this card stood for are
    # dismissed, never sent. Only now the send has succeeded — a failure above
    # puts this row back and leaves them waiting with it.
    also_dismissed = _dismiss_duplicates(duplicates)
    db.log("held_sent", item["chat_id"])
    db.log_message(item["chat_id"], contacts.name_for(item["chat_id"], item.get("number")), "out", text, "held_sent")
    log_event("held_sent", duplicates=also_dismissed)
    add_assistant_message(item["chat_id"], text)  # keep the conversation memory coherent
    return {"status": "sent", "duplicates_dismissed": also_dismissed}


@router.post("/api/held/{item_id}/dismiss", dependencies=AUTH)
def dismiss_held(item_id: int):
    """Drop a draft, and with it the repetitions of it that the card stood for.

    The status change goes first, so a draft that no longer exists still reads
    as already handled rather than as a different error.
    """
    if not db.transition(item_id, "pending", "dismissed"):
        raise HTTPException(409, "Already handled.")
    also_dismissed = _dismiss_duplicates(_duplicates_of(db.get(item_id) or {}))
    db.log("held_dismissed", str(item_id))
    log_event("held_dismissed", duplicates=also_dismissed)
    return {"status": "dismissed", "duplicates_dismissed": also_dismissed}