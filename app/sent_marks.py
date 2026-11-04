"""Which outgoing messages in a conversation the system sent, and how.

WhatsApp hands back a thread with nothing on an outgoing message but
`fromMe`, so a reply the assistant wrote by itself reads exactly like one the
owner typed on their phone. That is the difference the owner most needs to
see, because those are the ones that went out without being read first.

They are matched on their text rather than on an id. Carrying WhatsApp's own
id back would be exact, but for the automatic path it would have to travel
back through the webhook reply, and it would still say nothing about every
reply already sent. The text is the one thing both sides already have.
"""
import re

# The outcomes written by app.main (the assistant answering by itself),
# app.approvals (a draft the owner approved) and the dashboard's own send
# routes. Anything else came from somewhere this cannot speak for.
SENT_BY = {"auto": "auto", "held_sent": "held_sent", "dashboard": "dashboard"}

# The send queue spaces a reply out by up to about half a minute after the
# agent produced it, and the log row is written when the text is produced, so
# the two timestamps are never the same moment. Wide enough to cover the
# pacing and a slow send, narrow enough that yesterday's identical "Sawa" is
# not claimed as this one.
WINDOW_SECONDS = 300

# A long message reaches the thread clipped and with an ellipsis on the end
# while the log keeps what was actually sent, so the two only ever agree on a
# prefix. This much of it is far more than enough to tell two replies apart.
PREFIX_CHARS = 120

_SPACE = re.compile(r"\s+")


def fingerprint(text: str) -> str:
    """Enough of a message to recognise it again, whitespace differences gone."""
    return _SPACE.sub(" ", text or "").strip()[:PREFIX_CHARS].lower()


def mark(messages: list[dict], rows: list[dict]) -> list[dict]:
    """Annotate each message with `sent_by`, in place.

    Every log row is spent at most once, so two identical replies are matched
    to two separate sends instead of both claiming the nearest one.
    """
    pool: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        if (row.get("direction") or "") != "out":
            continue
        kind = SENT_BY.get(row.get("outcome") or "")
        key = fingerprint(row.get("body") or "")
        # An empty body is every photo and every voice note at once, so it
        # identifies nothing and would label them all.
        if kind and key:
            pool.setdefault(key, []).append((float(row.get("ts") or 0), kind))

    for message in messages:
        message["sent_by"] = ""
        if not message.get("from_me"):
            continue
        candidates = pool.get(fingerprint(message.get("body") or ""))
        if not candidates:
            continue
        when = float(message.get("ts") or 0)
        nearest = min(range(len(candidates)), key=lambda i: abs(candidates[i][0] - when))
        if abs(candidates[nearest][0] - when) <= WINDOW_SECONDS:
            message["sent_by"] = candidates.pop(nearest)[1]
    return messages
