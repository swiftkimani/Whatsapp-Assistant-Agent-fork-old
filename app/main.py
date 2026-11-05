import hmac
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()  # first, so every module below sees the .env values (USER_NAME, tokens...)

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app import db, groups, live, notify, quiet_hours
from app.agent_graph import run_agent
from app.approvals import router as approvals_router
from app.assistant import router as assistant_router
from app.notify import router as notify_router
from app.quality import router as quality_router
from app.style_learning import router as style_router
import httpx

from app import bridge
from app import contacts as contacts_store
from app.contacts import name_for
from app.controls import router as controls_router
from app.dashboard import router as dashboard_router
from app.log import anon, log_event
from app.memory import add_assistant_message, add_user_message
from app.tts import synthesize_to_base64

app = FastAPI()
app.include_router(dashboard_router)
app.include_router(assistant_router)
app.include_router(approvals_router)
app.include_router(controls_router)
app.include_router(style_router)
app.include_router(quality_router)
app.include_router(notify_router)
db.init()

REPLY_WITH_VOICE = os.getenv("REPLY_WITH_VOICE", "false") == "true"
ALLOW_GROUPS = os.getenv("ALLOW_GROUPS", "false") == "true"


def _load_allowed_contacts() -> set[str]:
    raw = os.getenv("ALLOWED_CONTACTS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


# Direct chats, privacy ids and groups are conversations. A status update, a
# channel or a broadcast list is not, and the bridge cannot reply into one.
# Digits, optionally a ":12" device suffix or a legacy group's "-1497087597"
# creator stamp. "status@broadcast" and "...@newsletter" fail on the domain.
CONVERSATION_ID = re.compile(r"^\d[\d:-]*@(c\.us|lid|g\.us)$")


def is_allowed(contact_id: str, number: str | None = None) -> bool:
    if not CONVERSATION_ID.match(contact_id or ""):
        return False
    # ALLOW_GROUPS is the outer of two gates, and it is deliberately coarse:
    # "do I want this assistant anywhere near my groups". With it off, the
    # bridge drops group messages before they are ever forwarded, so nobody's
    # group conversation reaches this process, let alone a model.
    #
    # It is not the setting that decides whether she speaks. That is each
    # group's own row in group_modes, which defaults to "leave alone" — so
    # turning this on reads every group and answers none of them until a group
    # is chosen. It used to be the only group setting there was, which meant
    # one flag away from replying to every message in every group.
    if contact_id.endswith("@g.us") and not ALLOW_GROUPS:
        return False

    entries = _load_allowed_contacts()
    if not entries:
        return True

    # "anyone" answers every direct chat, whether or not the sender is saved.
    # Spelled out rather than left to the empty list, which means the same
    # thing by accident: forgetting to set this must not be what opens the
    # door. It also makes the address book irrelevant, so it is not read.
    if any(e.lower() == "anyone" for e in entries):
        return True

    # "contacts" means everyone in the address book, which is the only setting
    # that stays correct as contacts are synced. An unreadable or empty book
    # means nobody rather than everybody.
    numbers = {_digits(e) for e in entries if e.lower() != "contacts"} - {""}
    if any(e.lower() == "contacts" for e in entries):
        book = {_digits(v) for v in contacts_store.load().values()} - {""}
        if not book:
            return False
        numbers |= book

    theirs = _digits(number or contact_id.split("@")[0])
    if not theirs:
        return False
    # Same person, different formats: 254712345678 and 0712345678 are one line.
    return any(n == theirs or (len(theirs) >= 9 and n[-9:] == theirs[-9:]) for n in numbers)


class IncomingMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(default=None, alias="from")
    body: str = ""
    type: str = "text"           # "text" | "voice" | "image"
    media_base64: str | None = None
    mimetype: str | None = None
    number: str | None = None    # phone number resolved by the bridge (for @lid contacts)
    # The sender's own WhatsApp display name, when the bridge could read it.
    # Bounded here because it is a name a stranger chose for themselves: it is
    # stored and rendered as text only, never concatenated into SQL, a path or
    # a command, and it never decides anything — the allowlist and the
    # per-contact mode both key on contacts.json, not on this.
    push_name: str | None = Field(default=None, max_length=120)

    # ---- groups ----
    # Whether this arrived in a group, and whether anybody in it was talking
    # to her. Both come from the bridge because both live on the WhatsApp
    # message rather than in its text: an @mention is a list of ids, and a
    # reply is a pointer to another message.
    is_group: bool = False
    addressed: bool = False
    addressed_how: str = Field(default="", max_length=20)
    sender_name: str | None = Field(default=None, max_length=120)
    # The burst's messages before they were joined. A group has many speakers,
    # so one joined body with one name attributes everyone's words to whoever
    # spoke last; app.groups.compose_transcript turns this into text that says
    # who said what. Bounded because it arrives from outside.
    turns: list[dict] = Field(default_factory=list, max_length=20)
    # The WhatsApp id of the message being answered. The bridge quotes with it
    # directly on an automatic reply; it is carried here so a draft the owner
    # approves later can quote the same message.
    msg_id: str | None = Field(default=None, max_length=120)


def _require_bridge(token: str) -> None:
    """Only the local bridge may call these. Without the check, anyone who can
    reach the port (or a tunnel pointing at it) could drive the agent."""
    expected = os.getenv("BRIDGE_API_TOKEN", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "unauthorized")


def _group_decision(msg: "IncomingMessage") -> groups.Decision:
    """What this group's own rules say about one message.

    The two settings are this group's; the ceilings are the same for every
    group; the timestamps are what she has already said in HERE, which is the
    only rate every member of the group can see.
    """
    mode, delivery = db.group_policy(msg.from_)
    recent = [row["ts"] for row in db.outgoing_for_chat(msg.from_, time.time() - 3600)
              if row.get("ts")]
    policy = groups.from_env(mode, delivery, recent)
    return groups.decide(groups.GroupMessage(
        chat_id=msg.from_, is_group=True, body=msg.body,
        addressed=msg.addressed, addressed_how=msg.addressed_how or "",
        sender_name=msg.sender_name or "", at=time.time(),
    ), policy)


def _should_quote(msg: "IncomingMessage", model_said: bool) -> bool:
    """Whether the reply shows what it is answering.

    The model's judgement, widened by two things it cannot see from the text
    alone. In a group a bare reply is addressed to the room rather than to
    anyone, so it always quotes. And a burst of messages is answered as one
    turn, so the reply answers something other than the last line -- which is
    exactly the confusion the quote exists to remove, and exactly the case the
    model cannot detect, because it was handed one joined turn.
    """
    if msg.is_group:
        return True
    if len(msg.turns) > 1:
        return True
    return model_said


def _model_text(msg: "IncomingMessage") -> str:
    """What the model is asked to answer.

    A direct chat is one person, so the joined body is the whole truth. A group
    is not: without a name against each run of messages the model reads a room
    as one speaker and answers two people's separate questions in one reply.
    """
    if not msg.is_group:
        return msg.body
    turns = msg.turns or [{"sender_name": msg.sender_name or "", "body": msg.body,
                           "number": msg.number, "sender_id": msg.from_}]
    return groups.compose_transcript(turns)


class ChatEvent(BaseModel):
    # Same shape as CONVERSATION_ID above: a classic group id carries a hyphen
    # and a multi-device chat a colon, and this rejected both — so the pane
    # never got told those conversations had moved.
    chat_id: str = Field(pattern=r"^\d[\d:-]*@(c\.us|lid|g\.us)$")


@app.post("/wa-event")
async def wa_event(event: ChatEvent, x_bridge_token: str = Header(default="")):
    """The bridge saying a conversation just changed, so that open dashboards
    can refresh at once instead of waiting for a poll.

    Separate from /webhook on purpose. That one is the agent's path and is
    gated by the allowlist; this one covers the owner's whole WhatsApp, and so
    carries no message text at all — only which conversation moved.
    """
    _require_bridge(x_bridge_token)
    live.publish(event.chat_id)
    return {"status": "ok"}


@app.get("/agent-state")
async def agent_state(x_bridge_token: str = Header(default="")):
    """What the bridge needs to know before it acts on the owner's behalf.

    Only "typing…" depends on this today: showing it while every reply is being
    held would promise an answer that never comes.
    """
    _require_bridge(x_bridge_token)
    return {"auto_reply": db.auto_reply_enabled() and not quiet_hours.is_quiet()}


@app.post("/webhook")
async def webhook(msg: IncomingMessage, x_bridge_token: str = Header(default="")):
    _require_bridge(x_bridge_token)

    if not is_allowed(msg.from_, msg.number):
        return {"action": "ignore"}

    name = name_for(msg.from_, msg.number)

    # A group and a direct chat are gated by different things, and the group
    # path must not fall through to the per-contact one: name_for() on a group
    # id finds nobody, so a group would take default_mode() and be judged by a
    # setting that was never about it.
    group = _group_decision(msg) if msg.is_group else None
    if group is not None and group.action == "ignore":
        # Nothing is stored and no model is called. A group left alone, or one
        # where nobody was talking to her, costs this check and no more.
        log_event("group_ignored", chat=anon(msg.from_), code=group.code)
        return {"action": "ignore"}

    mode = "auto" if group is not None else db.get_mode(name)
    if mode == "ignore":
        # Nothing is stored for an ignored message — that is the point of the
        # mode — but the drop itself is worth a line. It is otherwise the one
        # gate that can silently answer nobody: an allowlist of "anyone" with
        # the default still on "leave alone" looks live and replies to no one.
        log_event("ignored", chat=anon(msg.from_), known=bool(name))
        return {"action": "ignore"}

    # run_agent is blocking (LLM, Whisper, BLIP): keep it off the event loop
    result = await run_in_threadpool(
        run_agent,
        contact_id=msg.from_,
        message_type=msg.type,
        raw_text=_model_text(msg),
        media_base64=msg.media_base64,
        # The bridge read the type off the WhatsApp message, so a format the
        # model will not take is refused before the bytes are decoded. Without
        # it the type is still read off the bytes; this only makes the
        # rejection earlier and cheaper.
        mimetype=msg.mimetype,
    )

    add_user_message(msg.from_, result["resolved_text"])

    action, reason = result["action"], result.get("hold_reason", "")
    # The group's own decision first: it is the only one of these gates that
    # knows the reply is about to be public.
    if group is not None and action == "send" and group.action == "hold":
        action, reason = "hold", group.reason
    if action == "send" and not db.auto_reply_enabled():
        action, reason = "hold", "paused"          # global kill switch
    elif action == "send" and mode == "hold":
        action, reason = "hold", "contact_mode"    # this contact is always approved by hand
    elif action == "send" and quiet_hours.is_quiet():
        # The draft is still written and still waiting; only sending it waits.
        action, reason = "hold", "quiet_hours"

    if action == "hold":
        db.add_held(msg.from_, result["resolved_text"], result["reply_text"],
                    reason=reason, number=msg.number, why=result.get("why", ""),
                    push_name=msg.push_name or "", msg_id=msg.msg_id or "")
        db.log("held", msg.from_)
        db.log_message(msg.from_, name, "in", result["resolved_text"], "held")
        log_event("held", chat=anon(msg.from_), reason=reason)
        # So the draft appears beside the conversation at once, rather than
        # waiting in a tab the owner has to remember to open.
        live.publish(msg.from_, kind="draft")
        # And tell him somewhere he will actually see it. A queue nobody is
        # told about is a queue that does not get worked, and every safety
        # decision in this project assumes he works it. Cheap, never raises,
        # and does quiet hours, both switches and both rate limits itself.
        notify.notify_draft_waiting(msg.from_, name or "")
        return {"action": "hold", "reply_text": result["reply_text"], "reason": reason}

    add_assistant_message(msg.from_, result["reply_text"])
    await _copy_to_owner(name or (msg.number or msg.from_.split("@")[0]), result["reply_text"])
    db.log("auto_reply", msg.from_)
    db.log_message(msg.from_, name, "in", result["resolved_text"], "replied")
    db.log_message(msg.from_, name, "out", result["reply_text"], "auto")
    log_event("auto_reply", chat=anon(msg.from_))
    response = {"action": "send", "reply_text": result["reply_text"],
                # The bridge attaches the quote; it is the only side holding
                # the incoming message's WhatsApp id.
                "quote": _should_quote(msg, bool(result.get("quote"))),
                "reply_audio_base64": None}

    if REPLY_WITH_VOICE and msg.type == "voice":
        response["reply_audio_base64"] = await synthesize_to_base64(result["reply_text"])

    return response


async def _copy_to_owner(who: str, text: str) -> None:
    """Put a copy of a reply the assistant sent by itself into the owner's own
    WhatsApp chat.

    This is how the owner tells, on their phone, which messages were not
    theirs. A marker inside the message itself cannot do it: WhatsApp renders
    the same characters for both ends, so anything visible to the owner is
    visible to the person they are talking to.

    Best effort — a reply that went out must never be reported as failed
    because the note about it did not.
    """
    if not db.self_log_enabled():
        return
    note = f"🤖 Arianna replied to {who}:\n\n{text}"
    try:
        response = await bridge.post("/send-self", {"text": note}, timeout=20)
        if response.status_code != 200:
            log_event("self_log_failed", level="warning", status=response.status_code)
    except httpx.HTTPError as err:
        log_event("self_log_failed", level="warning", error=type(err).__name__)


@app.get("/", include_in_schema=False)
async def root():
    """The bare domain is what gets typed and bookmarked, and there was nothing
    there. Temporary rather than permanent: a 301 is cached by the browser for
    good, and this should stay cheap to change."""
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/health")
async def health():
    return {"status": "ok"}
