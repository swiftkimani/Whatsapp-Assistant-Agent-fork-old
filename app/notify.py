"""Telling the owner that a draft is waiting.

The Held queue is where every reply he must approve waits, and nothing said one
was there: the queue only existed while a dashboard was open on a screen he
happened to be looking at. The whole safety design of this project rests on him
working that queue, so the queue has to be able to reach him.

Two routes, each switchable on its own and both off until he turns them on:

  * **Web push**, which is the only one that reaches a phone with no page open.
    Payload-less on purpose: the push message carries no data at all, so
    nothing about a conversation passes through Google's or Mozilla's push
    service, encrypted or otherwise. The service worker asks this server how
    many replies are waiting when the push arrives (app/static/sw.js).
  * **A WhatsApp message to himself**, through the bridge's /send-self. It
    lands in the app he is already in, which makes it the most reliable of the
    two, and it is rate-limited hard: one message every
    NOTIFY_SELF_MIN_MINUTES at most, carrying a count ("3 replies are waiting
    for you"), never one per draft. This account must not be seen messaging
    itself dozens of times an hour.

Quiet hours (app/quiet_hours.py, consulted, never re-implemented) suppress
both. A notification at 3am is worse than no notification.

Nothing happens at import time and no key is ever generated here. With no VAPID
keys in the environment, web push is simply off and says why.

Env: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT (a mailto: address or
an https:// URL), NOTIFY_SELF_MIN_MINUTES (default 30), NOTIFY_PUSH_MIN_SECONDS
(default 60). Read when a request arrives, not at import, because main.py calls
load_dotenv() late.
"""
import base64
import binascii
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from app import bridge, db, quiet_hours
from app.dashboard import require_auth
from app.log import anon, log_event

router = APIRouter()
AUTH = [Depends(require_auth)]

# Both switches and both rate-limit clocks live in the settings table that
# already holds the other owner switches, so there is one place that answers
# "what is turned on". No new table for four scalars.
PUSH_ENABLED = "notify_push"
SELF_ENABLED = "notify_self_message"
PUSH_LAST_SENT = "notify_push_last_sent"
SELF_LAST_SENT = "notify_self_last_sent"

SERVICE_WORKER = Path(__file__).parent / "static" / "sw.js"

JWT_LIFETIME = 12 * 3600      # a VAPID token may be valid for at most 24 h
PUSH_TTL = 6 * 3600           # how long a push service may hold the nudge for a sleeping phone
PUSH_TIMEOUT = 10.0
SELF_TIMEOUT = 20.0
# A push service answers 404 or 410 when a browser has thrown the subscription
# away. It is the only authoritative "this device is gone" we ever get.
GONE = (404, 410)
MAX_ENDPOINT_LENGTH = 1000
MAX_NAME_LENGTH = 40
SELF_MIN_MINUTES_DEFAULT = 30
PUSH_MIN_SECONDS_DEFAULT = 60


# ------------------------------------------------------------- settings ----

