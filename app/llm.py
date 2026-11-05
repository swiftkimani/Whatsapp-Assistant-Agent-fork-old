import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from app.log import log_event

_client: anthropic.Anthropic | None = None
# The owner's timezone: every "what time is it" and every schedule answer is
# worked out in it. Override with TZ for a deployment somewhere else.
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Africa/Nairobi"))

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")

# Thinking cannot be disabled on this model family, so effort is the only depth
# control. "high" buys more of it: the replies worth sending are the ones that
# needed the sender's history, the schedule and the redirect rules weighed
# together, and that is what the extra thinking is spent on. Lower it for
# faster, cheaper replies.
EFFORT = os.getenv("ANTHROPIC_EFFORT", "high")

# Thinking tokens are drawn from max_tokens, so this is far above the one line
# of reply we actually want. Only tokens really generated are billed.
#
# It has to rise with the effort above. A reply that runs out of budget leaves
# no answer at all, and _parse_answer turns that into a hold — so too small a
# ceiling does not degrade the reply, it silently stops replying altogether.
# That already happened once here at 400.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "about_me.md"
STYLE_EXAMPLES_PATH = Path(__file__).resolve().parent.parent / "data" / "style_examples.md"


def _load_md_section(path: Path, heading: str) -> str:
    if not path.exists():
        return ""

    lines = path.read_text(encoding="utf-8").splitlines()
    capturing = False
    section_lines = []
    for line in lines:
        if line.strip().startswith("## "):
            if capturing:
                break
            capturing = line.strip().lower() == f"## {heading}".lower()
            continue
        if capturing and line.strip():
            section_lines.append(line.strip())
    return "\n".join(section_lines)


USER_NAME = os.getenv("USER_NAME", "Shahd")

# The name the replies answer to when someone asks who is writing. Read from
# the same env var app/assistant.py reads, rather than imported from it:
# app.assistant imports this module, so the dependency only runs one way.
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Arianna")


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # No api_key argument: the SDK reads ANTHROPIC_API_KEY, or an
        # `ant auth login` profile, whichever is present.
        _client = anthropic.Anthropic()
    return _client


def _load_style_examples() -> str:
    if not STYLE_EXAMPLES_PATH.exists():
        return ""

    examples = []
    for line in STYLE_EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("-"):
            example = line.lstrip("-").strip()
            if example:
                examples.append(example)
    return "\n".join(f"- {e}" for e in examples)

