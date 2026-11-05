"""When the assistant may speak in a group, and when she must stay quiet.

A direct message is between two people. A group reply is published: everyone
in the group sees it, it cannot be taken back, and it is the most visible and
most reportable thing this system does. Automated messaging is what WhatsApp
bans accounts for, and a bot answering every line of a family group is what
that looks like from the outside. So the default here is silence. A group says
nothing to the agent until the owner switches that one group on, and the first
thing she does in a group the owner has just switched on is write a draft.

ALLOW_GROUPS, the flag this exists to replace, had one setting for every group
the owner is in: on meant an automatic reply to every message in all of them.

Pure on purpose. State arrives as arguments and a decision goes out, so every
path can be tested exhaustively; nothing here reads the database or the
network, and the environment is read only by from_env(). The caller's own
gates stack on top of what this returns — the global kill switch and
app.quiet_hours are not repeated here, see held_instead().
"""
import os
import re
import time
from dataclasses import dataclass

# When she may answer at all, per group:
#   ignore   never — she reads nothing and says nothing (the default)
#   mention  only when somebody in the group was talking to her
#   open     every message in the group
#
# The per-group counterpart of db.MODES for a contact, keeping that module's
# word where it means the same thing ("ignore" is "leave alone" in both).
MODES = ("ignore", "mention", "open")
DEFAULT_MODE = "ignore"

# What happens to an answer she has written, per group. db.MODES calls these
# "auto" and "hold" for a contact and they mean the same thing here.
#
# "hold" is the default because a group reply is public and cannot be taken
# back: a group the owner has just switched on should show them what she would
# have said before she starts saying it.
DELIVERY = ("auto", "hold")
DEFAULT_DELIVERY = "hold"

# At most this many replies in one group per hour, and never two of them
# closer together than this.
#
# whatsapp-bridge/send_queue.js keeps ceilings of the same kind for the
# account as a whole (60 an hour, a few seconds apart, jittered). These are
# per group and stack on top of that one rather than replacing it: the
# account-wide ceiling answers "is this account talking too much", and this
# one answers "is she talking too much in this room" — which is the only rate
# every member of the group can actually see.
#
# Three an hour, five minutes apart, is a person glancing at their phone
# between other things. Thirty an hour would be somebody doing nothing else,
# and an answer to every message is not a person at all.
REPLIES_PER_HOUR = 3
MIN_GAP_SECONDS = 300
HOUR_SECONDS = 3600

# A display name is a stranger's own words and it goes into a prompt, so it is
# clipped as well as cleaned.
NAME_CHARS = 40

_SPACE = re.compile(r"\s+")


# ---- what she answers to ----

def names() -> tuple[str, ...]:
    """Her name and the short forms she also answers to.

    The same two settings app.assistant and app.llm read, so the name she
    introduces herself by is the name that wakes her in a group.
    """
    name = os.getenv("ASSISTANT_NAME", "Arianna").strip()
    nicknames = os.getenv("ASSISTANT_NICKNAMES", "Arie")
    return tuple(n for n in [name, *(part.strip() for part in nicknames.split(","))] if n)


def name_pattern(words: tuple[str, ...] | list[str]) -> re.Pattern | None:
    """Her names as one whole-word, case-insensitive test, or None.

    Whole word because a group discussing "Arianna's shop" is talking to her
    and "ariannaesque" is not. Escaped because a name is a setting, and a
    setting containing "." or "*" must be a name rather than a wildcard.

    A configured name with no letter or digit in it is dropped: escaped or
    not, a name of pure punctuation would match punctuation in every message
    and switch the group to "open" by accident.
    """
    parts = [re.escape(word) for word in words or () if word and re.search(r"[^\W_]", word)]
    if not parts:
        return None
    # \w rather than \b: \b means "between a word and a non-word character",
    # which is no boundary at all on a name that ends in punctuation.
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def mentions_name(text: str, words: tuple[str, ...] | list[str]) -> bool:
    """Whether her name or a nickname is in these words."""
    pattern = name_pattern(words)
    return bool(pattern and pattern.search(text or ""))


# ---- the decision ----

