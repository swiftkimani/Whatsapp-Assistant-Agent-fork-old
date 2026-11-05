"""Which of the two languages a piece of text is in.

Whisper may guess any of ~99 languages for a short clip; this system only needs
English or Kiswahili. The two share the Latin script, so unlike a pair written
in different alphabets they cannot be told apart by characters at all: text
that did not come from Whisper is judged by vocabulary instead.
"""
import re

# Everyday Kiswahili and Sheng that rarely turns up in an English message.
# Short, ambiguous words ("na", "ya", "si") are left out on purpose: they occur
# inside ordinary English text and would send English replies to the Swahili
# voice. Missing a Swahili message costs an accent; misreading an English one
# costs intelligibility, so this leans towards English.
# Words dropped on purpose, because they are English, English names, or words
# the reply prompt tells the assistant to write inside English sentences:
#   pole   — "the north pole", "pole vault", "pole position"
#   leo    — a common given name and the star sign
#   sasa, mambo, nani, habari, karibu, jana — all double as names or English
#   sawa, poa, fiti — these turn up in the assistant's OWN English replies
# Each one of them was enough, on its own, to read an English sentence as
# Swahili and answer it in the wrong voice.
_SWAHILI_WORDS = re.compile(
    r"\b(niaje|vipi|manze|buda|wacha|noma|ndio|ndiyo|hapana|tafadhali|kesho|wapi|"
    r"lini|nini|tuma|ujumbe|jumbe|meseji|nipigie|nitakuambia|nimeshika|"
    r"uko|niko|yako|yangu|kwako|rafiki|nakuja|tuonane|baadaye|kwanza|bado|"
    r"haraka|nimechelewa|sijui|utakuja|unaenda|umefika|twende|asubuhi|jioni)\b",
    re.I,
)


def pick_language(info) -> str:
    """info: faster-whisper's TranscriptionInfo (language, all_language_probs)."""
    if info.language in ("en", "sw"):
        return info.language
    probs = dict(getattr(info, "all_language_probs", None) or [])
    return "sw" if probs.get("sw", 0) > probs.get("en", 0) else "en"


def looks_swahili(text: str) -> bool:
    """For text with no Whisper guess behind it, such as a typed reply.

    One hit anywhere used to decide a whole message, however long. Two hits,
    or one in something short enough to be a greeting, is the same rule with
    the coin-flips taken out.
    """
    hits = len(_SWAHILI_WORDS.findall(text or ""))
    return hits >= 2 or (hits == 1 and len((text or "").split()) <= 3)
