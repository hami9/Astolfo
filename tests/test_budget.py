from astolfo.budget import ADDRESSED_ONLY, CHEAP, FULL, STOPPED, BudgetTracker
from astolfo.llm import Usage


def _spend(tracker: BudgetTracker, cost: float, *, chat_id: int = 1, mode: str = "fast") -> None:
    tracker.record(
        mode=mode,
        model="test/model",
        usage=Usage(prompt_tokens=1000, completion_tokens=100, cached_tokens=400, cost=cost),
        chat_id=chat_id,
    )


def test_unlimited_by_default(settings):
    tracker = BudgetTracker(settings)
    _spend(tracker, 5.0)
    allowance = tracker.check(chat_id=1, addressed=False)
    assert allowance.allowed and allowance.level == FULL


def test_degradation_ladder(settings):
    tracker = BudgetTracker(settings.replace(daily_budget_usd=1.0))

    assert tracker.check(chat_id=1, addressed=False).level == FULL

    _spend(tracker, 0.85)
    cheap = tracker.check(chat_id=1, addressed=False)
    assert cheap.allowed and cheap.level == CHEAP
    assert cheap.force_mode == "fast"
    assert cheap.allow_web is False
    assert cheap.allow_router_llm is False

    _spend(tracker, 0.20)  # 1.05 total
    assert tracker.check(chat_id=1, addressed=False).allowed is False
    addressed = tracker.check(chat_id=1, addressed=True)
    assert addressed.allowed and addressed.level == ADDRESSED_ONLY

    _spend(tracker, 0.30)  # 1.35 total
    assert tracker.check(chat_id=1, addressed=True).level == STOPPED
    assert tracker.check(chat_id=1, addressed=True).allowed is False


def test_monthly_cap_stops_everything(settings):
    tracker = BudgetTracker(settings.replace(monthly_budget_usd=0.5))
    _spend(tracker, 0.6)
    assert tracker.check(chat_id=1, addressed=True).level == STOPPED


def test_per_chat_call_cap(settings):
    tracker = BudgetTracker(settings.replace(chat_daily_call_limit=2))
    _spend(tracker, 0.01, chat_id=7)
    _spend(tracker, 0.01, chat_id=7)
    assert tracker.check(chat_id=7, addressed=True).allowed is False
    assert tracker.check(chat_id=8, addressed=True).allowed is True


def test_summary_and_cache_stats(settings):
    tracker = BudgetTracker(settings)
    _spend(tracker, 0.01, mode="fast")
    _spend(tracker, 0.05, mode="think")
    tracker.record_cache_hit()

    summary = tracker.summary()
    assert summary["calls"] == 2
    assert summary["cost_today"] == 0.06
    assert summary["by_mode"]["think"] == 0.05
    assert summary["cache_replies"] == 1
    assert summary["cache_hit_rate"] == 0.4  # 800 cached of 2000 prompt tokens
    assert summary["top_model"] == "test/model"


def test_usage_survives_restart(settings):
    tracker = BudgetTracker(settings)
    _spend(tracker, 0.42)
    tracker.save(force=True)

    reloaded = BudgetTracker(settings)
    assert reloaded.today_cost() == 0.42
    assert reloaded.month_cost() >= 0.42


# -- what each model did --------------------------------------------------
def test_a_model_records_calls_and_tokens_not_just_cost(settings):
    tracker = BudgetTracker(settings)
    tracker.record(
        mode="fast", model="a/b", usage=Usage(prompt_tokens=100, completion_tokens=20, cost=0.001)
    )
    tracker.record(
        mode="fast", model="a/b", usage=Usage(prompt_tokens=50, completion_tokens=10, cost=0.0005)
    )

    (model, row), = tracker.model_usage()
    assert model == "a/b"
    assert row == {"calls": 2, "prompt": 150, "completion": 30, "cost": 0.0015}


def test_free_models_are_ranked_by_work_since_they_all_cost_nothing(settings):
    tracker = BudgetTracker(settings)
    for _ in range(3):
        tracker.record(mode="fast", model="busy/one", usage=Usage(prompt_tokens=10))
    tracker.record(mode="fast", model="quiet/one", usage=Usage(prompt_tokens=10))

    assert [model for model, _ in tracker.model_usage()] == ["busy/one", "quiet/one"]
    assert tracker.summary()["top_model"] == "busy/one"


def test_history_written_before_tokens_were_tracked_still_reads(settings, tmp_path):
    """Old files recorded a bare cost per model; it must not crash or vanish."""
    import json
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"days": {today: {"cost": 0.02, "by_model": {"old/model": 0.02}}}}))

    tracker = BudgetTracker(settings.replace(data_dir=str(tmp_path)))
    (model, row), = tracker.model_usage()
    assert model == "old/model"
    assert row == {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.02}

    tracker.record(mode="fast", model="old/model", usage=Usage(prompt_tokens=5, cost=0.001))
    (_, row), = tracker.model_usage()
    assert row == {"calls": 1, "prompt": 5, "completion": 0, "cost": 0.021}
