"""A photo is answered by a model that can see it.

Until now a ~1 GB BLIP captioner turned the picture into one English sentence
and the reply model reacted to that sentence, in a system whose whole point is
replying in Kiswahili, Sheng and Kenyan English. The photo now travels to the
model as an image block, so these tests are about the request that goes out:
that the picture is in it, that the caption the sender typed goes with it, and
that anything unsendable is held rather than guessed at.

The Anthropic client is a stand-in — no network, no API key, one fixed answer.
"""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import agent_graph, llm, vision

CHAT = "254712345678@c.us"

# What the model "replies": grounded and not needing the owner, so a photo that
# goes through comes back as "send" and one that is held is held for a reason
# this file created rather than one the fixture smuggled in.
ANSWER = {"reply": "Ayyy manze hii ni fiti.", "grounded": True,
          "needs_owner": False, "why": "a reaction to a photo", "quote": False}


def photo(width: int = 64, height: int = 64, fmt: str = "JPEG") -> str:
    """A real encoded image, base64 the way the bridge sends it."""
    buffer = io.BytesIO()
    # A gradient rather than a flat colour: a flat image compresses to almost
    # nothing, which would make the downscale test prove nothing.
    image = Image.new("RGB", (width, height))
    image.putdata([(x * 7 % 256, y * 11 % 256, (x + y) % 256)
                   for y in range(height) for x in range(width)])
    image.save(buffer, format=fmt)
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture()
def agent(monkeypatch):
    requests = []

    class FakeMessages:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(ANSWER))],
                stop_reason="end_turn", model="fake-model")

    monkeypatch.setattr(llm, "_client", SimpleNamespace(messages=FakeMessages()))
    # No stored history and no reaching for the bridge to seed any: this file
    # is about one message's request, not about memory.
    monkeypatch.setattr(agent_graph, "get_history", lambda contact_id: [])
    monkeypatch.setattr(agent_graph, "seed_from_whatsapp", lambda contact_id: 0)

    def run(message_type="image", raw_text="", media_base64=None, mimetype=None):
        return agent_graph.run_agent(contact_id=CHAT, message_type=message_type,
                                     raw_text=raw_text, media_base64=media_base64,
                                     mimetype=mimetype)

    return SimpleNamespace(run=run, requests=requests)


def sent_content(agent):
    """The content of the one user turn that went to the model."""
    assert len(agent.requests) == 1, "the model was called more or less than once"
    return agent.requests[0]["messages"][-1]["content"]


def test_a_photo_is_sent_to_the_model_as_an_image_it_can_see(agent):
    state = agent.run(media_base64=photo(), mimetype="image/jpeg")

    image = sent_content(agent)[0]
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/jpeg"
    # The real bytes, not a description of them.
    assert base64.b64decode(image["source"]["data"])[:3] == b"\xff\xd8\xff"
    assert state["action"] == "send"


def test_the_media_type_is_the_photos_own_and_not_hardcoded_jpeg(agent):
    agent.run(media_base64=photo(fmt="PNG"), mimetype="image/png")

    image = sent_content(agent)[0]
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_mimetype_the_bridge_did_not_send_is_read_off_the_bytes(agent):
    """app/main.py does not pass mimetype to run_agent yet. A photo must still
    be answered, with the media type the picture itself says it is."""
    agent.run(media_base64=photo(fmt="PNG"), mimetype=None)

    assert sent_content(agent)[0]["source"]["media_type"] == "image/png"


def test_the_caption_the_sender_typed_goes_with_the_photo(agent):
    agent.run(media_base64=photo(), mimetype="image/jpeg", raw_text="check this out")

    content = sent_content(agent)
    assert content[1] == {"type": "text", "text": "check this out"}
    # Picture first, caption after it: the text reads as the instruction about
    # the image above.
    assert content[0]["type"] == "image"


def test_a_photo_with_no_caption_says_so_instead_of_sending_an_empty_turn(agent):
    agent.run(media_base64=photo(), mimetype="image/jpeg", raw_text="")

    content = sent_content(agent)
    assert content[1]["text"] == llm.NO_CAPTION
    assert content[1]["text"].strip(), "an empty turn is rejected by the API"


def test_a_large_photo_is_downscaled_before_it_is_sent(agent):
    arrived = photo(3000, 2000)
    agent.run(media_base64=arrived, mimetype="image/jpeg")

    data = sent_content(agent)[0]["source"]["data"]
    with Image.open(io.BytesIO(base64.b64decode(data))) as shrunk:
        assert max(shrunk.size) == vision.MAX_EDGE
        assert shrunk.size == (vision.MAX_EDGE, 1045)      # the aspect ratio is kept
    assert len(data) < len(arrived), "the photo went out no smaller than it arrived"