@dataclass(frozen=True)
class GroupMessage:
    """One message that arrived in a conversation, as the webhook sees it."""
    chat_id: str = ""
    is_group: bool = False
    body: str = ""
    # What the bridge decided (whatsapp-bridge/group_mentions.js): an @mention
    # of the owner, or a reply to one of her messages. Both of those live on
    # the WhatsApp message rather than in its text, so only the bridge can see
    # them.
    addressed: bool = False
    addressed_how: str = ""      # "mention" | "name" | "reply" | ""
    sender_name: str = ""
    at: float = 0.0              # when it arrived, epoch seconds


@dataclass(frozen=True)
class GroupPolicy:
    """The rules in force for one group at one moment."""
    mode: str = DEFAULT_MODE
    delivery: str = DEFAULT_DELIVERY
    replies_per_hour: int = REPLIES_PER_HOUR
    min_gap_seconds: int = MIN_GAP_SECONDS
    # When she has already replied in THIS group, epoch seconds, in any order.
    # db.outgoing_for_chat(chat_id, since) is where these come from.
    recent_replies: tuple[float, ...] = ()
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """What to do, and why in words the owner can read.

    `code` is the stable key — for the logs, and for the one place that has to
    compare a reason rather than show it. `reason` is the same thing as a
    phrase, which is what the Held card and the Activity feed display; the
    dashboard falls back to showing a reason it has no entry for, so a phrase
    there reads correctly without anything else having to know about groups.
    """
    action: str      # "send" | "hold" | "ignore"
    code: str
    reason: str


# Why she is answering, for the owner to read in the Activity feed.
SEND_REASONS = {
    "mention": "She was mentioned in this group",
    "name": "She was called by name in this group",
    "reply": "Someone in this group replied to her",
    "": "This group is set to reply to everything",
}


def normalise_mode(mode: str | None) -> str:
    """A mode nobody recognises means silence, not speech: a typo in storage
    must never be what switches a group on."""
    return mode if mode in MODES else DEFAULT_MODE


def normalise_delivery(delivery: str | None) -> str:
    """Likewise: anything unrecognised writes a draft rather than sending."""
    return delivery if delivery in DELIVERY else DEFAULT_DELIVERY


def addressed_in(message: GroupMessage, policy: GroupPolicy) -> tuple[bool, str]:
    """Whether somebody in the group was talking to her, and how.

    The bridge's answer, or her name in the words. The name test is repeated
    on this side because this is the gate: a payload that arrives without the
    bridge's field — an older bridge, a replayed message — must not make an
    addressed message look like chatter.
    """
    if message.addressed:
        return True, message.addressed_how or "mention"
    if mentions_name(message.body, policy.names):
        return True, "name"
    return False, ""


def decide(message: GroupMessage, policy: GroupPolicy, now: float | None = None) -> Decision:
    """What to do about one message in one group.

    A direct message is handed straight back: no mode, no addressing test and
    no ceiling is applied to one, so nothing in this module can change how a
    direct chat is answered. That is the one thing switching groups on must
    not do.
    """
    if not message.is_group:
        return Decision("send", "not_a_group", "Not a group chat")

    mode = normalise_mode(policy.mode)
    if mode == "ignore":
        # Before the addressing test on purpose: a group set to leave alone is
        # left alone even when she is named in it by everybody in it.
        return Decision("ignore", "group_off", "This group is set to leave alone")

    addressed, how = addressed_in(message, policy)
    if mode == "mention" and not addressed:
        return Decision("ignore", "not_addressed", "Nobody in this group was talking to her")

    code, reason = rate_limited(policy, time.time() if now is None else now)
    if code:
        # A question somebody actually asked her is kept for the owner to
        # answer by hand. Ordinary chatter past the ceiling is let go instead,
        # because holding it would fill the Held queue with lines nobody
        # addressed — which is how the owner stops reading the queue.
        return Decision("hold" if addressed else "ignore", code, reason)

    if normalise_delivery(policy.delivery) == "hold":
        return Decision("hold", "group_mode", "This group is set to ask you before anything goes out")

    return Decision("send", f"group_{how or 'open'}", SEND_REASONS.get(how, SEND_REASONS[""]))


def rate_limited(policy: GroupPolicy, now: float) -> tuple[str, str]:
    """Whether she has said enough in this group for the moment: a code and a
    phrase, or two empty strings."""
    within_hour = [at for at in policy.recent_replies if at > now - HOUR_SECONDS]
    if len(within_hour) >= policy.replies_per_hour:
        # A ceiling of 0 is read as silence rather than as "no ceiling": a
        # setting of none must not be the thing that lets her talk freely.
        return ("group_hour_cap",
                f"She has already replied {_spell_count(len(within_hour))} in this group this hour")
    if policy.recent_replies and now - max(policy.recent_replies) < policy.min_gap_seconds:
        return ("group_min_gap",
                f"She replied in this group less than {_spell_gap(policy.min_gap_seconds)} ago")
    return "", ""