def _int_env(name: str, default: int, floor: int) -> int:
    """A whole number from the environment. A misspelt value falls back to the
    default rather than raising: this is the limiter that protects the WhatsApp
    account, so it must never be the reason a request fails."""
    try:
        return max(floor, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _self_min_seconds() -> int:
    return _int_env("NOTIFY_SELF_MIN_MINUTES", SELF_MIN_MINUTES_DEFAULT, 1) * 60


def _push_min_seconds() -> int:
    return _int_env("NOTIFY_PUSH_MIN_SECONDS", PUSH_MIN_SECONDS_DEFAULT, 5)


def channels() -> dict[str, bool]:
    """Which routes the owner has turned on. Both off until he says otherwise."""
    return {"push": db.get_setting(PUSH_ENABLED, "0") == "1",
            "self_message": db.get_setting(SELF_ENABLED, "0") == "1"}


def set_channels(push: bool | None = None, self_message: bool | None = None) -> dict[str, bool]:
    """Turn either route on or off. Omitting one leaves it as it was."""
    if push is not None:
        db.set_setting(PUSH_ENABLED, "1" if push else "0")
    if self_message is not None:
        db.set_setting(SELF_ENABLED, "1" if self_message else "0")
    return channels()


def _claim_window(key: str, min_seconds: int, now: float) -> bool:
    """True when this route may speak now — and then its window is marked used.

    Marked before anything is sent, on purpose. A send that fails loses its
    turn rather than leaving the door open for the next draft to try again a
    second later, which is how an account ends up messaging itself in a loop.
    """
    try:
        last = float(db.get_setting(key, "0") or 0)
    except ValueError:
        last = 0.0
    if last > now:
        last = 0.0          # the clock moved backwards; don't wedge the limiter
    if now - last < min_seconds:
        return False
    db.set_setting(key, str(now))
    return True


# ------------------------------------------------- subscription storage ----

def _ensure_subscriptions(c) -> None:
    """Create push_subscriptions if it isn't there yet.

    Purpose: one row per browser that has been granted permission to receive a
    push, holding the endpoint its push service issued. That is the address the
    nudge is sent to; without it a granted permission is unusable.

    Why not an existing table: settings is a key/value store read by key, so it
    could only hold these as one JSON blob rewritten whole on every subscribe —
    and could not carry the UNIQUE constraint on the endpoint that stops the
    same browser being stored twice. No other table is about devices.

    Expected rows: one per browser the owner uses, so a handful. It grows only
    when he grants permission somewhere new, and a row is deleted as soon as a
    push service reports the endpoint gone.

    Read and written by this module only: the subscribe and unsubscribe routes
    below, and the sender.

    Lives here rather than in db.init() because app/db.py is being edited
    elsewhere; the connection policy itself (WAL, busy timeout) is reused from
    app/db.py rather than copied, so there is only one of it.
    """
    c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
        endpoint TEXT PRIMARY KEY,
        p256dh TEXT NOT NULL DEFAULT '',
        auth TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""")


def save_subscription(endpoint: str, p256dh: str, auth: str) -> bool:
    """Store one browser's subscription. True when this browser was new.

    Keyed on the endpoint, which the push service issues per browser, so the
    same browser subscribing again — which it does on every dashboard visit —
    updates its row instead of adding a second one. The two keys are the rest
    of the subscription as the browser hands it over; today's pushes carry no
    payload and so do not use them, and keeping half a subscription would mean
    every device had to be re-enabled by hand to ever use the other half.
    """
    now = time.time()
    with db._db() as c:
        _ensure_subscriptions(c)
        known = c.execute("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
                  "p256dh = excluded.p256dh, auth = excluded.auth, updated_at = excluded.updated_at",
                  (endpoint, p256dh, auth, now, now))
    return known is None


def forget_subscription(endpoint: str) -> bool:
    """Drop one browser. True when there was a row to drop."""
    with db._db() as c:
        _ensure_subscriptions(c)
        return c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).rowcount == 1


def subscribed_endpoints() -> list[str]:
    """Every browser to nudge, oldest first. Only the endpoint: a payload-less
    push needs nothing else."""
    with db._db() as c:
        _ensure_subscriptions(c)
        rows = c.execute("SELECT endpoint FROM push_subscriptions ORDER BY created_at")
        return [r["endpoint"] for r in rows]


def device_count() -> int:
    with db._db() as c:
        _ensure_subscriptions(c)
        return c.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]


# ----------------------------------------------------------- VAPID keys ----

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_key(text: str) -> bytes | None:
    """A VAPID key from the environment. None when it isn't base64 at all.

    Both alphabets are accepted: the tools that generate these keys emit
    base64url, but a key copied through something that re-encoded it arrives
    with "+" and "/", and refusing that would look like a bad key.
    """
    cleaned = text.strip().replace("+", "-").replace("/", "_").rstrip("=")
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError):
        return None


def _import_crypto() -> SimpleNamespace | None:
    """The pieces of `cryptography` that signing a VAPID token needs, or None
    when the package is not installed.

    A function rather than a module-level import so that a machine without the
    package still imports this module, runs the WhatsApp route and says plainly
    why the push route is unavailable — and so that the "not installed" path
    can be tested on a machine that has it.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError:
        return None
    return SimpleNamespace(hashes=hashes, ec=ec, utils=utils,
                           Encoding=Encoding, PublicFormat=PublicFormat)


def _vapid_config() -> tuple[str, str, str]:
    return (os.getenv("VAPID_PUBLIC_KEY", "").strip(),
            os.getenv("VAPID_PRIVATE_KEY", "").strip(),
            os.getenv("VAPID_SUBJECT", "").strip())


def _signing_key(private_b64: str, crypto: SimpleNamespace):
    """(key, problem) — the P-256 private key VAPID tokens are signed with."""
    secret = _decode_key(private_b64)
    if secret is None or len(secret) != 32:
        return None, "VAPID_PRIVATE_KEY must be a 32-byte P-256 key, base64url-encoded."
    try:
        key = crypto.ec.derive_private_key(int.from_bytes(secret, "big"), crypto.ec.SECP256R1())
    except ValueError:
        return None, "VAPID_PRIVATE_KEY is not a valid P-256 key."
    return key, ""


def _public_point(key, crypto: SimpleNamespace) -> bytes:
    return key.public_key().public_bytes(crypto.Encoding.X962, crypto.PublicFormat.UncompressedPoint)


def push_problem() -> str:
    """Why web push cannot work, in words for the owner. "" when it can.

    Every check is made here rather than at the first send, because each of
    these mistakes otherwise shows up as a 401 from a push service that nobody
    is watching: a mismatched key pair and a missing subject both look exactly
    like notifications quietly not arriving.
    """
    public_b64, private_b64, subject = _vapid_config()
    if not public_b64 or not private_b64:
        return "Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY to turn on phone notifications."
    if not subject.startswith(("mailto:", "https://")):
        return "Set VAPID_SUBJECT to a mailto: address or an https:// URL."
    crypto = _import_crypto()
    if crypto is None:
        return "Web push needs the cryptography package, which is not installed."
    key, problem = _signing_key(private_b64, crypto)
    if key is None:
        return problem
    public_bytes = _decode_key(public_b64)
    if public_bytes is None or len(public_bytes) != 65 or public_bytes[0] != 0x04:
        return "VAPID_PUBLIC_KEY must be a 65-byte uncompressed P-256 point, base64url-encoded."
    if public_bytes != _public_point(key, crypto):
        return "VAPID_PUBLIC_KEY is not the public half of VAPID_PRIVATE_KEY."
    return ""


def push_status() -> dict:
    """What the dashboard needs to show the push switch honestly."""
    problem = push_problem()
    return {"configured": not problem, "reason": problem,
            # Only advertised when it can actually sign: a browser that
            # subscribes with a key this server cannot sign for is a device
            # that will never be nudged and will never say so.
            "public_key": _vapid_config()[0] if not problem else "",
            "devices": device_count(),
            "enabled": channels()["push"]}


def _vapid_header(endpoint: str, now: float) -> str | None:
    """The Authorization header for one endpoint, or None when push is off.

    The token is audience-bound to the push service's own origin, so it is
    built per endpoint rather than once per send.
    """
    problem = push_problem()
    if problem:
        log_event("push_not_configured", level="warning", reason=problem)
        return None
    public_b64, private_b64, subject = _vapid_config()
    crypto = _import_crypto()
    key, _ = _signing_key(private_b64, crypto)      # already validated by push_problem()
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(now) + JWT_LIFETIME, "sub": subject}
    signed = (_b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
              + "." + _b64(json.dumps(claims, separators=(",", ":")).encode()))
    der = key.sign(signed.encode(), crypto.ec.ECDSA(crypto.hashes.SHA256()))
    r, s = crypto.utils.decode_dss_signature(der)
    # JOSE wants the two halves raw and fixed-width; cryptography hands back DER.
    return f"vapid t={signed}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={public_b64}"


