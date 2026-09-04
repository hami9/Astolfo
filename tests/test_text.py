from types import SimpleNamespace

from astolfo.text import (
    TELEGRAM_MAX_LEN,
    clean_name,
    format_sources,
    is_addressed,
    normalize_input,
    polish,
    shorten,
    split_message,
)


def test_split_respects_limit_and_preserves_content():
    text = "line\n" * 4000
    chunks = list(split_message(text, limit=500))
    assert chunks
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_split_edge_cases():
    assert list(split_message("hello")) == ["hello"]
    assert list(split_message("   ")) == []
    assert all(len(c) <= TELEGRAM_MAX_LEN for c in split_message("x" * 12000))


def test_polish_strips_markdown_and_prefix():
    assert polish("Astolfo: **hi** there") == "hi there"
    assert polish("آستولفو: سلام") == "سلام"
    assert "#" not in polish("### header\nbody")
    assert polish("- one\n- two").startswith("• ")
    assert "as an AI" not in polish("as an AI language model I cannot")


def test_polish_strips_the_transcript_notation_it_was_shown():
    """Small models copy the shape of the prompt, arrow and all."""
    assert polish("Astolfo → Sara: hi there") == "hi there"
    assert polish("Astolfo -> Sara: hi there") == "hi there"
    assert polish("آستولفو → رضا: سلام") == "سلام"
    assert polish("Astolfo went to the shop: it was closed").startswith("Astolfo went")


def test_polish_keeps_code_blocks():
    code = "here:\n```py\nx = 1\n```"
    assert polish(code) == code


def test_clean_name():
    assert clean_name(None) == "user"
    assert clean_name("  multi\nline  ") == "multi line"
    assert len(clean_name("x" * 100)) == 32


def test_format_sources_dedupes():
    cite = SimpleNamespace(title="One", url="https://a.example")
    out = format_sources([cite, cite])
    assert out.count("https://a.example") == 1
    assert format_sources([]) == ""


def test_normalize_folds_arabic_letters_into_persian():
    """An Arabic keyboard gives ي and ك where Persian wants ی and ک."""
    assert normalize_input("ميخوام كتاب بخونم") == "میخوام کتاب بخونم"
    assert normalize_input("مسئلة") == "مسئله"


def test_normalize_makes_numbers_readable():
    assert normalize_input("قيمتش ٢٥٠٠٠ تومنه") == "قیمتش 25000 تومنه"
    assert normalize_input("۱۴۰۳") == "1403"


def test_normalize_drops_what_costs_tokens_and_says_nothing():
    assert normalize_input("مُحَمَّد") == "محمد"  # diacritics
    assert normalize_input("ميـــگم") == "میگم"  # kashida
    assert normalize_input("سلاااااام") == "سلاام"  # a stretched word
    assert normalize_input("heyyyyy!!!!!!!") == "heyy!!!"
    assert normalize_input("a​b﻿c") == "abc"  # invisible marks


def test_normalize_keeps_what_carries_meaning():
    # The zero-width non-joiner separates Persian words; dropping it glues them.
    assert normalize_input("می‌دونم") == "می‌دونم"
    # Digits are not enthusiasm: 1000 must not become 100.
    assert normalize_input("1000 تومن") == "1000 تومن"
    assert normalize_input("") == ""


def test_shorten_cuts_on_a_word_boundary():
    assert shorten("the quick brown fox jumps", 20) == "the quick brown fox…"
    assert shorten("short", 20) == "short"
    assert len(shorten("x" * 50, 10)) == 11  # the ellipsis is the eleventh


def _message(text="", *, entities=None, reply_to=None):
    return SimpleNamespace(
        text=text,
        caption=None,
        entities=entities or [],
        caption_entities=[],
        reply_to_message=reply_to,
    )


def test_is_addressed_variants():
    bot_user = SimpleNamespace(id=999, username="astolfo_bot")

    assert is_addressed(_message("hey @astolfo_bot"), bot_user)
    assert is_addressed(_message("astolfo what do you think"), bot_user)
    assert is_addressed(_message("آستولفو نظرت چیه"), bot_user)
    assert not is_addressed(_message("just chatting"), bot_user)
    # Typed on an Arabic keyboard, or stretched out.
    assert is_addressed(_message("آستولفو نظرت چيه"), bot_user)
    assert is_addressed(_message("astolfooooo hey"), bot_user)

    reply = SimpleNamespace(from_user=SimpleNamespace(id=999))
    assert is_addressed(_message("exactly", reply_to=reply), bot_user)

    mention = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=999))
    assert is_addressed(_message("look", entities=[mention]), bot_user)