def held_instead(decision: Decision, code: str, reason: str) -> Decision:
    """What a decision becomes when sending has to wait — quiet hours, or the
    global pause.

    Those two are app.quiet_hours' and db.auto_reply_enabled()'s business and
    are not repeated here; this is only how they stack with a group's own
    rules. A send becomes a held draft, exactly as the direct path already
    does it. An ignore stays an ignore: a group whose messages were never
    going to be answered does not need a draft written for every line of it at
    two in the morning, which is the hour this gate exists for.
    """
    if decision.action != "send":
        return decision
    return Decision("hold", code, reason)


def from_env(mode: str = DEFAULT_MODE, delivery: str = DEFAULT_DELIVERY,
             recent_replies: tuple[float, ...] | list[float] = ()) -> GroupPolicy:
    """A policy for one group: that group's own two settings, plus the parts
    that are the same for every group.

    GROUP_REPLIES_PER_HOUR and GROUP_MIN_GAP_SECONDS override the ceilings. A
    missing or misspelt value keeps the default rather than failing, and so
    does a negative one: "-1 replies an hour" is not a licence to reply
    freely.
    """
    return GroupPolicy(
        mode=normalise_mode(mode),
        delivery=normalise_delivery(delivery),
        replies_per_hour=_bounded("GROUP_REPLIES_PER_HOUR", REPLIES_PER_HOUR),
        min_gap_seconds=_bounded("GROUP_MIN_GAP_SECONDS", MIN_GAP_SECONDS),
        recent_replies=tuple(recent_replies),
        names=names(),
    )


# ---- the text the model reads ----

def speaker_label(turn: dict) -> str:
    """What to call whoever sent this message.

    The display name WhatsApp gave, then their bare number. Never the local
    part of an @lid, which is fifteen digits matching nothing the owner or
    anyone in the group has ever seen — app.controls.readable_who makes the
    same call for the same reason.

    The name is a stranger's own words on their way into a prompt, so the one
    thing it must not be able to do is look like the start of somebody else's
    line: colons and line breaks come out of it.
    """
    name = _SPACE.sub(" ", str(turn.get("sender_name") or "")).replace(":", " ").strip()
    if name:
        return _SPACE.sub(" ", name[:NAME_CHARS]).strip()
    digits = _digits(turn.get("number"))
    if not digits:
        sender_id = str(turn.get("sender_id") or "")
        if sender_id.endswith("@c.us"):
            digits = _digits(sender_id)
    return f"+{digits}" if digits else "Someone"


def compose_transcript(turns: list[dict]) -> str:
    """The text the model should answer, with each speaker named.

    chat_completion() takes one string. In a group that string is several
    people, and with no name on any line the model reads a room as a single
    speaker: two members' separate requests come back answered as one, and the
    answer to one of them is addressed to the other.

    Named once per run of messages from the same speaker rather than once per
    line, because three messages from one person is one person talking and
    their name three times is the padding the owner has twice asked not to
    have.
    """
    lines: list[str] = []
    previous = ""
    for turn in turns or ():
        body = str(turn.get("body") or "").strip()
        if not body:
            continue
        who = speaker_label(turn)
        lines.append(f"{who}: {body}" if who != previous else body)
        previous = who
    return "\n".join(lines)


def _spell_count(count: int) -> str:
    return "once" if count == 1 else f"{count} times"


def _spell_gap(seconds: int) -> str:
    if seconds < 60:
        return f"{int(seconds)} seconds"
    minutes = round(seconds / 60)
    return "a minute" if minutes == 1 else f"{minutes} minutes"


def _digits(value: object) -> str:
    # The device suffix goes before the punctuation does: on a multi-device
    # account WhatsApp writes "254712345678:12@c.us", and stripping non-digits
    # first would splice that "12" onto the number.
    local = str(value or "").split("@")[0].split(":")[0]
    return re.sub(r"\D", "", local)


def _bounded(key: str, fallback: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value >= 0 else fallback