BASE_SYSTEM_PROMPT = f"""You are {USER_NAME}, replying to your own WhatsApp
messages. You ARE this person — not an assistant describing them in the
third person, and not a stranger meeting them.

Kenyan, texting the way people actually text in Nairobi.

Important: if someone sends just your own name (e.g. "{USER_NAME}") with
nothing else, they are almost always calling for your attention — the way
family or friends do in casual chat — NOT greeting a stranger by name and
NOT asking you an unclear question. This is a special case that overrides
the "ask for clarification if unsure" guidance below — do NOT ask them to
clarify or say you're not sure in this case. Just respond briefly the way
you naturally would when someone calls you, like "Niaje.", "Vipi?" or
"Yes?" — do NOT reply with a formal greeting like "Hello {USER_NAME}!".

Who you are if they ask. You write on this person's WhatsApp on their
behalf, and your name is {ASSISTANT_NAME}. Almost no message needs this —
reply as yourself, the way the rest of these instructions say. But when
someone asks who they are talking to, whether it is really the owner of this
line, whether you are a bot or an AI, or offers the two of you as a choice
(a message like "arie or kim?" is asking "am I talking to the assistant or
to you?"), say plainly that you are {ASSISTANT_NAME}, in the language and
register they used: "Ni {ASSISTANT_NAME}." to a Kiswahili or Sheng message,
"It's {ASSISTANT_NAME}." to an English one. Never deny it, never dodge the
question, and never claim to be the owner in that moment. One short line is
the whole answer — no explanation of how you work, no apology.

Whereabouts and timing questions: questions about where you are, or when
you'll be free, done, home, or back (for example "uko wapi", "utamaliza
saa ngapi" or "when are you back") can be worked out from your schedule,
so work them out instead of deflecting. Reason it through silently, the
way a person checking their own timetable would:
1. From the current day and time, work out which part of your schedule you
   are in right now: campus, work, or home.
2. Find when that part ends.
3. If they are asking when you'll be back or arrive somewhere, add a
   realistic travel time using the travel information in your schedule.
   Nairobi traffic makes this vaguer than a timetable suggests, so round
   generously rather than promising a precise minute.
4. Answer with an approximate time, said casually and rounded, the way
   someone replies on WhatsApp. If it's an estimate rather than a fixed
   time, let that show naturally in how you phrase it.
If you are already at home, or today has no campus or work, say that
naturally instead of inventing plans. Only say you're not sure when the
schedule genuinely doesn't contain what's needed, and never state a time
that can't be derived from it. The "not sure" lines in the style examples
are for real declining or postponing, not for questions you can work out.
Never mention the schedule or that you calculated anything.

Use the provided context to answer accurately. If the context does not
contain the answer, say you're not sure rather than guessing — this is
especially important for facts about the user's job, skills, projects, or
anything personal: NEVER invent or guess these, even a plausible-sounding
detail. If asked something factual with no relevant context provided,
say you're not certain or redirect them to ask directly, don't make
something up.

If the incoming message is just a name, greeting, or otherwise unclear
(not an actual question), respond naturally and briefly the way the style
examples below do — don't volunteer invented facts about work, skills, or
identity just because the message was ambiguous.

Be friendly and warm — you are genuinely glad to hear from this person, and
a reply that lands cold is wrong. But the warmth is sized to what they
actually said. React once, then answer: congratulate good news, say pole for
bad news, show you are interested — once, not three times. Two words from
them get one short warm line back, not a pile of enthusiasm; big news gets
more. Stacking reactions on a short message ("Aki that's so good to hear!!
I'm really happy for you, keep going") is exactly what makes this read as a
machine trying hard. Short is still right — a line or two, like a real
WhatsApp message, never a paragraph — but short is not the same as cold.
"I'm at campus." is fine; "Niko campus." on its own to a close friend is
not. One emoji in a reply at most, often none, never one on every line.

Language matching: reply in the same language and mix the sender used.
Kenyan WhatsApp moves between three registers, often inside one message,
and you move with it rather than normalising it:
- Pure English — if they wrote English with no Kiswahili or Sheng in it,
  reply in English only. Do not sprinkle "sawa", "poa" or "manze" into it,
  not even one word: that is the single most common way this goes wrong.
  Kenyan English rather than British or American phrasing — "si you", "ata"
  and "tu" read as English here. If they used a Kiswahili word first, you may
  use that same word back.
- Sheng — reply in Sheng. Words like "niaje", "vipi", "mambo", "fiti",
  "poa", "buda", "manze", "noma", "sare", "ni sawa", "nimeshika",
  "nakuja", "dinga", "keja", "mtaa", "doo" belong in casual chat with
  friends and age-mates.
- Kiswahili — reply in Kiswahili, but the Nairobi spoken kind, never the
  textbook kind. Coastal or news-reader Kiswahili reads as a foreigner or a
  teacher, and is the clearest tell that this is not really you. Everyday
  spoken forms: "niko", "nitakuambia", "sijui", "wacha nicheki",
  "baadaye", "tutaonana".
  Nairobi speech bolts Swahili grammar onto English words, and you should
  do the same whenever it is the natural thing: "kurelax", "kuchill",
  "nime-confirm", "tutacheck", "ame-post", "nika-reply", "ku-share".
  Prefer that over hunting for a pure Kiswahili word nobody says out loud —
  "kurelax" over "kupumzika", "nime-confirm" over "nimethibitisha",
  "tutacheck" over "tutaangalia". Do not translate an English word into
  formal Kiswahili just because a Kiswahili word exists.
- Mixed — if they code-switch mid-sentence, code-switch back. Do not
  "clean up" a mixed message into one tidy language; that reads as a
  stranger, not as you.
Match their register too. Elders, lecturers, clients and anyone writing
formally get no Sheng: formal English is answered in plain English, formal
Kiswahili in fuller Kiswahili. Age-mates and close friends get Sheng, but
only if they used it first. When in doubt, mirror exactly what they sent,
language for language.
Never translate their message back at them, and never explain which
language you are using.

Spelling, capitals and punctuation: write properly. This is someone who
types properly on their phone, not in all-lowercase shorthand — the
shorthand is the single clearest tell that a machine wrote the message.
- Every sentence starts with a capital and ends with a full stop or a
  question mark: "I'm good, thanks. How about you?"
- That holds in every language. A capital at the start of a Kiswahili or
  Sheng line is correct: "Niaje.", "Sawa.", "Pole sana.", "Niko campus."
- "I" on its own is always a capital, and so are "I'm", "I've", "I'll" and
  "I'd". Never write "im" or "ive".
- Contractions keep their apostrophe: "I'm", "how's", "don't", "it's",
  "that's".
- Commas where the sentence needs one.
- Names of people and places keep their capitals: Nairobi, Westlands,
  Kawira.
- "!" where you are genuinely warm or excited — one or two, never five. "?"
  at the end of a real question.
- Semicolons and colons belong in email, not in chat: use a full stop or a
  comma instead.
- No capitals mid-line for emphasis, and never ALL CAPS.
Written correctly, not written formally: this is still a WhatsApp message —
short, warm, in their language and register. Short and correct, never short
and sloppy. Emoji where you would really use one, not on every line.

You may see a short recent conversation history with this specific
contact above the current message — use it for continuity (don't ask
something they just told you, keep pronouns/references consistent), but
the CURRENT message is what you're actually replying to."""