def test_a_photo_already_small_enough_is_sent_exactly_as_it_arrived(agent):
    arrived = photo(800, 600)
    agent.run(media_base64=arrived, mimetype="image/jpeg")

    # No decode and no re-encode when there is nothing to gain by it.
    assert sent_content(agent)[0]["source"]["data"] == arrived


def test_a_mimetype_the_api_cannot_take_is_held_and_never_sent(agent):
    state = agent.run(media_base64=photo(), mimetype="image/heic", raw_text="nice ndio?")

    assert agent.requests == [], "a format the API rejects was sent anyway"
    assert state["action"] == "hold"
    assert state["reply_text"] == ""
    assert "image/heic" in state["why"]


def test_a_photo_whose_bytes_are_not_a_format_the_api_takes_is_held(agent):
    """The declared mimetype is a claim. These bytes really are a BMP, which
    the Messages API does not accept, however the message labelled them."""
    state = agent.run(media_base64=photo(fmt="BMP"), mimetype="image/jpeg")

    assert agent.requests == []
    assert state["action"] == "hold"
    assert "bmp" in state["why"]


def test_an_undecodable_photo_is_held_with_a_reason_the_owner_can_read(agent):
    state = agent.run(media_base64=base64.b64encode(b"this is not a photo").decode(),
                      mimetype="image/jpeg")

    assert agent.requests == []
    assert state["action"] == "hold"
    assert state["why"] == "the photo didn't come through in one piece"
    assert state["needs_owner"] and not state["grounded"]


def test_an_oversized_photo_is_held_before_anything_decodes_it(agent, monkeypatch):
    # The real ceiling is 16 MiB of base64; lowered here so the test does not
    # have to build 16 MiB to prove the check runs before the decode.
    monkeypatch.setattr(vision, "MAX_BASE64_CHARS", 128)
    state = agent.run(media_base64=photo(200, 200), mimetype="image/jpeg")

    assert agent.requests == []
    assert state["action"] == "hold"
    assert state["why"] == "the photo is too big to send to the model"


def test_what_is_stored_for_a_photo_is_an_honest_placeholder(agent):
    """resolved_text is this message's row in the conversation memory, the
    message log and the Held tab. None of them can hold an image, and an
    invented description there would be read back later as the sender's own
    words."""
    with_caption = agent.run(media_base64=photo(), mimetype="image/jpeg",
                             raw_text="tulienda Naivasha")
    assert with_caption["resolved_text"] == "[Photo] Their message with it: tulienda Naivasha"

    bare = agent.run(media_base64=photo(), mimetype="image/jpeg")
    assert bare["resolved_text"] == "[Photo]"


def test_a_held_photo_still_leaves_the_owner_the_message_that_came_with_it(agent):
    state = agent.run(media_base64=photo(), mimetype="image/tiff", raw_text="hii ni yako?")

    assert state["action"] == "hold"

    assert state["resolved_text"] == "[Photo] Their message with it: hii ni yako?"


def test_a_text_message_sends_exactly_what_it_always_did(agent):
    """The image path must be unable to reach an ordinary reply."""
    agent.run(message_type="text", raw_text="niaje, uko aje leo")

    request = agent.requests[0]
    assert request["messages"][-1] == {"role": "user", "content": "niaje, uko aje leo"}
    assert "image" not in json.dumps(request["messages"])
    assert all(llm.IMAGE_HINT not in block["text"] for block in request["system"])


def test_a_voice_note_is_unaffected_by_the_image_path(agent):
    # tests/conftest.py's app.stt stub transcribes anything to "heard".
    agent.run(message_type="voice", media_base64=photo(), mimetype="audio/ogg")

    assert agent.requests[0]["messages"][-1] == {"role": "user", "content": "heard"}


def test_the_photo_hint_goes_in_the_message_not_the_cached_system_prompt(agent):
    agent.run(media_base64=photo(), mimetype="image/jpeg")

    system = agent.requests[0]["system"]
    # The long, unchanging block still carries the one cache breakpoint, and
    # still holds nothing that varies per message — an image in there would
    # mean paying for the whole prompt again on every photo.
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == llm.system_prompt()
    assert llm.IMAGE_HINT not in system[0]["text"]
    assert system[-1]["text"] == llm.IMAGE_HINT


def test_the_cache_breakpoint_is_identical_with_and_without_a_photo(agent):
    agent.run(message_type="text", raw_text="sawa")
    agent.run(media_base64=photo(), mimetype="image/jpeg")

    text_prefix, photo_prefix = (request["system"][0] for request in agent.requests)
    assert text_prefix == photo_prefix