# ------------------------------------------------------------- sending ----

@dataclass(frozen=True)
class Nudge:
    """What is about to go out, decided before anything is sent."""
    push: bool
    self_message: bool
    count: int
    text: str


def waiting_line(count: int, name: str = "") -> str:
    """The whole message. A count, and a name only when there is exactly one
    draft — otherwise the line would name whoever happened to be last."""
    if count > 1:
        return f"{count} replies are waiting for you in the dashboard."
    if name:
        return f"A reply to {name} is waiting for you in the dashboard."
    return "A reply is waiting for you in the dashboard."


def _short_name(name: str) -> str:
    """A name fit to put in a message. It may be a display name a stranger
    chose for themselves, so it is flattened to one printable line and bounded
    here rather than trusted."""
    printable = "".join(ch for ch in (name or "") if ch.isprintable())
    return " ".join(printable.split())[:MAX_NAME_LENGTH]


def _post_push(endpoint: str, headers: dict[str, str]) -> int:
    """POST one payload-less push. Returns the status code; raises httpx.HTTPError.

    The one place this module touches a push service, so tests stand in here
    instead of reaching the network.
    """
    with httpx.Client(timeout=PUSH_TIMEOUT) as client:
        return client.post(endpoint, headers=headers, content=b"").status_code


def _send_push(now: float) -> tuple[int, int]:
    """Nudge every subscribed browser. (delivered, forgotten). Never raises."""
    delivered = forgotten = 0
    for endpoint in subscribed_endpoints():
        header = _vapid_header(endpoint, now)
        if header is None:
            break                       # not configured: no endpoint will work either
        try:
            status = _post_push(endpoint, {"Authorization": header, "TTL": str(PUSH_TTL),
                                           "Content-Length": "0"})
        except httpx.HTTPError as err:
            log_event("push_failed", level="warning", device=anon(endpoint), error=type(err).__name__)
            continue
        if status in GONE:
            forget_subscription(endpoint)
            forgotten += 1
            log_event("push_subscription_gone", device=anon(endpoint), status=status)
        elif status >= 400:
            log_event("push_failed", level="warning", device=anon(endpoint), status=status)
        else:
            delivered += 1
    return delivered, forgotten


