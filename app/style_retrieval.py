"""Retrieve similar past exchanges, used only to show the model how you really reply.

The pairs come from data/style_pairs.json, which app.style_learning writes when
the owner runs the learn step over their own WhatsApp history.

Scoring is lexical: IDF-weighted overlap between the incoming message and the
`incoming` side of every stored pair. A sentence-transformers model would rank
paraphrases a little better, but it pulls in torch for roughly a gigabyte of
resident memory, and this container is already the reason the host swaps. For a
few thousand short messages the overlap puts the useful ones first, in pure
Python, in microseconds — and it costs nothing when nobody has run the learn
step yet.

Missing file, half-written file, any failure at all means "no examples", never
a crash: style is a bonus on a reply, not a precondition for sending one.
Turn it off for A/B comparisons with STYLE_RETRIEVAL=false in .env.
"""
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

from app.log import log_event

# Written by app.style_learning. The path is spelled out in both modules rather
# than shared from one of them: this module is imported on the reply path of
# every message and must stay free of FastAPI and of the learning module's
# imports, and the learning module cannot import this one because the test
# suite stubs it for the whole run.
PAIRS_FILE = Path(__file__).resolve().parent.parent / "data" / "style_pairs.json"

# \w on a str pattern is Unicode by default, so this keeps Arabic and accented
# words whole while dropping punctuation and emoji.
_WORD = re.compile(r"\w+")

_signature: tuple | None = None
_pairs: list[dict] = []
_pair_tokens: list[set[str]] = []
_idf: dict[str, float] = {}


def tokens_of(text: str) -> set[str]:
    """The words of a message, lowercased, punctuation gone."""
    return set(_WORD.findall((text or "").lower()))


def _file_signature() -> tuple | None:
    try:
        stat = PAIRS_FILE.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _load() -> bool:
    """Read the pairs, and read them again whenever the file changes.

    A learn run rewrites the file inside a container that is already serving,
    so loading once per process would mean the owner clicks Learn and sees no
    difference until the next restart. Noticing costs one stat() per message,
    the same bargain app.llm makes for the notes files.
    """
    global _signature, _pairs, _pair_tokens, _idf

    signature = _file_signature()
    if signature == _signature:
        return bool(_pairs)
    _signature, _pairs, _pair_tokens, _idf = signature, [], [], {}
    if signature is None:
        return False

    stored = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
    pairs = [p for p in stored.get("pairs", []) if p.get("incoming") and p.get("reply")]
    document_frequency = Counter(token for p in pairs for token in tokens_of(p["incoming"]))
    _pairs = pairs
    _pair_tokens = [tokens_of(p["incoming"]) for p in pairs]
    # Rare words say what a message is about; "ok", "na" and "the" say nothing
    # and would otherwise decide the ranking, because they are in everything.
    _idf = {token: math.log(1 + len(pairs) / (1 + df)) for token, df in document_frequency.items()}
    return bool(_pairs)


def _score(query: set[str], pair: set[str]) -> float:
    shared = query & pair
    if not shared:
        return 0.0
    # Divided by the union so a long rambling message that happens to contain
    # the query's words does not outrank a short, tight match.
    return sum(_idf.get(token, 0.0) for token in shared) / len(query | pair)


def retrieve_examples(text: str, k: int = 5) -> list[dict]:
    try:
        if os.getenv("STYLE_RETRIEVAL", "true").lower() != "true":
            return []
        if not text.strip() or not _load():
            return []
        query = tokens_of(text)
        # Pairs sharing nothing with the message are left out rather than used
        # to pad k: they teach the model nothing and cost prompt tokens on
        # every reply. Ties break on position, so the result is deterministic.
        scored = []
        for index, pair_tokens in enumerate(_pair_tokens):
            score = _score(query, pair_tokens)
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [_pairs[index] for _, index in scored[:k]]
    except Exception as err:
        # Swallowed on purpose — a reply must go out with no examples rather
        # than not go out — but said out loud, because a pairs file that
        # cannot be read means the owner's voice silently stopped reaching the
        # prompt. The class only: no message text in a log line. _load caches
        # the signature of the file it failed on, so this is one line per bad
        # file rather than one per message.
        log_event("style_retrieval_failed", level="warning", error=type(err).__name__)
        return []
