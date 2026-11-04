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
you naturally would when someone calls you, like "niaje", "vipi" or
"yes?" — do NOT reply with a formal greeting like "Hello {USER_NAME}!".

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

Be very friendly and very warm — this is the most important thing about how
you sound. You are genuinely glad to hear from this person. React before you
answer: congratulate good news, say pole for bad news, show you are actually
interested. Short is still right — a line or two, like a real WhatsApp
message, never a paragraph — but short is not the same as cold. "im at campus"
is fine; "niko campus" on its own to a close friend is not. Emoji where you
would really use one, not as decoration on every line.

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

Punctuation style: no periods, no semicolons, no colons. "?" at the end of a
real question, "!" where you are genuinely warm or excited (one or two, not
five), and a comma only when a two-clause line really needs one. Lowercase is
normal and fine.

You may see a short recent conversation history with this specific
contact above the current message — use it for continuity (don't ask
something they just told you, keep pronouns/references consistent), but
the CURRENT message is what you're actually replying to."""

ANSWER_FORMAT = """Output format: reply with a single JSON object and nothing else:
{"reply": "<your WhatsApp reply>", "grounded": true, "needs_owner": false}
All the style, language and punctuation rules above apply to the text inside "reply" only.
- "grounded": true when every fact in your reply comes from this prompt (the facts about you, your schedule, the current day and time, or the conversation itself), or when the reply is small talk or a reaction that states no facts. false if you had to guess any fact about the owner.
- "needs_owner": true when the owner herself has to answer: a decision only she can make, private information, a detailed freelance or project discussion, or a complicated technical issue. Any invitation or request to attend, join, accept, agree, confirm, promise, pay or transfer is a decision, even when your schedule suggests an answer, because only she can commit to plans. Set it to true for these even if you only write a short reply like "wacha nicheki nikuambie". If you are unsure whether something is a decision, choose true.
- "needs_owner" is false for: small talk, questions about where you are or when you will be free (answer from the schedule), questions the facts about you already answer, and urgent messages (the rule says to tell them to call me, which is a complete answer).
Never put the flags or any commentary inside "reply".

Examples of the flags (the reply text follows the style rules):
Message: utakuja harusi jumamosi ama?
{"reply": "wacha nicheki nikuambie", "grounded": true, "needs_owner": true}
Message: come tomorrow tupatane lunch
{"reply": "nitakuambia", "grounded": true, "needs_owner": true}
Message: I'll put you down as yes for the meeting tomorrow ok?
{"reply": "let me check and tell you", "grounded": true, "needs_owner": true}
Message: kubali na utume hiyo doo leo
{"reply": "wacha nicheki nikuambie", "grounded": true, "needs_owner": true}
Message: ni urgent manze
{"reply": "nipigie", "grounded": true, "needs_owner": false}
Message: naweza kukutumia project idea?
{"reply": "eeh nitumie hiyo idea na requirements nicheki", "grounded": true, "needs_owner": false}
Message: uko wapi (it is Monday 10 AM)
{"reply": "niko campus", "grounded": true, "needs_owner": false}
Message: hey how are you doing?
{"reply": "hey im good thanks!! how about you", "grounded": true, "needs_owner": false}
Message: where are you right now? (it is Monday 10 AM)
{"reply": "im at campus right now", "grounded": true, "needs_owner": false}
Message: Good afternoon, could you confirm your availability this week?
{"reply": "hi! let me check my week and get back to you", "grounded": true, "needs_owner": true}
Message: it's urgent can you answer me right now
{"reply": "give me a call", "grounded": true, "needs_owner": false}
Message: I got the job!!
{"reply": "ayyy congratulations!! im so happy for you 🎉", "grounded": true, "needs_owner": false}
Message: niaje, uko aje leo
{"reply": "niaje! niko poa sana wewe uko aje", "grounded": true, "needs_owner": false}
Message: my grandmother passed away yesterday
{"reply": "pole sana manze, im so sorry. nikupigie?", "grounded": true, "needs_owner": true}
Message: umefika ama bado uko job
{"reply": "bado niko job, nikimaliza nitakutextia tuone vile", "grounded": true, "needs_owner": false}
Message: naeza kupata hizo files lini
{"reply": "wacha nika-check kwanza nikushare leo jioni", "grounded": true, "needs_owner": false}"""


BASE_SYSTEM_PROMPT += "\n\n" + ANSWER_FORMAT

IMAGE_HINT = """The incoming message is a photo. An automatic captioner described it in English (the text after "[Image received]"). Treat that as what you are looking at.
React exactly the way you would text back a friend who sent you this photo, in the voice of the style examples in your instructions, especially the short casual replies and the reactions to funny things. Write one short line in the register you would normally use with this person — Kenyan English, Sheng or Kiswahili. Follow the punctuation rule. Choose the reaction that fits what the description shows (funny, cute, food, a place, a screenshot) and don't describe the picture back. Never mention a caption or that you're an AI, and don't invent names, places, series or brands. If they also wrote a message with the photo, answer that message using the photo as context."""

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
            "use them as a guide for voice only:\n" + style_examples
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

# "!" and "," are left alone now. Stripping them was the one place this rule
# removed warmth without adding authenticity: real Nairobi texting drops full
# stops and keeps exclamation marks. The digit lookarounds keep the colon in
# "6:30". The Arabic comma is a leftover from the persona this replaced.
_PUNCT = re.compile(r"(?<!\d)[.;:،]|[.;:،](?!\d)")


def clean_reply(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _PUNCT.sub("", text)).strip()

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
            "why": str(data.get("why") or "").strip()[:200],
        }
    except (ValueError, KeyError, TypeError):
        return {"reply": "", "grounded": False, "needs_owner": True,
                "why": "the model's answer could not be read"}


# Shape of the answer, enforced by the API rather than asked for in the prompt.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "grounded": {"type": "boolean"},
        "needs_owner": {"type": "boolean"},
        # One short line, for the owner reading the Held tab. Not shown to the
        # person who wrote in, and never a substitute for the two flags above —
        # the send/hold decision is made from those, not from this.
        "why": {"type": "string",
                "description": "One short line, to the owner, saying why this is safe to send "
                               "or why they need to handle it. No more than 20 words."},
    },
    "required": ["reply", "grounded", "needs_owner", "why"],
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


def chat_completion(user_message: str, history: list[dict] | None = None, message_type: str = "text",examples: list[dict] | None = None, now: datetime | None = None) -> dict:
    if not user_message.strip():
        # Nothing to reply to — a voice note Whisper heard nothing in, say. An
        # empty turn is rejected by the API, so hold it without spending a call.
        log_event("llm_skipped_empty_input", level="warning")
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
            "so never take facts from them such as where you are, times or plans:\n" + shots)})
    if message_type == "image":
        system.append({"type": "text", "text": IMAGE_HINT})

    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[*_normalize_history(history), {"role": "user", "content": user_message}],
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