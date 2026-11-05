"""How a reply is written: capitals, the pronoun "I", and punctuation left alone.

The owner read a real exchange off his own phone — "im good thanks!! how about
you" — and said the assistant still reads as a machine because of the casing.
clean_reply() is the one pass every reply goes through, so the repair lives
there and not only in the prompt, which the model can always ignore.
"""
from app.llm import ASSISTANT_NAME, BASE_SYSTEM_PROMPT, clean_reply


def test_the_transcript_line_the_owner_complained_about_is_repaired():
    assert clean_reply("im good thanks!! how about you") == "I'm good thanks!! How about you"


def test_the_pronoun_i_is_always_capitalised():
    assert clean_reply("nilimwambia i will come") == "Nilimwambia I will come"
    assert clean_reply("i think i can make it") == "I think I can make it"


def test_the_pronoun_contractions_are_spelt_out():
    assert clean_reply("ok im coming") == "Ok I'm coming"
    assert clean_reply("ive told her already") == "I've told her already"
    assert clean_reply("iam almost there") == "I am almost there"
    # An apostrophe the model did type is kept; only the capital is added.
    assert clean_reply("sawa i'm on my way") == "Sawa I'm on my way"


def test_the_english_word_ill_and_the_noun_id_are_left_alone():
    """Deliberately not rewritten to "I'll" and "I'd": "ill" is also English
    for unwell and "id" is also an ID card here. A wrong rewrite would be
    worse than a missed one, so only the sentence capital is applied."""
    assert clean_reply("im feeling ill today") == "I'm feeling ill today"
    assert clean_reply("nimepoteza id yangu") == "Nimepoteza id yangu"


def test_a_sheng_word_is_not_capitalised_mid_sentence():
    assert clean_reply("hii ni fiti sawa poa nikupigie") == "Hii ni fiti sawa poa nikupigie"


def test_the_first_word_is_capitalised_in_any_language():
    assert clean_reply("niaje buda") == "Niaje buda"
    assert clean_reply("pole sana manze") == "Pole sana manze"


def test_a_reply_starting_with_an_emoji_still_capitalises_its_first_word():
    assert clean_reply("😊 niaje bro") == "😊 Niaje bro"


def test_text_with_no_latin_letter_to_repair_comes_back_as_it_went_in():
    """The repair only ever replaces an ASCII lowercase letter, so text that
    carries none of them survives it whole: a reply that is nothing but emoji,
    and a Kiswahili line already written the way the owner would write it.
    Non-Latin characters are not the casing pass's business and it may not
    insert, drop or re-case one."""
    assert clean_reply(" 👍👍 ") == "👍👍"
    assert clean_reply("Sawa, nitakuambia baadaye") == "Sawa, nitakuambia baadaye"
    assert clean_reply("Ng'ombe wa shamba amepotea. Nitakupigia") == "Ng'ombe wa shamba amepotea. Nitakupigia"


def test_a_sentence_after_a_question_mark_starts_with_a_capital():
    assert clean_reply("uko wapi? niko campus") == "Uko wapi? Niko campus"


def test_a_sentence_after_a_full_stop_starts_with_a_capital():
    assert clean_reply("im good. how about you") == "I'm good. How about you"


def test_a_line_break_starts_a_new_sentence():
    assert clean_reply("niaje\nuko aje leo") == "Niaje\nUko aje leo"


def test_sentence_punctuation_survives():
    """The old pass deleted every full stop, semicolon and colon, which is
    most of why the replies read as shorthand. The model writes the
    punctuation now and this keeps its hands off it."""
    assert clean_reply("I'm good, thanks. How about you?") == "I'm good, thanks. How about you?"


def test_a_time_keeps_its_colon():
    assert clean_reply("tuonane 6:30 kwa stage") == "Tuonane 6:30 kwa stage"


def test_a_domain_name_is_not_capitalised_at_its_dot():
    """A full stop only starts a sentence when a space follows it."""
    assert clean_reply("check nairobits.com for that") == "Check nairobits.com for that"


def test_a_run_of_dots_does_not_start_a_sentence():
    assert clean_reply("hmm... sijui") == "Hmm... sijui"


def test_a_sentence_that_opens_on_a_number_is_left_alone():
    assert clean_reply("250 bob tu") == "250 bob tu"
    assert clean_reply("1.5 hours tu") == "1.5 hours tu"


def test_stray_whitespace_is_still_tidied():
    assert clean_reply("  niko   njiani  ") == "Niko njiani"


def test_her_own_name_is_capitalised_wherever_it_lands():
    """The answer the owner asked for is "Ni Arianna", so the name has to read
    as a name even when the model types it in lowercase."""
    lower = ASSISTANT_NAME.lower()
    assert clean_reply(f"ni {lower} 😊") == f"Ni {ASSISTANT_NAME} 😊"
    assert clean_reply(f"it's {lower}, si unajua") == f"It's {ASSISTANT_NAME}, si unajua"


def test_the_prompt_says_who_the_assistant_is():
    """"arie or kim?" went unanswered because the prompt never named her."""
    assert ASSISTANT_NAME in BASE_SYSTEM_PROMPT


def test_the_prompt_asks_for_capitals_instead_of_blessing_lowercase():
    assert "Lowercase is normal and fine" not in BASE_SYSTEM_PROMPT
    assert "starts with a capital" in BASE_SYSTEM_PROMPT
