"""Owner-only dashboard: send a message or your GPS location to a named contact.

Put this file and dashboard.html together in app/. Names come from
contacts.json in the project root.

Auth: you type DASHBOARD_PASSWORD (from .env; the old name DASHBOARD_TOKEN
still works) once on the login screen. The server checks it and sets a signed,
HttpOnly, SameSite=Strict cookie. The password itself is never sent to the page
or stored in the browser. Changing it in .env signs every device out.

Env vars are read when a request arrives (not at import time) because main.py
calls load_dotenv() late.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from app import bridge
from app import contacts as contacts_store
from app import llm
from app import db, live, sent_marks
from app.log import log_event
from app.stt import transcribe_bytes
from app.tts import synthesize_bytes

router = APIRouter()

PAGE = Path(__file__).with_name("dashboard.html")

SESSION_COOKIE = "dash_session"
SESSION_SECONDS = 30 * 24 * 3600   # "keep me signed in"
SHORT_SESSION_SECONDS = 12 * 3600  # otherwise: ends when the browser closes, or after 12 h
LOGIN_WINDOW = 300      # seconds
LOGIN_MAX_FAILS = 5     # per IP, per window
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# How long a location reading from the phone stays usable. Long enough to cover
# asking for a pin and confirming it, short enough that nobody is sent a place
# you have already left.
PHONE_FIX_MAX_AGE = 300  # seconds
SYNC_COOLDOWN = 60       # seconds between contact syncs
STREAM_KEEPALIVE = 25    # seconds of quiet before the live stream says it is still there

_last_send: dict[str, float] = {}
_failed_logins: dict[str, list[float]] = {}


# ---------------------------------------------------------------- auth ----

def _client_ip(request: Request) -> str:
    # Behind a tunnel every request comes from localhost, so the real IP is in a
    # header. Those headers can be forged when NOT behind a proxy, so they are
    # only trusted when you opt in with TRUST_PROXY=true in .env.
    if os.getenv("TRUST_PROXY", "false").lower() == "true":
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
        fwd = fwd.split(",")[0].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


def _password() -> str:
    """The plaintext password, for a dashboard only reachable over loopback."""
    return os.getenv("DASHBOARD_PASSWORD") or os.getenv("DASHBOARD_TOKEN", "")


def _password_hash() -> str:
    """An argon2id hash of the password. Required once the dashboard is exposed
    beyond localhost: it is the only form that survives someone reading the
    environment. Generate one with scripts/hash_password.py.

    DASHBOARD_PASSWORD_HASH_B64 holds the same hash, base64-encoded, and wins
    when both are set. An argon2 hash is built out of "$" separators, and
    deployment platforms that interpolate "$NAME" inside environment values
    silently shred it — "$argon2id$v=19$m=..." arrives as "=19=65536,..." and
    then rejects every password that will ever be typed. Base64 contains no
    "$", so it cannot be mangled that way.
    """
    encoded = os.getenv("DASHBOARD_PASSWORD_HASH_B64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            log_event("dashboard_password_hash_b64_unreadable", level="error")
            return ""
    return os.getenv("DASHBOARD_PASSWORD_HASH", "")


def _credential() -> str:
    """Whichever secret is configured. Sessions are signed with it, so changing
    the password — in either form — signs everyone out."""
    return _password_hash() or _password()


def _check_password(candidate: str) -> bool:
    if hash_ := _password_hash():
        try:
            return PasswordHasher().verify(hash_, candidate)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    expected = _password()
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def _session_key() -> bytes:
    return hashlib.sha256(b"dashboard-session:" + _credential().encode()).digest()


def _sign(expires: int) -> str:
    sig = hmac.new(_session_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _cookie_valid(value: str) -> bool:
    try:
        expires_s, sig = value.split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < time.time():
        return False
    good = hmac.new(_session_key(), expires_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def require_auth(request: Request) -> None:
    """FastAPI dependency: use as dependencies=[Depends(require_auth)]."""
    if not _credential():
        raise HTTPException(503, "Set DASHBOARD_PASSWORD_HASH or DASHBOARD_PASSWORD in .env.")
    if not _cookie_valid(request.cookies.get(SESSION_COOKIE, "")):
        raise HTTPException(401, "Please sign in.")


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    remember: bool = True


@router.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response):
    if not _credential():
        raise HTTPException(503, "Set DASHBOARD_PASSWORD_HASH or DASHBOARD_PASSWORD in .env.")

    ip, now = _client_ip(request), time.time()
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_MAX_FAILS:
        _failed_logins[ip] = attempts
        raise HTTPException(429, "Too many wrong attempts. Try again in 5 minutes.")

    if not _check_password(body.password):
        attempts.append(now)
        _failed_logins[ip] = attempts
        raise HTTPException(401, "Wrong password.")

    _failed_logins.pop(ip, None)
    lifetime = SESSION_SECONDS if body.remember else SHORT_SESSION_SECONDS
    response.set_cookie(
        SESSION_COOKIE, _sign(int(now) + lifetime),
        max_age=lifetime if body.remember else None, httponly=True, samesite="strict",
        secure=_is_https(request), path="/",
    )
    return {"status": "ok"}


@router.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/api/session", dependencies=[Depends(require_auth)])
def session():
    return {"status": "ok"}


# ------------------------------------------------------------ contacts ----

class LocationRequest(BaseModel):
    # Deliberately only the recipient. The coordinates are not accepted here —
    # they are read from the fix the phone reported, so a caller holding a
    # session cookie cannot send someone an arbitrary place as you.
    contact: str = Field(min_length=1, max_length=60)


class PhoneFixRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


class TextRequest(BaseModel):
    contact: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=4000)


def _contact_names() -> list[str]:
    return contacts_store.names()


def _phone_fix() -> dict | None:
    """The last location the phone reported, or None when there is no fresh one.

    A stale or unreadable reading is deleted rather than kept, so failing to get
    a new fix can never quietly fall back to somewhere you used to be.
    """
    raw = db.get_setting("phone_location")
    if not raw:
        return None
    try:
        fix = json.loads(raw)
        age = time.time() - float(fix["ts"])
    except (ValueError, TypeError, KeyError):
        db.set_setting("phone_location", "")
        return None
    if age > PHONE_FIX_MAX_AGE:
        db.set_setting("phone_location", "")
        return None
    return fix


async def _bridge_post(path: str, payload: dict, timeout: float) -> httpx.Response:
    """One POST to the bridge's local API. The bridge holds the WhatsApp
    session, so anything that needs WhatsApp itself goes through here."""
    try:
        return await bridge.post(path, payload, timeout)
    except httpx.HTTPError:
        raise HTTPException(502, bridge.UNREACHABLE)


async def _send(kind: str, path: str, payload: dict, cooldown: float) -> dict:
    known = {n.strip().lower() for n in _contact_names()}
    if payload["contact"].strip().lower() not in known:
        raise HTTPException(400, "Unknown contact. Add the name to contacts.json.")
    if time.time() - _last_send.get(kind, 0.0) < cooldown:
        raise HTTPException(429, "Just sent one. Wait a few seconds.")

    r = await _bridge_post(path, payload, timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, f"The bridge could not send it ({r.status_code}): {r.text[:120]}")

    _last_send[kind] = time.time()
    if kind == "text":
        db.log_message("", contacts_store.canonical(payload["contact"]) or payload["contact"], "out", payload["text"], "dashboard")
    log_event("dashboard_send", kind=kind)  # no message text, name or coordinates in logs
    return {"status": "sent"}


# --------------------------------------------------------------- routes ----

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    # The page itself contains no secrets; every /api/* call needs the cookie.
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/api/contacts", dependencies=[Depends(require_auth)])
async def contacts():
    return {"contacts": _contact_names()}


class NewContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    number: str = Field(min_length=1, max_length=40)


@router.post("/api/contacts", dependencies=[Depends(require_auth)])
async def add_contact(body: NewContactRequest):
    """Put one person on the list by hand.

    Until this existed, the only way in was a full sync of the phone's address
    book, so someone who had written to you but was never saved on the phone
    could not be given a setting of their own at all. Adding only ever adds,
    exactly as the sync does.
    """
    try:
        name = contacts_store.add(body.name, body.number)
    except ValueError as err:
        raise HTTPException(400, str(err))
    log_event("contact_added")   # no name or number in logs
    return {"name": name, "contacts": _contact_names()}


@router.post("/api/contacts/sync", dependencies=[Depends(require_auth)])
async def sync_contacts():
    """Copy the phone's own address book into contacts.json.

    Only the bridge can read it — it is the process holding the WhatsApp
    session — and it only ever adds: a name already in the file is never
    renamed, repointed or removed. Widening the file widens who the dashboard
    may message, so this happens when you ask for it, never on a timer.
    """
    if time.time() - _last_send.get("sync", 0.0) < SYNC_COOLDOWN:
        raise HTTPException(429, "Just synced. Give it a minute.")
    # Reading the address book drives the browser session, which takes
    # noticeably longer than sending one message.
    r = await _bridge_post("/sync-contacts", {}, timeout=120)
    if r.status_code != 200:
        raise HTTPException(502, f"The bridge could not read your contacts ({r.status_code}): {r.text[:120]}")

    _last_send["sync"] = time.time()
    data = r.json()
    # Counts only: the names belong in the file and on your screen, not in the
    # host's logs.
    log_event("contacts_synced", added=data.get("added"), total=data.get("total"))
    return data


@router.post("/api/send-text", dependencies=[Depends(require_auth)])
async def send_text(body: TextRequest):
    return await _send("text", "/send-text", body.model_dump(), cooldown=3)


@router.post("/api/phone-location", dependencies=[Depends(require_auth)])
async def phone_location(body: PhoneFixRequest):
    """Record where the phone says it is. The timestamp is set here rather than
    taken from the caller, so freshness cannot be faked."""
    db.set_setting("phone_location", json.dumps(
        {"lat": body.latitude, "lon": body.longitude, "acc": body.accuracy, "ts": time.time()}))
    log_event("phone_location_updated")  # no coordinates in logs
    return {"status": "ok"}


@router.post("/api/send-location", dependencies=[Depends(require_auth)])
async def send_location(body: LocationRequest):
    fix = _phone_fix()
    if fix is None:
        raise HTTPException(409, "No recent location from the phone. Open the dashboard "
                                 "on your phone and allow location, then try again.")
    payload = {"contact": body.contact, "latitude": fix["lat"],
               "longitude": fix["lon"], "accuracy": fix.get("acc")}
    return await _send("location", "/send-location", payload, cooldown=20)


# ------------------------------------------- the owner's own WhatsApp ----
# Read-only, and deliberately not stored: `message_log` holds what went through
# the agent, while these are the owner's whole WhatsApp passing through on its
# way to their own screen. Opening a thread here sends no read receipt either.

DIRECT_CHAT_ID = r"^\d+@(c\.us|lid)$"          # one person
# Digits, plus a ":12" device suffix or a classic group's "-<created>" stamp.
ANY_CHAT_ID = r"^\d[\d:-]*@(c\.us|lid|g\.us)$"       # or a group


# A WhatsApp message id, for replying to one in particular. The bridge checks
# it again — against its own pattern and against the conversation it belongs
# to — because a quote carries the quoted text into the chat, so an id from
# somewhere else would copy one person's words into another person's thread.
MESSAGE_ID = r"^(true|false)_\d[\d:-]*@(c\.us|lid|g\.us)_[A-Za-z0-9=-]{4,128}(_\d[\d:-]*@(c\.us|lid))?$"


class ReplyRequest(BaseModel):
    chat_id: str = Field(pattern=DIRECT_CHAT_ID)
    text: str = Field(min_length=1, max_length=4000)
    quoted_msg_id: str = Field(default="", pattern=rf"^$|{MESSAGE_ID[1:-1]}$")


async def _bridge_read(path: str, payload: dict, timeout: float) -> dict:
    r = await _bridge_post(path, payload, timeout=timeout)
    if r.status_code == 404:
        raise HTTPException(404, "That conversation is not on WhatsApp any more.")
    if r.status_code != 200:
        raise HTTPException(502, f"The bridge could not read WhatsApp ({r.status_code}): {r.text[:120]}")
    return r.json()


# ------------------------------------------------------ your own notes ----
# These two files are the only thing the assistant knows about its owner, and
# they live on a Docker volume the image never writes to — so without this the
# only way to put a line in them is shell access to the server.

NOTES = {"about_me": llm.KB_PATH, "style": llm.STYLE_EXAMPLES_PATH}
MAX_NOTE_CHARS = 20000


class NoteRequest(BaseModel):
    name: Literal["about_me", "style"]
    text: str = Field(max_length=MAX_NOTE_CHARS)


def _read_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""      # not written yet, which is the normal first state


@router.get("/api/notes", dependencies=[Depends(require_auth)])
def read_notes():
    return {"notes": {name: _read_note(path) for name, path in NOTES.items()}}


@router.post("/api/notes", dependencies=[Depends(require_auth)])
def write_note(body: NoteRequest):
    """Saved through a temporary file and renamed, so a half-written save can
    never leave the assistant reading a truncated version of who you are.

    The prompt notices the new mtime by itself, so this takes effect on the
    next message rather than on the next restart.
    """
    path = NOTES[body.name]
    text = body.text.replace("\r\n", "\n")
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as err:
        log_event("note_save_failed", level="error", note=body.name, error=type(err).__name__)
        raise HTTPException(500, "Could not save that. The notes folder may not be writable.")

    # Length only: these are the owner's own words and do not belong in a log.
    log_event("note_saved", note=body.name, chars=len(text))
    return {"status": "saved", "chars": len(text)}


def _assistant_states(chats: list[dict]) -> None:
    """Annotate a whole list of chats in one pass, in place.

    The single-chat helper below reads contacts.json and opens two or three
    SQLite connections every time it is called. Once per chat that is 1,100
    connections and 400 file reads for a 400-row list, around a quarter of a
    second of blocked event loop — and this runs inside an async handler, so
    the live stream and every other request wait behind it.

    Same answers, read once.
    """
    book = contacts_store.load()
    modes = db.all_modes()
    fallback = db.default_mode()
    for chat in chats:
        chat_id = chat.get("id", "")
        name = contacts_store.name_for(chat_id, book=book)
        key = name.strip().lower() if name else ""
        mode = modes.get(key, fallback) if name else fallback
        is_group = bool(chat.get("is_group"))
        chat["assistant"] = {
            "name": name,
            "mode": mode,
            "auto": mode == "auto" and not is_group,
            "chosen": bool(name) and key in modes,
            "can_choose": not is_group,
        }


def _assistant_state(chat_id: str, is_group: bool = False) -> dict:
    """What the assistant will do with this conversation, for the pane to show.

    A chat with nobody on the contact list has no setting of its own, so it
    takes the default — which is worth saying out loud, because "chosen" and
    "happens to be covered by the default" look identical until the default
    changes.
    """
    name = contacts_store.name_for(chat_id)
    mode = db.get_mode(name)
    return {
        "name": name,
        "mode": mode,
        "auto": mode == "auto" and not is_group,
        "chosen": bool(name) and name.strip().lower() in db.all_modes(),
        # The agent never answers a group, whatever the mode says.
        "can_choose": not is_group,
    }


@router.get("/api/wa/link", dependencies=[Depends(require_auth)])
async def wa_link():
    """Whether WhatsApp is linked, and the code to link it with.

    A server has nowhere to show a QR: the terminal art in the bridge's logs
    rotates every twenty seconds and cannot be scanned off a log viewer, and
    requesting a pairing code has started failing. So the code is drawn here
    and scanned off this page instead, which is the only route that needs
    neither a visible browser on the server nor a session copied in by hand.
    """
    try:
        r = await bridge.post("/link-status", {}, timeout=15)
    except httpx.HTTPError:
        # The bridge really is unreachable, which is a different problem from
        # being reachable and unlinked. The page says so differently.
        return {"reachable": False, "linked": False, "qr_svg": None}
    if r.status_code != 200:
        return {"reachable": True, "linked": False, "qr_svg": None}
    body = r.json()
    return {
        "reachable": True,
        "linked": bool(body.get("linked")),
        "qr_svg": body.get("qr_svg"),
        "code": body.get("code"),
        "stage": body.get("stage") or "",
        "error": body.get("error"),
    }


@router.get("/api/wa/chats", dependencies=[Depends(require_auth)])
async def wa_chats(limit: int = Query(25, ge=1, le=600)):
    """Recent conversations, most recently active first.

    The ceiling used to be 100, which is why the pane never showed a whole
    account: this phone has something over three hundred conversations, so a
    hundred of them was all anyone could ask for and the rest simply were not
    there. Raising it costs almost nothing, because the bridge already reads
    every chat out of the page on each call — the limit only decides how many
    of them are shaped and sent on, never how many are fetched.
    """
    data = await _bridge_read("/chats", {"limit": limit}, timeout=60)
    _assistant_states(data.get("chats", []))
    data["default_mode"] = db.default_mode()
    return data


@router.get("/api/wa/thread", dependencies=[Depends(require_auth)])
async def wa_thread(chat_id: str = Query(pattern=ANY_CHAT_ID), limit: int = Query(50, ge=1, le=100)):
    """One conversation, oldest message first."""
    data = await _bridge_read("/thread", {"chat_id": chat_id, "limit": limit}, timeout=90)
    chat = data.get("chat") or {}
    chat["assistant"] = _assistant_state(chat_id, chat.get("is_group", False))
    # Who actually sent each outgoing message. One query for the whole thread,
    # bounded by the oldest message on screen rather than the whole log.
    messages = data.get("messages") or []
    if messages:
        oldest = min(float(m.get("ts") or 0) for m in messages)
        sent_marks.mark(messages, db.outgoing_for_chat(
            chat_id, oldest - sent_marks.WINDOW_SECONDS))
    data["default_mode"] = db.default_mode()
    return data


class AssistantForChatRequest(BaseModel):
    chat_id: str = Field(pattern=ANY_CHAT_ID)
    name: str = Field(default="", max_length=60)
    # "on or off" was never the whole question: on means either answering by
    # herself or writing a draft and waiting, and those are very different
    # things to agree to for one person. `enabled` is still accepted so an
    # older page keeps working, and means the same as mode="auto"/"hold".
    mode: Literal["auto", "hold", "ignore"] | None = None
    enabled: bool | None = None


@router.post("/api/wa/assistant", dependencies=[Depends(require_auth)])
async def set_assistant_for_chat(body: AssistantForChatRequest):
    """Choose, from the conversation itself, whether the assistant answers it.

    Picking someone here is what puts them on the contact list, so a person who
    has written to you but was never saved on your phone can be chosen without
    first being added by hand. Turning it off holds their replies for approval
    rather than ignoring them: you asked to stop the sending, not to stop
    hearing from them.
    """
    if body.chat_id.endswith("@g.us"):
        raise HTTPException(400, "The assistant never answers group chats.")
    try:
        name = contacts_store.add_for_chat(body.chat_id, body.name)
    except ValueError as err:
        raise HTTPException(400, str(err))

    if body.mode is None and body.enabled is None:
        raise HTTPException(400, "Say what the assistant should do with this chat.")
    mode = body.mode or ("auto" if body.enabled else "hold")
    db.set_mode(name, mode)
    db.log("mode_changed", f"{name}: {mode}")
    log_event("mode_changed", mode=mode)   # no name or number in logs
    return {"name": name, "assistant": _assistant_state(body.chat_id)}


# What a browser may be handed as a picture, a video or a sound. Anything else
# WhatsApp labels as media is served as a download, never rendered: the page
# must not be talked into treating an arbitrary file as something to execute.
RENDERABLE_MEDIA = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/quicktime", "video/3gpp",
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr", "audio/wav",
}


@router.get("/api/wa/media", dependencies=[Depends(require_auth)])
async def wa_media(chat_id: str = Query(pattern=ANY_CHAT_ID), msg_id: str = Query(min_length=1, max_length=120)):
    """The bytes of one message's photo, video, sticker or voice note.

    Fetched one at a time, when the thread actually shows it, because every one
    of these is a download through the bridge's browser session.
    """
    r = await _bridge_post("/media", {"chat_id": chat_id, "msg_id": msg_id}, timeout=120)
    if r.status_code == 404:
        # Say which of these it was. One sentence for all of them sent the
        # owner looking for a message that is still on their phone.
        reasons = {
            "no_such_message": "This device does not have that message.",
            "different_chat": "That attachment belongs to another conversation.",
            "no_media": "That message has no attachment.",
            "not_on_this_device": "The file was never downloaded to this device, "
                                  "and WhatsApp no longer has it. Open it on your phone.",
            "download_failed": "WhatsApp would not hand over that file.",
            "lookup_failed": "That message could not be read.",
        }
        try:
            reason = (r.json() or {}).get("error", "")
        except ValueError:
            reason = ""
        raise HTTPException(404, reasons.get(reason, "That attachment could not be fetched."))
    if r.status_code == 413:
        raise HTTPException(413, "That attachment is too large to show here.")
    if r.status_code != 200:
        raise HTTPException(502, f"The bridge could not fetch it ({r.status_code}).")

    payload = r.json()
    try:
        raw = base64.b64decode(payload.get("data") or "", validate=True)
    except (ValueError, TypeError):
        raise HTTPException(502, "That attachment did not arrive intact.")

    mimetype = str(payload.get("mimetype") or "application/octet-stream").split(";")[0].strip().lower()
    # An unexpected type is still handed over, but as a download rather than as
    # something the page will render in place.
    disposition = "inline" if mimetype in RENDERABLE_MEDIA else "attachment"
    log_event("wa_media_read", kind=mimetype, bytes=len(raw))  # no id, name or number
    return Response(
        content=raw,
        media_type=mimetype if mimetype in RENDERABLE_MEDIA else "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="attachment"',
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/wa/avatar", dependencies=[Depends(require_auth)])
async def wa_avatar(chat_id: str = Query(pattern=ANY_CHAT_ID)):
    """A chat's profile photo.

    The bridge hands back WhatsApp's own URL and the fetch happens here, so the
    browser never talks to WhatsApp and that URL never reaches the page. No
    photo is an ordinary answer, not an error: the other person may simply have
    their privacy set so the owner cannot see one.
    """
    r = await _bridge_post("/avatar", {"chat_id": chat_id}, timeout=30)
    if r.status_code != 200:
        raise HTTPException(404, "No photo.")
    url = str(r.json().get("url") or "")
    if not url.startswith("https://"):
        raise HTTPException(404, "No photo.")

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
            picture = await http.get(url)
    except httpx.HTTPError:
        raise HTTPException(502, "Could not fetch that photo.")
    if picture.status_code != 200:
        raise HTTPException(404, "No photo.")

    mimetype = picture.headers.get("content-type", "").split(";")[0].strip().lower()
    if not mimetype.startswith("image/"):
        raise HTTPException(404, "No photo.")
    return Response(
        content=picture.content,
        media_type=mimetype,
        headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/wa/stream", dependencies=[Depends(require_auth)])
async def wa_stream(request: Request):
    """Server-sent events: one line each time a conversation changes.

    The pane follows WhatsApp as it happens rather than polling. Only the chat
    id is sent; the page then fetches that conversation through the endpoints
    above, so nothing bypasses the password.
    """
    queue = live.subscribe()

    async def notices():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    kind, chat_id = await asyncio.wait_for(queue.get(), timeout=STREAM_KEEPALIVE)
                except TimeoutError:
                    yield ": still here\n\n"   # stops a proxy closing an idle stream
                    continue
                yield f"event: {kind}\ndata: {json.dumps({'chat_id': chat_id})}\n\n"
        finally:
            live.unsubscribe(queue)

    return StreamingResponse(notices(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",   # nginx and friends must not buffer a stream
    })


# ---- the actions on one message: WhatsApp's own long-press menu ----
# Reply is /api/wa/reply above; Copy never leaves the browser; Report is a
# moderation action this will not automate.


class MessageAction(BaseModel):
    chat_id: str = Field(pattern=ANY_CHAT_ID)
    msg_id: str = Field(pattern=MESSAGE_ID)


class ReactRequest(MessageAction):
    # "" is how the library removes a reaction, so it is a real value here and
    # not a missing one. The bridge checks it is a single emoji; the bound
    # here only keeps an unreasonable string off the wire.
    emoji: str = Field(default="", max_length=64)


class ForwardRequest(MessageAction):
    to_chat_id: str = Field(pattern=ANY_CHAT_ID)


class StarRequest(MessageAction):
    starred: bool


class PinRequest(MessageAction):
    pinned: bool
    duration: Literal["24h", "7d", "30d"] = "24h"


class DeleteRequest(MessageAction):
    # No default on purpose. Deleting for everyone is visible to the other
    # person and cannot be undone, so it is never what a caller gets by
    # forgetting to say.
    scope: Literal["me", "everyone"]


async def _message_action(path: str, payload: dict) -> dict:
    """Pass one of WhatsApp's own message actions through to the bridge.

    The bridge answers 400/404/409 with a sentence meant to be read — "too
    late to delete this for everyone", "WhatsApp would not pin this message" —
    so those come back as they are rather than being flattened into a 502. A
    person deciding what to do next needs WhatsApp's reason, not ours.
    """
    r = await _bridge_post(path, payload, timeout=30)
    if r.status_code == 200:
        return r.json()
    reason = ""
    if r.headers.get("content-type", "").startswith("application/json"):
        reason = ((r.json() or {}).get("error") or "")
    if r.status_code in (400, 404, 409) and reason:
        raise HTTPException(r.status_code, reason)
    raise HTTPException(502, f"The bridge could not do that ({r.status_code}): {r.text[:120]}")


@router.post("/api/wa/react", dependencies=[Depends(require_auth)])
async def wa_react(body: ReactRequest):
    return await _message_action("/react", body.model_dump())


@router.post("/api/wa/forward", dependencies=[Depends(require_auth)])
async def wa_forward(body: ForwardRequest):
    return await _message_action("/forward", body.model_dump())


@router.post("/api/wa/star", dependencies=[Depends(require_auth)])
async def wa_star(body: StarRequest):
    return await _message_action("/star", body.model_dump())


@router.post("/api/wa/pin", dependencies=[Depends(require_auth)])
async def wa_pin(body: PinRequest):
    return await _message_action("/pin-message", body.model_dump())


@router.post("/api/wa/delete", dependencies=[Depends(require_auth)])
async def wa_delete(body: DeleteRequest):
    """Deleting for everyone is irreversible and the other person sees it go.

    The bridge refuses it up front when WhatsApp would not actually honour it,
    because the library quietly downgrades to a delete-for-me in that case and
    reports success either way — which would tell the owner a message was
    withdrawn when it is still sitting on somebody's phone.
    """
    return await _message_action("/delete-message", body.model_dump())


@router.post("/api/wa/reply", dependencies=[Depends(require_auth)])
async def wa_reply(body: ReplyRequest):
    """Send a message as yourself, into a conversation that already exists.

    This is the one place where a chat id comes from the browser rather than
    from the database, so the bridge checks it against WhatsApp itself: only an
    existing conversation resolves, and groups are refused. Replying is not
    limited to contacts.json — the point of the pane is to answer whoever has
    written to you — so it is the dashboard password that stands between a
    stranger and your WhatsApp.
    """
    if time.time() - _last_send.get("reply", 0.0) < 2:
        raise HTTPException(429, "Just sent one. Wait a moment.")

    r = await _bridge_post("/send-reply", body.model_dump(), timeout=30)
    if r.status_code == 404:
        # Two different things are missing at 404 here, and sending the owner
        # to look for the wrong one wastes their time.
        detail = ((r.json() or {}).get("error") or "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if "quoted" in detail:
            raise HTTPException(404, "That message is not in this conversation any more, so it could not be quoted. Send it without the quote?")
        raise HTTPException(404, "That conversation is not on WhatsApp any more.")
    if r.status_code != 200:
        raise HTTPException(502, f"The bridge could not send it ({r.status_code}): {r.text[:120]}")

    _last_send["reply"] = time.time()
    db.log_message(body.chat_id, contacts_store.name_for(body.chat_id), "out", body.text, "dashboard")
    log_event("dashboard_reply")   # no text, name or number
    return {"status": "sent"}


@router.post("/api/transcribe", dependencies=[Depends(require_auth)])
async def transcribe(request: Request, lang: str = "auto"):
    """Raw audio bytes in the body (whatever the browser's MediaRecorder made).
    Returns text only: the page puts it in the input box and nothing is sent."""
    if lang not in ("auto", "en", "sw"):
        raise HTTPException(400, "Language must be auto, en or sw.")
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording is too long.")

    audio = await request.body()
    if not audio:
        raise HTTPException(400, "No audio received.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording is too long.")

    try:
        # Whisper is CPU-heavy and blocking: keep it off the event loop.
        text = await run_in_threadpool(
            transcribe_bytes, audio, lang, os.getenv("DASHBOARD_WHISPER_SIZE") or None, True
        )
    except Exception as err:
        log_event("transcribe_failed", level="error", error=type(err).__name__)
        raise HTTPException(422, "Couldn't read that recording. Try again.")
    return {"text": text}


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/api/speak", dependencies=[Depends(require_auth)])
async def speak(body: SpeakRequest):
    """Text in, MP3 out. Only the assistant's own replies are sent here, never contact messages."""
    try:
        audio = await synthesize_bytes(body.text)
    except Exception as err:
        log_event("speak_failed", level="error", error=type(err).__name__)
        raise HTTPException(502, "Couldn't make the voice. Is the internet connected?")
    return Response(content=audio, media_type="audio/mpeg")
