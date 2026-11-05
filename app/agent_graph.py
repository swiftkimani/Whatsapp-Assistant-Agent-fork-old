from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.llm import chat_completion
from app.log import anon, log_event
from app.memory import get_history, seed_from_whatsapp
from app.stt import transcribe_base64_audio
from app.style_retrieval import retrieve_examples
from app.vision import ImageUnusable, image_block


class AgentState(TypedDict):
    contact_id: str               # who this conversation is with (msg.from_)
    message_type: str             # "text" | "voice" | "image"
    raw_text: str                 # original text body, if any
    media_base64: str | None
    mimetype: str | None          # what the bridge read off the media, if anything
    image: dict | None            # the photo, as a block the model can look at
    image_problem: str            # "" | why that photo cannot be sent, for the owner
    resolved_text: str            # original-script text after STT, or the photo placeholder
    history: list[dict]           # this contact's rolling memory buffer
    reply_text: str
    grounded: bool                # every fact in the reply comes from the prompt
    needs_owner: bool             # only the owner can really answer this
    action: str                   # "send" | "hold"
    hold_reason: str              # "" | "needs_owner" | "not_grounded" | "empty_reply"
    why: str                      # the model's own one-line account, for the owner
    quote: bool                   # attach the incoming message as a quote


def classify_and_resolve(state: AgentState) -> AgentState:
    if state["message_type"] == "voice" and state["media_base64"]:
        state["resolved_text"] = transcribe_base64_audio(state["media_base64"])
    elif state["message_type"] == "image" and state["media_base64"]:
        # Nothing here describes the photo: the model is given the picture and
        # looks at it itself. resolved_text is bookkeeping — it is this
        # message's row in the conversation memory, the message log and the
        # Held tab, none of which can hold an image — so it says a photo
        # arrived and repeats only what the sender actually typed. A
        # description invented here would be read back later as their words.
        # Same convention as seed_from_whatsapp() in app/memory.py.
        state["resolved_text"] = "[Photo]"
        if state["raw_text"]:
            state["resolved_text"] += f" Their message with it: {state['raw_text']}"
        try:
            state["image"] = image_block(state["media_base64"], state["mimetype"])
        except ImageUnusable as err:
            # Fail closed, the way _parse_answer and verify() do: the owner
            # gets the message in the Held tab with the reason, and no reply is
            # guessed at a photo nobody could look at.
            state["image_problem"] = str(err)
            log_event("image_unusable", level="warning",
                      chat=anon(state["contact_id"]), reason=str(err))
    else:
        state["resolved_text"] = state["raw_text"]
    return state


def fetch_history(state: AgentState) -> AgentState:
    history = get_history(state["contact_id"])
    if not history:
        # Nothing stored yet, but WhatsApp has been holding this conversation
        # all along. Pull it once, so the first reply knows what was last said.
        seed_from_whatsapp(state["contact_id"])
        history = get_history(state["contact_id"])
    state["history"] = history
    return state


def generate_reply(state: AgentState) -> AgentState:
    if state["image_problem"]:
        # Held without spending an API call: there is nothing to reply to here
        # but a photo that could not be sent.
        state["reply_text"] = ""
        state["grounded"] = False
        state["needs_owner"] = True
        state["why"] = state["image_problem"][:120]
        state["quote"] = False
        return state

    # With a photo attached, what the model is asked about is the caption the
    # sender typed: resolved_text's "[Photo]" is for the owner's records, and
    # repeating it beside the picture tells the model nothing. The same string
    # is what the style lookup should match on, which is why it is one name.
    message = state["raw_text"] if state["image"] else state["resolved_text"]

    examples = []
    if message.strip():
        try:
            examples = retrieve_examples(message)
        except Exception as err:  # style is a bonus: never let it block a reply
            log_event("style_retrieval_skipped", level="warning", error=str(err))

    result = chat_completion(
        message,
        state["history"],
        message_type=state["message_type"],
        examples=examples,
        image=state["image"],
    )
    state["reply_text"] = result["reply"]
    state["grounded"] = result["grounded"]
    state["needs_owner"] = result["needs_owner"]
    state["why"] = result.get("why", "")
    state["quote"] = result.get("quote", False)
    return state


def verify(state: AgentState) -> AgentState:
    if state["needs_owner"]:
        reason = "needs_owner"
    elif not state["grounded"]:
        reason = "not_grounded"
    elif not state["reply_text"]:
        reason = "empty_reply"
    else:
        reason = ""
    state["hold_reason"] = reason
    state["action"] = "hold" if reason else "send"
    # no message text in logs: only flags, sizes and an anonymised chat id
    log_event("verify", chat=anon(state["contact_id"]), type=state["message_type"],
              grounded=state["grounded"], needs_owner=state["needs_owner"],
              action=state["action"], reason=reason, chars_in=len(state["resolved_text"]),
              chars_out=len(state["reply_text"]))
    return state


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("classify_and_resolve", classify_and_resolve)
    graph.add_node("fetch_history", fetch_history)
    graph.add_node("generate_reply", generate_reply)
    graph.add_node("verify", verify)

    graph.set_entry_point("classify_and_resolve")
    graph.add_edge("classify_and_resolve", "fetch_history")
    graph.add_edge("fetch_history", "generate_reply")
    graph.add_edge("generate_reply", "verify")
    graph.add_edge("verify", END)

    return graph.compile()


_compiled_graph = None


def run_agent(contact_id: str, message_type: str, raw_text: str, media_base64: str | None,
              mimetype: str | None = None) -> AgentState:
    """`mimetype` is the media type the bridge read off the WhatsApp message
    (IncomingMessage.mimetype in app/main.py). It is optional because the photo
    is also identified from its own bytes, so a caller that does not pass it
    still gets an answered photo — it only loses the earlier, cheaper check on
    a format the API cannot take."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()

    initial_state: AgentState = {
        "contact_id": contact_id,
        "message_type": message_type,
        "raw_text": raw_text,
        "media_base64": media_base64,
        "mimetype": mimetype,
        "image": None,
        "image_problem": "",
        "resolved_text": "",
        "history": [],
        "reply_text": "",
        "grounded": False,
        "needs_owner": True,
        "action": "hold",
        "hold_reason": "",
        "why": "",
        "quote": False,
    }
    return _compiled_graph.invoke(initial_state)