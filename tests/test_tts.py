from app.tts import MAX_SPEECH_CHARS, clean_for_speech, pick_voice


def test_swahili_gets_the_swahili_voice_and_english_gets_kenyan_english(monkeypatch):
    # Explicitly unset, so this tests the built-in defaults rather than whatever
    # .env happens to hold on the machine running the suite.
    monkeypatch.delenv("DASHBOARD_VOICE_SW", raising=False)
    monkeypatch.delenv("DASHBOARD_VOICE_EN", raising=False)
    assert pick_voice("niaje uko wapi") == "sw-KE-ZuriNeural"
    assert pick_voice("on my way") == "en-KE-AsiliaNeural"
    # "sawa" and "poa" sit inside ordinary English sentences here, and the
    # assistant writes them itself, so one of them is not evidence of Swahili.
    # This used to assert the opposite and locked in an English reply being
    # read aloud by the Swahili voice.
    assert pick_voice("sure sawa that works for me") == "en-KE-AsiliaNeural"
    assert pick_voice("sawa nitakuambia baadaye") == "sw-KE-ZuriNeural"


def test_voice_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DASHBOARD_VOICE_SW", "sw-KE-RafikiNeural")
    assert pick_voice("niaje") == "sw-KE-RafikiNeural"


def test_emoji_and_markdown_are_not_read_out():
    assert clean_for_speech("**sawa** 😂😂 fiti") == "sawa fiti"
    assert clean_for_speech("  # hi `there`  ") == "hi there"


def test_long_replies_are_cut_at_a_word_boundary():
    out = clean_for_speech("neno " * 400)
    assert 0 < len(out) <= MAX_SPEECH_CHARS and out.endswith("neno")


def test_tatweel_is_removed():
    assert clean_for_speech("جاري الارسال لـMom") == "جاري الارسال لMom"


def test_provider_defaults_to_the_free_voice(monkeypatch):
    from app.tts import provider
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    assert provider() == "edge"
    monkeypatch.setenv("TTS_PROVIDER", " OpenAI ")
    assert provider() == "openai"
