"""Approval queue for held replies.

The browser only ever sends an item id (and optionally edited text). The chat
id comes from the database, so this page can't be used to message arbitrary
numbers.
"""
import os

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


def _card(row: dict, name: str, who: str) -> dict:
    """One held draft in the shape the dashboard's card renderer reads.

    The name and the readable fallback are arguments rather than looked up here
    because a caller holding a whole conversation's drafts resolves them once
    for the lot — see held_by_contact.
    """
    return {
        "id": row["id"], "chat_id": row["chat_id"] or "", "ts": row["ts"],
        "name": name, "from": who,
        "incoming": row["incoming"], "reply": row["reply"],
        "reason": row.get("reason") or "", "why": row.get("why") or "",
    }


@router.get("/api/held", dependencies=AUTH)
def list_held():
    items = []
    book = contacts.load()          # one read for the whole queue
    for r in db.pending():
        chat_id = r["chat_id"] or ""
        name = contacts.name_for(chat_id, r.get("number"), book=book)
        items.append(_card(r, name or "", readable_who(chat_id, name or "", book)))
    return {"items": items}


@router.get("/api/held/by-contact", dependencies=AUTH)
def held_by_contact(limit: int = GROUPED_LIMIT):
    """The same queue as one row per conversation instead of one per draft.

    Fifty cards in a flat list is not a backlog anyone can work through, and it
    is not how the owner thinks about it: they think about Mum, then about the
    landlord. Counts, names and the drafts themselves all come back together so
    opening a person costs no second request.
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
                                   "from": readable_who(chat_id, name, book),
                                   "count": 0, "latest_ts": 0.0, "items": []}
        # pending() hands them back id DESC, so appending keeps each group newest first.
        group["items"].append(_card(r, group["name"], group["from"]))
        group["count"] += 1
        group["latest_ts"] = max(group["latest_ts"], r["ts"] or 0.0)

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

    try:
        r = httpx.post(
            f"{os.getenv('BRIDGE_API_URL', 'http://127.0.0.1:3001')}/send-reply",
            json={"chat_id": item["chat_id"], "text": text},
            headers={"x-bridge-token": os.getenv("BRIDGE_API_TOKEN", "")},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        db.transition(item_id, "sending", "pending")  # nothing was sent: put it back
        raise HTTPException(502, "Couldn't send through the bridge. Is it running?")

    db.transition(item_id, "sending", "sent")
    db.log("held_sent", item["chat_id"])
    db.log_message(item["chat_id"], contacts.name_for(item["chat_id"], item.get("number")), "out", text, "held_sent")
    log_event("held_sent")
    add_assistant_message(item["chat_id"], text)  # keep the conversation memory coherent
    return {"status": "sent"}


@router.post("/api/held/{item_id}/dismiss", dependencies=AUTH)
def dismiss_held(item_id: int):
    if not db.transition(item_id, "pending", "dismissed"):
        raise HTTPException(409, "Already handled.")
    db.log("held_dismissed", str(item_id))
    return {"status": "dismissed"}