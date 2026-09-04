from types import SimpleNamespace

from astolfo.text import (
    TELEGRAM_MAX_LEN,
    clean_name,
    cut_impersonation,
    format_sources,
    is_addressed,
    looks_broken,
    normalize_input,
    polish,
    shorten,
    split_message,
    stray_language,
    strip_speaker,
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


# -- the transcript it was shown is not a script to continue -----------------
KNOWN = ["Arash(IQ 26)", "Arash", "Mehrshad y", "Hami", "Astolfo"]


def test_the_speaker_label_it_copied_is_removed():
    """Every reply in the group came back wearing the name of whoever it answered."""
    assert strip_speaker("Arash(IQ 26): ترجیح میدم نگم~", KNOWN) == "ترجیح میدم نگم~"
    assert strip_speaker("Hami: نخییییم", KNOWN) == "نخییییم"
    assert strip_speaker("Astolfo → Sara: hi", KNOWN) == "hi"


def test_a_name_it_invented_is_removed_too():
    """"Dollar" was a character in a GIF, not anyone in the chat."""
    assert strip_speaker("Dollar: heyyy that's me??", KNOWN) == "heyyy that's me??"


def test_it_does_not_eat_the_start_of_a_real_answer():
    for text in (
        "20:35 که رسیدم",
        "https://example.com is the link",
        "راستش نمیدونم: شاید فردا",
        "یه چیزی بگم: خیلی بامزه بود",
        "ehehe~ nope",
    ):
        assert strip_speaker(text, KNOWN) == text


def test_a_role_label_is_left_for_the_quality_guard():
    """"assistant:" is not a name, it is a model that lost track of itself."""
    assert strip_speaker("assistant: hello", KNOWN) == "assistant: hello"
    assert looks_broken("assistant: hello") == "answered in transcript format"


def test_it_stops_writing_other_people_s_messages():
    """One reply came back carrying two invented turns with real members' names."""
    faked = "عا برا خودت\n\nArash: هههه، گفتم که دیگه، من گوزلم"
    assert cut_impersonation(faked, KNOWN) == "عا برا خودت"


def test_a_normal_reply_survives_both():
    plain = "ehehe~ nope\nنمیگم بهت"
    assert cut_impersonation(strip_speaker(plain, KNOWN), KNOWN) == plain


def test_nothing_is_cut_when_the_chat_has_no_names_yet():
    assert cut_impersonation("Reza: hi", []) == "Reza: hi"


# -- one language per message ------------------------------------------------
def test_a_script_nobody_was_writing_in_is_a_broken_reply():
    assert stray_language("خوبم، 你好 نشدم") == "你"
    assert looks_broken("خوبم، 你好 نشدم")
    assert looks_broken("hello привет")


def test_english_inside_persian_is_left_alone():
    """Persian chats really do say کد and آپدیت in English."""
    assert stray_language("آپدیت رو زدم، commit هم کردم") is None
    assert not looks_broken("آپدیت رو زدم، commit هم کردم")