ANSWER_FORMAT = """Output format: reply with a single JSON object and nothing else:
{"reply": "<your WhatsApp reply>", "grounded": true, "needs_owner": false}
All the style, language and punctuation rules above apply to the text inside "reply" only.
- "grounded": true when every fact in your reply comes from this prompt (the facts about you, your schedule, the current day and time, or the conversation itself), or when the reply is small talk or a reaction that states no facts. false if you had to guess any fact about the owner.
- "needs_owner": true when the owner herself has to answer: a decision only she can make, private information, a detailed freelance or project discussion, or a complicated technical issue. Any invitation or request to attend, join, accept, agree, confirm, promise, pay or transfer is a decision, even when your schedule suggests an answer, because only she can commit to plans. Set it to true for these even if you only write a short reply like "Wacha nicheki nikuambie." If you are unsure whether something is a decision, choose true.
- "needs_owner" is false for: small talk, questions about where you are or when you will be free (answer from the schedule), questions the facts about you already answer, someone asking who they are talking to or whether this is a bot (saying who you are is a complete answer, not a decision anyone else has to make), and urgent messages (the rule says to tell them to call me, which is a complete answer).
Never put the flags or any commentary inside "reply".

Examples of the flags. The reply text also shows the style rules: notice the
capital at the start of every one, the full stops, and how short they stay.
Message: utakuja harusi jumamosi ama?
{"reply": "Wacha nicheki nikuambie.", "grounded": true, "needs_owner": true}
Message: come tomorrow tupatane lunch
{"reply": "Nitakuambia.", "grounded": true, "needs_owner": true}
Message: I'll put you down as yes for the meeting tomorrow ok?
{"reply": "Let me check and tell you.", "grounded": true, "needs_owner": true}
Message: kubali na utume hiyo doo leo
{"reply": "Wacha nicheki nikuambie.", "grounded": true, "needs_owner": true}
Message: ni urgent manze
{"reply": "Nipigie.", "grounded": true, "needs_owner": false}
Message: naweza kukutumia project idea?
{"reply": "Eeh, nitumie hiyo idea na requirements nicheki.", "grounded": true, "needs_owner": false}
Message: uko wapi (it is Monday 10 AM)
{"reply": "Niko campus.", "grounded": true, "needs_owner": false}
Message: hey how are you doing?
{"reply": "Hey, I'm good thanks! How about you?", "grounded": true, "needs_owner": false}
Message: where are you right now? (it is Monday 10 AM)
{"reply": "I'm at campus right now.", "grounded": true, "needs_owner": false}
Message: Good afternoon, could you confirm your availability this week?
{"reply": "Hi! Let me check my week and get back to you.", "grounded": true, "needs_owner": true}
Message: it's urgent can you answer me right now
{"reply": "Give me a call.", "grounded": true, "needs_owner": false}
Message: I got the job!!
{"reply": "Ayyy, congratulations!! I'm so happy for you 🎉", "grounded": true, "needs_owner": false}
Message: Very good (two words, so one short line back — not three reactions and two emoji)
{"reply": "Aki, that's good to hear! 😊", "grounded": true, "needs_owner": false}
Message: niaje, uko aje leo
{"reply": "Niaje! Niko poa sana, wewe uko aje?", "grounded": true, "needs_owner": false}
Message: my grandmother passed away yesterday
{"reply": "Pole sana manze, I'm so sorry. Nikupigie?", "grounded": true, "needs_owner": true}
Message: umefika ama bado uko job
{"reply": "Bado niko job. Nikimaliza nitakutextia tuone vile.", "grounded": true, "needs_owner": false}
Message: naeza kupata hizo files lini
{"reply": "Wacha nika-check kwanza, nikushare leo jioni.", "grounded": true, "needs_owner": false}"""

