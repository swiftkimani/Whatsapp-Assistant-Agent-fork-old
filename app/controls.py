"""Owner controls: auto-reply kill switch, per-contact modes, activity feed."""
import os
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import contacts, db
from app.dashboard import require_auth
from app.log import log_event

router = APIRouter()
AUTH = [Depends(require_auth)]
_NAMED_KINDS = {"auto_reply", "held", "held_sent"}  # audit rows whose target is a chat id
# The owner's timezone. TZ overrides it, which is how the containers are set.
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Africa/Nairobi"))
RANGE_LABELS = {"today": "today", "yesterday": "yesterday", "last_7_days": "in the last 7 days", "all": "in the stored history"}


def time_bounds(label: str, now: datetime | None = None) -> tuple[float | None, float | None]:
    """(since, until) as timestamps, using local midnight as the start of a day."""
    now = now or datetime.now(LOCAL_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "today":
        return midnight.timestamp(), None
    if label == "yesterday":
        return (midnight - timedelta(days=1)).timestamp(), midnight.timestamp()
    if label == "last_7_days":
        return (midnight - timedelta(days=6)).timestamp(), None
    return None, None


def readable_who(chat_id: str, stored: str, book: dict[str, str]) -> str:
    """Who this was with, in words the owner recognises.

    The fallback used to be the chat id's local part. For a phone number that
    is merely unfriendly; for a privacy id it is fifteen opaque digits that
    match nothing the owner has ever seen, and for a group it is the group id.
    """
    if stored:
        return stored
    name = contacts.name_for(chat_id, book=book)
    if name:
        return name
    if not chat_id:
        return "From this dashboard"
    if chat_id.endswith("@g.us"):
        return "A group"
    if chat_id.endswith("@lid"):
        return "Unknown contact"
    digits = chat_id.split("@")[0].split(":")[0]
    return f"+{digits}" if digits.isdigit() else "Unknown contact"


def find_messages(contact: str | None, period: str, direction: str, limit: int = 100) -> list[dict]:
    """Plain database query: no model involved, so listing messages costs no tokens."""
    since, until = time_bounds(period)
    rows = db.query_messages(contact, since, until, {"received": "in", "sent": "out"}.get(direction), limit)
    book = contacts.load()      # read once for the whole page, not per row
    return [{"ts": r["ts"], "direction": r["direction"], "body": r["body"], "outcome": r["outcome"],
             "chat_id": r["chat_id"] or "",
             "name": readable_who(r["chat_id"] or "", r["name"] or "", book)} for r in rows]


class ToggleRequest(BaseModel):
    enabled: bool


class ModeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    mode: Literal["auto", "hold", "ignore"]


class DefaultModeRequest(BaseModel):
    mode: Literal["auto", "hold", "ignore"]


# How many people one call may carry. The dashboard sends whichever names are
# selected, and "select all" on an unfiltered list is the whole address book,
# so the ceiling has to clear several hundred to be worth having. Past that it
# is a script or a mistake rather than a person choosing contacts, and it keeps
# the work per request bounded: one lookup each against a single read of
# contacts.json, then one transaction.
MAX_BULK_NAMES = 500


class ModesRequest(BaseModel):
    names: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(min_length=1, max_length=MAX_BULK_NAMES)
    mode: Literal["auto", "hold", "ignore"]


def _status() -> dict:
    return {"auto_reply": db.auto_reply_enabled(), "direct_send": db.direct_send_enabled(),
            "self_log": db.self_log_enabled(), "held": db.pending_count()}


@router.get("/api/status", dependencies=AUTH)
def status():
    return _status()


@router.post("/api/auto-reply", dependencies=AUTH)
def set_auto_reply(body: ToggleRequest):
    db.set_setting("auto_reply", "1" if body.enabled else "0")
    db.log("auto_reply_toggled", "on" if body.enabled else "off")
    log_event("auto_reply_toggled", enabled=body.enabled)
    return _status()


@router.post("/api/self-log", dependencies=AUTH)
def set_self_log(body: ToggleRequest):
    db.set_setting("self_log", "1" if body.enabled else "0")
    db.log("self_log_toggled", "on" if body.enabled else "off")
    log_event("self_log_toggled", enabled=body.enabled)
    return _status()


@router.post("/api/direct-send", dependencies=AUTH)
def set_direct_send(body: ToggleRequest):
    db.set_setting("direct_send", "1" if body.enabled else "0")
    db.log("direct_send_toggled", "on" if body.enabled else "off")
    log_event("direct_send_toggled", enabled=body.enabled)
    return _status()


@router.get("/api/messages", dependencies=AUTH)
def messages(contact: str = "", period: Literal["today", "yesterday", "last_7_days", "all"] = Query("today", alias="range"),
             direction: Literal["received", "sent", "both"] = "both", limit: int = 100):
    name = None
    if contact:
        name = contacts.canonical(contact)
        if not name:
            raise HTTPException(400, "Unknown contact.")
    return {"items": find_messages(name, period, direction, limit)}


@router.get("/api/contact-modes", dependencies=AUTH)
def list_modes():
    modes, fallback = db.all_modes(), db.default_mode()
    people = [{"name": n, "mode": modes.get(n.lower(), fallback)} for n in contacts.names()]
    return {"contacts": people, "default": fallback,
            "auto_count": sum(1 for p in people if p["mode"] == "auto")}


@router.post("/api/default-mode", dependencies=AUTH)
def change_default_mode(body: DefaultModeRequest):
    """What a contact you have not decided about yet gets. Changing this moves
    everyone you have not set individually, so the count comes back with it."""
    db.set_default_mode(body.mode)
    db.log("default_mode_changed", body.mode)
    log_event("default_mode_changed", mode=body.mode)
    return list_modes()


@router.post("/api/contact-modes", dependencies=AUTH)
def change_mode(body: ModeRequest):
    canonical = contacts.canonical(body.name)
    if not canonical:
        raise HTTPException(400, "Unknown contact. Add the name to contacts.json.")
    db.set_mode(canonical, body.mode)
    db.log("mode_changed", f"{canonical}: {body.mode}")
    log_event("mode_changed", mode=body.mode)
    return {"name": canonical, "mode": body.mode}


def resolve_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Split the names asked for into the ones contacts.json knows and the rest.

    contacts.canonical() is the rule — match ignoring case and surrounding
    space, return the name exactly as stored — but it re-reads the file on
    every call, and this route may be handed hundreds of names. So the book is
    read once and that rule applied to it, the way readable_who() takes a book
    for a whole page rather than one row.

    Both lists keep the order they were sent in and hold no duplicates, so the
    count of names written matches the length of the first one.
    """
    book = {stored.strip().lower(): stored for stored in contacts.load()}
    known: list[str] = []
    unknown: list[str] = []
    for name in names:
        stored = book.get(name.strip().lower())
        if stored is None:
            if name not in unknown:
                unknown.append(name)
        elif stored not in known:
            known.append(stored)
    return known, unknown


@router.post("/api/contacts/modes", dependencies=AUTH)
def change_modes(body: ModesRequest):
    """Make one choice for many people at once.

    One person per request meant an address book of hundreds took hundreds of
    requests to set up. A name that matches nobody comes back in "unknown"
    instead of being stored: a mode row is keyed on the name, so a typo would
    write a row no chat ever matches and quietly do nothing, and nobody reads
    twenty confirmations to notice. "changed" is what was really written.
    """
    known, unknown = resolve_names(body.names)
    changed = db.set_modes(known, body.mode) if known else 0

    if changed:
        # One summary row rather than one per person: a few hundred rows would
        # push everything else out of the activity feed. The kind is
        # "mode_changed" because the feed already reads that kind as
        # "<target>: <mode in words>", so this arrives as "7 people: Reply
        # automatically" instead of a key only this file was meant to see.
        db.log("mode_changed", f"{changed} {'person' if changed == 1 else 'people'}: {body.mode}")
    # "auto" is the one that answers people with no approval of yours, so doing
    # it to a selection is the change worth finding in the logs afterwards.
    # Counts only: names and numbers stay out of them.
    log_event("modes_changed", level="warning" if body.mode == "auto" else "info",
              mode=body.mode, changed=changed, unknown=len(unknown))
    return {"changed": changed, "mode": body.mode, "unknown": unknown}


@router.get("/api/audit", dependencies=AUTH)
def audit(limit: int = 50):
    limit = max(1, min(limit, 200))
    items = []
    book = contacts.load()
    for r in db.recent_audit(limit):
        target = r["target"] or ""
        if r["kind"] in _NAMED_KINDS and target:
            # A chat id. Resolve it, and never print the raw id as a fallback.
            name = contacts.name_for(target, book=book) or readable_who(target, "", book)
            items.append({"ts": r["ts"], "kind": r["kind"], "target": "", "name": name})
        else:
            # Not a chat id — a mode, a toggle state, a name. Safe as written.
            items.append({"ts": r["ts"], "kind": r["kind"], "target": target, "name": None})
    return {"items": items}