def _send_self_message(text: str) -> bool:
    """Put one line in the owner's own WhatsApp chat. Never raises.

    Best effort, like the reply copies in main.py: a draft that is waiting must
    never be reported as failed because the note about it did not arrive.
    """
    try:
        response = bridge.post_sync("/send-self", {"text": text}, timeout=SELF_TIMEOUT)
    except httpx.HTTPError as err:
        log_event("notify_self_failed", level="warning", error=type(err).__name__)
        return False
    if response.status_code != 200:
        log_event("notify_self_failed", level="warning", status=response.status_code)
        return False
    return True


def _deliver(nudge: Nudge, now: float) -> dict:
    """Send what was decided. Never raises; reports what each route did."""
    result = {"pending": nudge.count, "push_devices": device_count(),
              "push_sent": 0, "push_forgotten": 0, "self_message_sent": False}
    if nudge.push:
        result["push_sent"], result["push_forgotten"] = _send_push(now)
    if nudge.self_message:
        result["self_message_sent"] = _send_self_message(nudge.text)
    log_event("notify_sent", pending=nudge.count, push_sent=result["push_sent"],
              push_forgotten=result["push_forgotten"], self_message=result["self_message_sent"])
    return result


def _plan_nudge(now: float, name: str = "") -> Nudge | None:
    """What should go out right now, or None for silence.

    Every gate that can say no is here, so the caller is one line and so the
    expensive part — two HTTP calls — is never reached by a draft that should
    not have interrupted anybody.
    """
    on = channels()
    if not (on["push"] or on["self_message"]):
        return None
    if quiet_hours.is_quiet(datetime.fromtimestamp(now, quiet_hours.LOCAL_TZ)):
        return None
    count = db.pending_count()
    if count < 1:
        return None                     # already worked; nothing to interrupt him for
    push = on["push"] and _claim_window(PUSH_LAST_SENT, _push_min_seconds(), now)
    self_message = on["self_message"] and _claim_window(SELF_LAST_SENT, _self_min_seconds(), now)
    if not (push or self_message):
        return None
    return Nudge(push=push, self_message=self_message, count=count,
                 text=waiting_line(count, name))


def _dispatch(nudge: Nudge, now: float) -> None:
    """Hand the sending to a thread, so a webhook already holding a reply does
    not wait on two HTTP calls to say it held one."""
    def deliver_quietly() -> None:
        try:
            _deliver(nudge, now)
        except Exception as err:        # a thread's traceback goes nowhere useful
            log_event("notify_failed", level="warning", error=type(err).__name__)

    threading.Thread(target=deliver_quietly, name="notify", daemon=True).start()


def notify_draft_waiting(chat_id: str = "", name: str = "", now: float | None = None) -> None:
    """Tell the owner a draft is waiting. One line for the caller; never raises.

    Call it wherever a draft is created — after db.add_held() in the webhook.
    Cheap: a handful of settings reads and one COUNT, then the sending happens
    on its own thread. Quiet hours, both switches and both rate limits are
    checked in here, so the caller carries no policy at all.

    `now` exists for tests; leave it out in production.
    """
    try:
        at = time.time() if now is None else now
        nudge = _plan_nudge(at, _short_name(name))
        if nudge is None:
            return
        # Counts and a hashed chat id only — never the draft, never a name.
        log_event("notify_planned", chat=anon(chat_id) if chat_id else "", pending=nudge.count,
                  push=nudge.push, self_message=nudge.self_message)
        _dispatch(nudge, at)
    except Exception as err:
        # The note must never break the thing it is about: a draft that was
        # saved must not be reported as failed because telling him failed.
        log_event("notify_failed", level="warning", error=type(err).__name__)