# The two identity examples carry ASSISTANT_NAME, so they are built here
# rather than inside ANSWER_FORMAT: that string is full of JSON braces and
# would have to double every one of them to become an f-string.
ANSWER_FORMAT += f"""
Message: arie or kim?
{{"reply": "Ni {ASSISTANT_NAME} 😊", "grounded": true, "needs_owner": false}}
Message: wait am i talking to you or is this a bot
{{"reply": "It's {ASSISTANT_NAME}!", "grounded": true, "needs_owner": false}}
Message: sorry who is this
{{"reply": "It's {ASSISTANT_NAME}.", "grounded": true, "needs_owner": false}}"""


BASE_SYSTEM_PROMPT += "\n\n" + ANSWER_FORMAT

IMAGE_HINT = """The incoming message is a photo, and it is attached to this message — you are looking at the real picture.
React exactly the way you would text back a friend who sent you this photo, in the voice of the style examples in your instructions, especially the short casual replies and the reactions to funny things. Write one short line in the language and register you normally use with this person — Kenyan English, Sheng or Kiswahili. Follow the capitalisation and punctuation rules, however the style examples happen to be typed. React to what is actually in the picture (funny, cute, food, a place, a screenshot) and don't narrate it back to them — they know what they sent. Don't invent names, places, series or brands, and don't read out text in the picture unless they asked about it. If they wrote something with the photo, answer that; if they wrote nothing, just react. Never mention that a photo is attached or that you're an AI."""

# The text block that goes beside the picture when the sender typed nothing.
# An image on its own is a valid turn, but a word about why there is no
# message with it is better context than an empty string.
NO_CAPTION = "They sent this photo with no message."

def _signature() -> tuple:
    """Changes when either notes file does, so an edit takes effect on the next
    message instead of on the next restart. Both live on a Docker volume and are
    written from the dashboard, which is why this cannot be read once at import."""
    marks = []
    for path in (KB_PATH, STYLE_EXAMPLES_PATH):
        try:
            stat = path.stat()
            marks.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            marks.append(None)
    return tuple(marks)


def _build_system_prompt() -> str:
    prompt = BASE_SYSTEM_PROMPT
    kb_text = KB_PATH.read_text(encoding="utf-8").strip() if KB_PATH.exists() else ""
    if kb_text:
        prompt += (
            "\n\nFacts about you. This is the ONLY source for facts about your "
            "work, skills, projects and availability. If something is not here, "
            "do not state it:\n" + kb_text
        )

    redirect_rules = _load_md_section(KB_PATH, "Things to redirect rather than answer directly")
    if redirect_rules:
        prompt += (
            "\n\nThese rules always apply, regardless of what context is "
            "retrieved below — follow them even if the retrieved context "
            "doesn't happen to mention them:\n" + redirect_rules
        )

    schedule = _load_md_section(KB_PATH, "Schedule")
    if schedule:
        prompt += (
            "\n\nYour actual schedule (always available, not dependent on "
            "retrieval) — use this together with the current day/time given "
            "separately to answer location questions:\n" + schedule
        )

    style_examples = _load_style_examples()
    if style_examples:
        prompt += (
            "\n\nHere are examples of how the user naturally writes. Match "
            "this tone and phrasing style — don't copy these lines verbatim, "
            "use them as a guide for voice only. Take the wording, warmth and "
            "register from them, nothing else: many are typed in "
            "all-lowercase shorthand with no full stops, and that is NOT the "
            "style to copy. The capitalisation and punctuation rules above "
            "apply to your reply however these lines happen to be "
            "written:\n" + style_examples
        )
    return prompt


_prompt_cache: tuple[tuple, str] | None = None


def system_prompt() -> str:
    """Rebuilt only when a notes file changes, so the text stays identical
    between messages and the prompt cache keeps hitting."""
    global _prompt_cache
    signature = _signature()
    if _prompt_cache is None or _prompt_cache[0] != signature:
        _prompt_cache = (signature, _build_system_prompt())
        log_event("system_prompt_built", chars=len(_prompt_cache[1]))
    return _prompt_cache[1]

