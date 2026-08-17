import pytest

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