# -------------------------------------------------------------- routes ----

class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class SubscribeRequest(BaseModel):
    """A browser's PushSubscription, in the shape its own toJSON() produces, so
    the page posts it as it is given rather than taking it apart."""
    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LENGTH)
    keys: SubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def _must_be_https(cls, value: str) -> str:
        # This is a URL the server will later POST to, so it is bounded: https
        # only, a real host, and a length no push service exceeds. The route
        # itself needs the owner's session, so nobody else can plant one.
        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("a push endpoint is an https URL")
        return value.strip()


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LENGTH)


class ChannelsRequest(BaseModel):
    """Either switch, or both. An omitted one is left as it was."""
    push: bool | None = None
    self_message: bool | None = None


def _quiet_hours_status() -> dict:
    bounds = quiet_hours.window()
    return {"window": f"{bounds[0]:%H:%M}-{bounds[1]:%H:%M}" if bounds else "",
            "quiet_now": quiet_hours.is_quiet()}


@router.get("/api/notify", dependencies=AUTH)
def notify_settings():
    """Everything the page needs to render the notification switches."""
    return {"push": push_status(),
            "self_message": {"enabled": channels()["self_message"],
                             "at_most_every_minutes": _self_min_seconds() // 60},
            "quiet_hours": _quiet_hours_status(),
            "pending": db.pending_count()}


@router.post("/api/notify/channels", dependencies=AUTH)
def change_channels(body: ChannelsRequest):
    if body.push:
        # Turning push on when it cannot sign a token would show an "on" switch
        # that never notifies anybody. Say so instead.
        problem = push_problem()
        if problem:
            raise HTTPException(400, problem)
    set_channels(body.push, body.self_message)
    db.log("notify_channels_changed", ", ".join(f"{k}: {'on' if v else 'off'}"
                                                for k, v in channels().items()))
    log_event("notify_channels_changed", **channels())
    return notify_settings()


@router.post("/api/notify/subscribe", dependencies=AUTH)
def subscribe(body: SubscribeRequest):
    """Remember this browser, so it can be nudged when a draft is waiting."""
    added = save_subscription(body.endpoint, body.keys.p256dh, body.keys.auth)
    devices = device_count()
    # The endpoint identifies a device and is a URL that can be POSTed to:
    # hashed in the logs, never printed.
    log_event("push_subscribed", device=anon(body.endpoint), added=added, devices=devices)
    return {"added": added, "devices": devices}


@router.post("/api/notify/unsubscribe", dependencies=AUTH)
def unsubscribe(body: UnsubscribeRequest):
    """Forget this browser. Nothing is sent to it again."""
    removed = forget_subscription(body.endpoint)
    devices = device_count()
    log_event("push_unsubscribed", device=anon(body.endpoint), removed=removed, devices=devices)
    return {"removed": removed, "devices": devices}


@router.post("/api/notify/test", dependencies=AUTH)
async def send_test():
    """Prove the switches work without waiting for a real draft.

    Deliberately ignores quiet hours and both rate limits: he asked for this
    one, right now, and a test that silently did nothing because of a limiter
    would be worse than no test. It does respect the switches, because what is
    being tested is the thing he has turned on.
    """
    on = channels()
    if not (on["push"] or on["self_message"]):
        raise HTTPException(400, "Turn on a notification route first, then test it.")
    count = db.pending_count()
    nudge = Nudge(push=on["push"], self_message=on["self_message"], count=count,
                  text="Test from your dashboard: notifications are working.")
    result = await run_in_threadpool(_deliver, nudge, time.time())
    log_event("notify_test", **result)      # so a test can be told apart from a real draft
    return result


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """The service worker, served from the root so its scope covers /dashboard.

    A worker served from a subdirectory is scoped to that subdirectory;
    Service-Worker-Allowed widens it. The file holds no secrets — everything it
    reads back needs the session cookie.
    """
    if not SERVICE_WORKER.exists():
        log_event("service_worker_missing", level="error")
        raise HTTPException(404, "The service worker is missing from this build.")
    return Response(SERVICE_WORKER.read_text(encoding="utf-8"),
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