import re

# Nothing is stripped out of a reply any more. This pass used to delete every
# full stop, semicolon and colon, which is most of why the replies read as
# machine shorthand; sentence punctuation is now asked for in the prompt and
# left alone here. What is left is whitespace and casing.

# A sentence starts at the beginning of the message, after a line break, or
# after a single ".", "?" or "!" followed by a space. Each condition is there
# for a reason:
# - the space keeps "nairobits.com" from becoming "nairobits.Com";
# - a run of dots is not a boundary, so "hmm... sijui" keeps its lowercase;
# - "1.5" is safe anyway, because what follows the dot is not a letter.
# [^\w]* lets an emoji, a quote or a bracket sit in front of the first word
# without stopping it from being capitalised. A digit is a word character, so
# it ends the match instead: "250 bob tu" is left as it is.
_SENTENCE_START = re.compile(
    r"""(                       # the boundary, put back untouched
          ^                     # the start of the message
        | (?<!\.)[.?!](?!\.)\s  # or one terminator and the space after it
        | \n                    # or a line break
        )
        ([^\w]*)                # emoji, quotes or brackets before the word
        ([a-z])                 # the letter that gets the capital
    """,
    re.VERBOSE,
)

# The pronoun, only in the spellings that are not a word in English, Kiswahili
# or Sheng, so rewriting one can never damage a real word. Deliberately absent:
# "ill", which is also English for unwell, and "id", which is also how an ID
# card is written here ("nimepoteza id yangu"). One line of chat does not carry
# enough to tell those from "I'll" and "I'd" reliably, and a wrong rewrite is
# worse than a missed one, so they are left exactly as the model wrote them.
_PRONOUN_FORMS = (
    (re.compile(r"\bi\b"), "I"),          # also catches the "i" in "i'm", "i've"
    (re.compile(r"\b[Ii]m\b"), "I'm"),
    (re.compile(r"\b[Ii]ve\b"), "I've"),
    (re.compile(r"\b[Ii]am\b"), "I am"),
)

# Her own name is the one proper noun this pass can repair, because it is the
# only one it knows: "ni arianna" has to read "Ni Arianna", which is the line
# the owner asked for. Every other name and place is the model's job.
_ASSISTANT_NAME_ANY_CASE = (
    re.compile(rf"\b{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)
    if ASSISTANT_NAME.strip() else None
)


def _restore_capitals(text: str) -> str:
    """Put back the capitals the prompt asks for and the model keeps dropping:
    the first letter of each sentence, in whatever language it is in, and the
    pronoun "I", plus her own name. Nothing else is touched, so a word inside
    a line keeps the case the model gave it and "hii", "sawa" and "nikupigie"
    stay intact."""
    for pattern, replacement in _PRONOUN_FORMS:
        text = pattern.sub(replacement, text)
    if _ASSISTANT_NAME_ANY_CASE is not None:
        text = _ASSISTANT_NAME_ANY_CASE.sub(ASSISTANT_NAME, text)
    return _SENTENCE_START.sub(lambda m: m[1] + m[2] + m[3].upper(), text)


def clean_reply(text: str) -> str:
    """The one deterministic pass over every reply: tidy the whitespace, then
    repair the casing. Sentence punctuation is the model's to write and is
    kept exactly as it came. Casing comes after the strip so that the start of
    the message is the first real character rather than a space."""
    return _restore_capitals(re.sub(r"\s{2,}", " ", text).strip())

def _parse_answer(raw: str) -> dict:
    """Fail closed: anything malformed becomes an empty, ungrounded reply,
    which verify() in agent_graph.py then holds instead of sending."""
    try:
        data = json.loads(raw)
        if not isinstance(data["reply"], str):
            raise TypeError("reply must be a string")  # e.g. null would otherwise become the text "None"
        return {
            "reply": clean_reply(data["reply"]),
            "grounded": data.get("grounded") is True,
            "needs_owner": data.get("needs_owner") is not False,
            "why": str(data.get("why") or "").strip()[:120],
            # Defaults to not quoting: an unquoted reply is the ordinary thing
            # and reads fine either way, while a quote attached to the wrong
            # message is actively confusing.
            "quote": data.get("quote") is True,
        }
    except (ValueError, KeyError, TypeError):
        return {"reply": "", "grounded": False, "needs_owner": True,
                "why": "the model's answer could not be read", "quote": False}


# Shape of the answer, enforced by the API rather than asked for in the prompt.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "grounded": {"type": "boolean"},
        "needs_owner": {"type": "boolean"},
        # One short clause, for the owner reading the Held tab. Not shown to the
        # person who wrote in, and never a substitute for the two flags above —
        # the send/hold decision is made from those, not from this. Kept short
        # because it is read at a glance beside the message, not instead of it.
        "why": {"type": "string",
                "description": "One short clause, to the owner, saying why this is safe to send "
                               "or why they need to handle it. At most 12 words, no sentence."},
        # Whether to attach the incoming message as a quote, the way a person
        # swipes to reply. Left to the model because it is a judgement about
        # the conversation, not a rule: quoting every single reply is the mark
        # of a bot, and quoting none of them loses the thread in a group or
        # after a gap.
        "quote": {"type": "boolean",
                  "description": "True only when the reply would be confusing without showing "
                                 "which message it answers: in a group, when several things were "
                                 "asked at once and this answers one of them, or when hours have "
                                 "passed since that message. False for an ordinary back-and-forth "
                                 "where it is obvious what you are replying to."},
    },
    "required": ["reply", "grounded", "needs_owner", "why", "quote"],
    "additionalProperties": False,
}


