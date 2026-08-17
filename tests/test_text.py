from types import SimpleNamespace

from astolfo.text import (
    TELEGRAM_MAX_LEN,
    clean_name,
    format_sources,
    is_addressed,
    polish,
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

    reply = SimpleNamespace(from_user=SimpleNamespace(id=999))
    assert is_addressed(_message("exactly", reply_to=reply), bot_user)

    mention = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=999))
    assert is_addressed(_message("look", entities=[mention]), bot_user)
