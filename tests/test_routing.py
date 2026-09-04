import pytest

from astolfo import routing
from astolfo.persona import FAST, SEARCH, SERIOUS, THINK
from astolfo.routing import CONFIDENT, heuristic

CASES = [
    ("hi how are you", FAST),
    ("lol", FAST),
    ("سلام چطوری", FAST),
    ("what's the dollar price today?", SEARCH),
    ("قیمت دلار امروز چنده؟", SEARCH),
    ("آخرین نسخهٔ پایتون چیه؟", SEARCH),
    ("سرچ کن ببین کی برنده شد", SEARCH),
    ("search who won yesterday", SEARCH),
    ("چطور این ارور رو دیباگ کنم؟", THINK),
    ("why does this code throw an error?", THINK),
    ("یه کد پایتون بنویس که فایل بخونه", THINK),
    ("خیلی داغونم، حالم بده", SERIOUS),
    ("i feel awful today", SERIOUS),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_heuristic_modes(text, expected):
    decision, confidence = heuristic(text)
    assert decision.mode == expected
    assert 0.0 <= confidence <= 1.0


def test_search_always_enables_web():
    decision, _ = heuristic("قیمت دلار امروز چنده؟")
    assert decision.web is True


def test_media_without_text_is_fast_and_confident():
    decision, confidence = heuristic("", has_media=True)
    assert decision.mode == FAST
    assert confidence >= CONFIDENT


async def test_forced_mode_skips_everything(rt):
    decision, usage = await rt.router.decide(text="anything", forced_mode=THINK)
    assert decision.mode == THINK
    assert decision.source == "user"
    assert usage.total_tokens == 0


async def test_confident_heuristic_does_not_call_the_model(rt, llm):
    rt.settings = rt.settings.replace(router_llm=True)
    rt.router._s = rt.settings
    await rt.router.decide(text="hi")
    assert llm.json_calls == []


async def test_dispatcher_used_for_ambiguous_text(rt, llm):
    rt.router._s = rt.settings.replace(router_llm=True)
    llm.json_result = {"mode": "search", "web": True, "query": "eiffel tower height", "why": "fact"}

    decision, usage = await rt.router.decide(text="how tall is that tower in paris again")
    assert decision.mode == SEARCH
    assert decision.source == "llm"
    assert decision.query == "eiffel tower height"
    assert usage.total_tokens > 0
    assert len(llm.json_calls) == 1


async def test_dispatcher_results_are_cached(rt, llm):
    rt.router._s = rt.settings.replace(router_llm=True)
    llm.json_result = {"mode": "think", "web": False, "query": "", "why": "reasoning"}
    text = "which of these two approaches would you actually pick and why"

    first, _ = await rt.router.decide(text=text)
    second, usage = await rt.router.decide(text=text)

    assert first == second
    assert len(llm.json_calls) == 1, "identical text must not be classified twice"
    assert usage.total_tokens == 0
    assert rt.router.cache.hits == 1


async def test_dispatcher_skipped_when_budget_degraded(rt, llm):
    rt.router._s = rt.settings.replace(router_llm=True)
    llm.json_result = {"mode": "think", "web": False}
    decision, _ = await rt.router.decide(
        text="which of these two approaches would you pick", allow_llm=False
    )
    assert llm.json_calls == []
    assert decision.source == "heuristic"


async def test_distress_is_never_downgraded(rt, llm):
    rt.router._s = rt.settings.replace(router_llm=True)
    llm.json_result = {"mode": "fast", "web": False}
    decision, _ = await rt.router.decide(text="i feel awful today and i cannot cope")
    assert decision.mode == SERIOUS


async def test_bad_dispatcher_output_falls_back(rt, llm):
    rt.router._s = rt.settings.replace(router_llm=True)
    llm.json_result = {"mode": "nonsense"}
    decision, _ = await rt.router.decide(text="what do you make of this whole situation")
    assert decision.source == "heuristic"


def test_router_min_words_shortcut(rt, llm):
    assert rt.settings.router_min_words >= 1


async def test_budget_downgrade_never_silences_distress(rt):
    decision, _ = await rt.router.decide(
        text="i feel awful today and i want to give up",
        forced_mode=FAST,
        forced_source="budget",
    )
    assert decision.mode == SERIOUS, "cost cuts must not turn distress into banter"


async def test_budget_downgrade_applies_to_ordinary_messages(rt):
    decision, _ = await rt.router.decide(
        text="why does python raise a KeyError here",
        forced_mode=FAST,
        forced_source="budget",
    )
    assert decision.mode == FAST
    assert decision.source == "budget"


async def test_user_pinned_mode_is_labelled(rt):
    decision, _ = await rt.router.decide(text="anything at all", forced_mode=SEARCH)
    assert decision.source == "user" and decision.web is True


def test_dispatcher_prompt_excludes_banter_from_serious():
    """Group trash talk was reaching the expensive model as genuine distress."""
    prompt = " ".join(routing.DISPATCHER_PROMPT.lower().split())
    assert "insults or threats aimed at the bot" in prompt
    assert "when in doubt it is \"fast\"" in prompt
    assert "group chats are mostly banter" in prompt


def test_serious_mode_can_drop_the_concern_if_it_is_banter():
    from astolfo import persona

    block = " ".join(persona.MODE_BLOCKS[SERIOUS].lower().split())
    assert "do not perform concern" in block
    assert "unbothered voice" in block


# -- work it was not built for --------------------------------------------
def test_homework_is_not_escalated_to_a_think_model():
    """It declines these in character, so paying for reasoning buys the refusal."""
    for text in (
        "میشه تکلیف ریاضیمو حل کنی",
        "این انتگرال رو حساب کن",
        "can you write me a python bot",
        "prove that the sum is even",
    ):
        decision, confidence = heuristic(text, heavy_lifting=False)
        assert decision.mode == FAST, text
        assert confidence >= 0.85, "confident enough not to ask the dispatcher"


def test_small_things_are_still_normal_conversation():
    """Only work somebody wants done is turned away, not every question."""
    for text in ("معنی serendipity چیه؟", "فرق list و tuple چیه", "how do I center a div"):
        decision, _ = heuristic(text, heavy_lifting=False)
        assert decision.reason != "not what it is for", text


def test_an_owner_who_wants_a_solver_gets_one():
    text = "میشه تکلیف ریاضیمو حل کنی"
    assert heuristic(text, heavy_lifting=False)[0].mode == FAST
    assert heuristic(text, heavy_lifting=True)[0].mode == THINK


def test_distress_still_wins_over_everything():
    decision, _ = heuristic("تکلیفم مونده و افسرده‌ام", heavy_lifting=False)
    assert decision.mode == SERIOUS