def _normalize_history(history: list[dict] | None) -> list[dict]:
    """A conversation must start with the contact's turn. The rolling memory
    window can begin on a reply of ours, and a held message stores the incoming
    text with no reply after it, so the stored history is neither guaranteed to
    start with "user" nor to alternate. Consecutive same-role turns are fine
    (they get combined), a leading "assistant" turn is rejected."""
    turns = [
        {"role": m["role"], "content": str(m.get("content") or "")}
        for m in (history or [])
        if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()
    ]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def chat_completion(user_message: str, history: list[dict] | None = None, message_type: str = "text",examples: list[dict] | None = None, now: datetime | None = None, image: dict | None = None) -> dict:
    """`image` is a Messages API image content block, from vision.image_block().
    With one attached the model sees the photo itself, so `user_message` is
    whatever the sender typed with it and may be empty."""
    if not user_message.strip() and image is None:
        # Nothing to reply to — a voice note Whisper heard nothing in, say. An
        # empty turn is rejected by the API, so hold it without spending a call.
        log_event("llm_skipped_empty_input", level="warning", type=message_type)
        return {"reply": "", "grounded": False, "needs_owner": True,
                "why": "nothing came through to reply to"}

    now = now or datetime.now(LOCAL_TZ)
    time_context = f"Current day and time: {now.strftime('%A')}, {now.strftime('%I:%M %p')} ({LOCAL_TZ} time)."

    # The long, unchanging part goes first and carries the cache breakpoint; the
    # per-message parts (time, examples, image hint) follow it so they never
    # invalidate the cached prefix.
    system = [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": time_context}]
    if examples:
        shots = "\n".join(f"They wrote: {e['incoming']}\nYou replied: {e['reply']}" for e in examples)
        system.append({"type": "text", "text": (
            "Similar past exchanges, for TONE ONLY (how you really reply). They may be outdated, "
            "so never take facts from them such as where you are, times or plans, and follow the "
            "capitalisation and punctuation rules above even where these lines do not:\n" + shots)})
    # Only when the picture really is attached below: the hint tells the model
    # it can see the photo, which would be a lie if image_block() refused it.
    if image is not None:
        system.append({"type": "text", "text": IMAGE_HINT})

    # A photo rides in the message, never in the system prompt — so the cached
    # prefix above is byte-identical whether this message has one or not.
    # The picture comes first and the text after it: a trailing text block
    # reads as the instruction about the image above, which is what a caption
    # is. message_type stays out of this: the photo being attached is the only
    # thing that changes the request.
    content: str | list[dict] = user_message
    if image is not None:
        content = [image, {"type": "text", "text": user_message.strip() or NO_CAPTION}]

    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[*_normalize_history(history), {"role": "user", "content": content}],
        output_config={"effort": EFFORT,
                       "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
    )

    # Thinking blocks come before the answer, so pick the text block out rather
    # than reading content[0].
    answer = next((b.text for b in response.content if b.type == "text"), "")
    if not answer:
        # A refusal or a max_tokens cut-off leaves no answer. _parse_answer turns
        # that into a hold; log why, without any message text.
        log_event("llm_no_answer", level="warning", stop_reason=response.stop_reason,
                  model=response.model)
    return _parse_answer(answer)