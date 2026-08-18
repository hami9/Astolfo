from astolfo import persona
from astolfo.persona import FAST, SEARCH, SERIOUS, THINK


def test_static_prompt_contains_every_layer():
    prompt = persona.static_prompt(is_group=True, locale="en")
    for marker in (
        "<identity>",
        "<voice>",
        "<canon-anchors>",
        "<language>",
        "<never>",
        "<meta-questions>",
        '<truthfulness priority="highest">',
        "<examples>",
        "<output>",
    ):
        assert marker in prompt, f"missing layer {marker}"
    assert 'mode="group"' in prompt


def test_static_prompt_is_stable_for_caching():
    a = persona.static_prompt(is_group=True, locale="en")
    b = persona.static_prompt(is_group=True, locale="en")
    assert a == b, "static block must be byte-identical so providers can cache it"
    assert persona.static_prompt(is_group=False, locale="en") != a
    assert persona.static_prompt(is_group=True, locale="fa") != a


def test_mode_blocks_live_in_the_dynamic_half():
    static = persona.static_prompt()
    for mode in (FAST, THINK, SEARCH, SERIOUS):
        assert f'name="{mode}"' not in static
        assert f'name="{mode}"' in persona.dynamic_prompt(mode=mode)


def test_media_block_is_conditional():
    assert "<media>" not in persona.dynamic_prompt(mode=FAST)
    assert "<media>" in persona.dynamic_prompt(mode=FAST, has_media=True)


def test_dynamic_prompt_carries_context():
    prompt = persona.dynamic_prompt(
        mode=SEARCH,
        notes="Reza loves coffee",
        participants=["Reza", "Sara"],
        bot_name="Astolfo",
        search_query="dollar rate today",
    )
    assert "Reza loves coffee" in prompt
    assert "Reza, Sara" in prompt
    assert "dollar rate today" in prompt


def test_locale_detection():
    assert persona.detect_locale(["سلام چطوری", "خوبی؟"]) == "fa"
    assert persona.detect_locale(["hello there", "how are you"]) == "en"
    assert persona.detect_locale([]) == "en"
    assert persona.detect_locale([], default="fa") == "fa"


def test_persian_examples_selected_for_persian_chats():
    assert "آستولفو" in persona.static_prompt(locale="fa")
    assert "آستولفو" not in persona.static_prompt(locale="en")


def _flat(text: str) -> str:
    """Prompt prose is hard-wrapped, so compare on a single normalised line."""
    return " ".join(text.lower().split())


def test_output_rules_scope_the_reply_to_the_newest_message():
    prompt = _flat(persona.static_prompt())
    assert "answering the newest message only" in prompt
    assert "not a queue of questions waiting on you" in prompt
    assert "never address more than one person in a single reply" in prompt


def test_group_rules_say_not_to_answer_the_backlog():
    assert "you read the backlog, you do not reply to it" in _flat(
        persona.static_prompt(is_group=True)
    )
    assert "you read the backlog" not in _flat(persona.static_prompt(is_group=False))


def test_dynamic_prompt_points_at_the_final_message():
    assert "reply to the final message" in _flat(persona.dynamic_prompt())
