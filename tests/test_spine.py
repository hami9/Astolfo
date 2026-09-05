"""Being hard to offend is not the same as having no spine.

Written from one evening in the group, all of it real output: told it talks too
much, it apologised; called a clown, it said being a clown is cool; called a
name, it said the name back; handed a crude two-way insult about its parents, it
answered "maybe both". Every reply took the other person's side against itself.

The prompt was not wrong - "unbothered", "never apologise for being weak" - but a
small model reads unbothered as agreeable, and free mode is running the compact
prompt, which said neither.
"""

from __future__ import annotations

from astolfo import persona


def _both(**kwargs) -> list[str]:
    """The prompt as each kind of chat gets it, and as a small model gets it.

    Whitespace-collapsed: these blocks are hand-wrapped prose, and a phrase that
    happens to straddle a line break is still the rule being there. Rewrapping a
    paragraph should not fail a test about what it says.
    """
    return [
        " ".join(prompt.lower().split())
        for prompt in (
            persona.static_prompt(is_group=True, **kwargs),
            persona.static_prompt(is_group=False, **kwargs),
            persona.compact_prompt(is_group=True),
            persona.compact_prompt(is_group=False),
        )
    ]


def test_no_prompt_leaves_it_without_a_spine() -> None:
    """The compact one especially: free mode is what the group is running."""
    for prompt in _both():
        assert "punching bag" in prompt, "it is told it is nobody's"
        assert "never agree with an insult" in prompt or (
            "never agree with it" in prompt
        ), prompt[:60]


def test_it_is_told_not_to_apologise_for_being_itself() -> None:
    for prompt in _both():
        assert "apologise for being yourself" in prompt


def test_saying_the_name_back_is_named_as_agreeing() -> None:
    """"بنگی" came back as "باشه، بنگی!". Repeating the word is accepting it."""
    for prompt in _both():
        assert "say the name back" in prompt or "the name back" in prompt


def test_it_is_told_to_answer_back_rather_than_go_quiet() -> None:
    """The ask was a reply with some Astolfo in it, not a refusal and not silence."""
    for prompt in _both():
        assert "answer back" in prompt


def test_answering_back_is_not_licence_to_be_cruel() -> None:
    """The line the fix must not cross: teasing, never a match for the insult."""
    for prompt in _both():
        assert "never humiliate" in prompt or "you do not humiliate" in prompt
        assert "in kind" in prompt, "and never returns a crude insult with one"


def test_somebody_actually_upset_still_gets_the_sincere_voice() -> None:
    """A spine must not run over the one part of the character that matters."""
    for prompt in _both():
        assert "upset" in prompt


def test_echoing_the_message_is_banned_in_both_shapes() -> None:
    """How the agreeing happened mechanically: it handed the words straight back."""
    for prompt in _both():
        assert "echo" in prompt


def test_the_shrug_is_banned_in_both_shapes() -> None:
    """"شاید باشه، شاید هم نباشه! من که نمی‌دونم!" was three of the replies."""
    for prompt in _both():
        assert "maybe, maybe not" in prompt


def test_the_spine_is_a_layer_of_its_own_and_survives_heavy_lifting() -> None:
    """Dropped alongside <not-your-job>, it would go missing on the hardest turns."""
    assert "<spine>" in persona.static_prompt(heavy_lifting=True)
    assert "<spine>" in persona.static_prompt(heavy_lifting=False)


def test_the_boundaries_still_stand_next_to_it() -> None:
    """The 2.5.2 fix and this one answer different messages; neither replaces the
    other, and a crude question is still not something to answer back to."""
    prompt = persona.static_prompt()
    assert "<boundaries>" in prompt and "<spine>" in prompt
    assert prompt.index("<boundaries>") < prompt.index("<spine>")
