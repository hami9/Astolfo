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


def test_it_is_told_to_get_into_it_rather_than_go_quiet() -> None:
    """One bored line was the first draft and it was too little. Being wound up is
    the fun part; the ask was a bot that can hold a round, not one that endures."""
    for prompt in _both():
        assert "bite back" in prompt
        assert "as long as they do" in prompt, "it does not tap out after one line"


def test_it_bites_back_without_swearing() -> None:
    """The whole constraint on the fun: clean hits, and said to be the better ones."""
    for prompt in _both():
        assert "swear word" in prompt
        assert "lost the round" in prompt, "reaching for one is framed as losing"


def test_it_goes_after_what_somebody_chose_and_nothing_else() -> None:
    """Where a roast turns into something else. Chosen: the bragging, the attempt,
    their aim in a game. Not chosen: a family, a body, money, illness, an origin."""
    for prompt in _both():
        assert "what they chose" in prompt or "what they did not choose" in prompt
        assert "in kind" in prompt, "and a crude one is still never matched"


def test_it_stops_first_and_says_so() -> None:
    """Three ways a round ends, and none of them wait to be asked."""
    for prompt in _both():
        assert "upset" in prompt, "somebody actually hurt ends it"
        assert "on one person" in prompt, "and so does the group ganging up"


def test_the_bite_back_sample_is_in_every_prompt() -> None:
    """A small model copies a sample far more reliably than it follows a rule, and
    the compact prompt used to carry only the excited one."""
    assert "took you three days to notice" in persona.static_prompt(locale="en")
    assert "took you three days to notice" in persona.compact_prompt(locale="en")
    assert "سه روز طول کشید" in persona.static_prompt(locale="fa")
    assert "سه روز طول کشید" in persona.compact_prompt(locale="fa")
    assert "bites back" not in persona.compact_prompt(), "the tag itself is stripped"
    assert "[excited]" not in persona.compact_prompt(), "and so is the other one"


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


# -- and who it is actually talking to ------------------------------------
def test_the_newest_sender_is_named_outright() -> None:
    """Summoned by one member, it opened with the owner's name: "جانم حامی؟" to a
    message Arash sent. The transcript said so, but a small model took the most
    familiar name in the chat instead of the one on the last line."""
    block = persona.dynamic_prompt(sender="Arash")

    assert "newest message is from Arash" in block
    assert "only name your reply may use" in block


def test_no_sender_leaves_the_line_out() -> None:
    """A private chat has nobody to get wrong."""
    assert "newest message is from" not in persona.dynamic_prompt()


async def test_the_name_reaches_the_prompt_from_a_real_turn(rt, llm) -> None:
    """The wiring, not just the wording: it is the sender of this message, not
    whoever the chat happens to remember."""
    from astolfo import chat as chat_mod
    from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update

    message = FakeMessage("astolfo!", name="Arash")
    await chat_mod.handle_message(make_update(message), FakeContext(rt, FakeBot()))

    system = "\n".join(
        m["content"] for m in llm.calls[-1]["messages"] if m["role"] == "system"
    )
    assert "newest message is from Arash" in system
