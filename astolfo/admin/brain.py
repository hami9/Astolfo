"""Gate 6: what the brain is doing, and the buttons that stop it.

The plan's promise was that nothing the brain does is invisible and nothing it
does is irreversible. This is that screen: which recipe each model family is
running, how each one is scoring, which families the breaker has sent home, and
three buttons - stop exploring, forget everything learned, back to factory.

Reading it costs nothing: every number here is already in memory, kept by the
bandit that the turn loop was going to update anyway.
"""

from __future__ import annotations

from .. import brain as brain_mod
from .. import recipes, settings_store
from ..guardrail import EXPLORATION_FLOOR, MAX_FAMILIES
from .sections import View, audit
from .ui import back_row, button, confirm_rows, keyboard

# Enough to see what is happening without a screen you have to scroll.
FAMILIES_SHOWN = 8


def _line(brain, family: str) -> str:
    """One family: what it is running, and what the evidence says."""
    arms = {name: arm for (fam, name), arm in brain.arms.items() if fam == family}
    total = sum(arm.samples for arm in arms.values())
    if brain.breaker.blocked(family):
        return f"• {family} — sent home by the breaker, {total} samples"
    if total < brain_mod.ENOUGH:
        return f"• {family} — still watching, {total}/{brain_mod.ENOUGH} samples"
    best = max(arms.items(), key=lambda pair: pair[1].mean)
    return f"• {family} — {best[0]} ({best[1].mean:.0%} of {total} samples)"


def overview(ctx) -> View:
    rt = ctx.rt
    brain = rt.brain
    on = bool(rt.settings.brain)

    lines = [
        "🧩 Brain\n",
        "It learns which prompt weight each model family answers to, and nothing else.",
        f"\nselecting: {'on' if on else 'off'}",
        "writing: not built yet",
    ]
    if not on:
        lines.append(
            "\nWith selecting off the prompt is byte for byte what it has always been. "
            "The counters below are still being kept, so switching it on is not "
            "starting from nothing."
        )
    if brain.breaker.tripped:
        lines.append(f"\n⚠️ the whole brain tripped: {brain.breaker.tripped}")

    families = sorted({fam for fam, _ in brain.arms})
    lines.append(f"\nfamilies: {len(families)}/{MAX_FAMILIES}")
    if not families:
        lines.append("• nothing learned yet")
    for family in families[:FAMILIES_SHOWN]:
        lines.append(_line(brain, family))
    if len(families) > FAMILIES_SHOWN:
        lines.append(f"…and {len(families) - FAMILIES_SHOWN} more")

    lines.append(
        f"\n{EXPLORATION_FLOOR:.0%} of turns go deliberately off the winner, so a recipe "
        "that suited last week cannot hold a family for good."
    )
    select = button(
        "⏻ selecting off" if on else "⏻ selecting on", "brain", "on", "0" if on else "1"
    )
    # The part that writes a layer of its own is the last unbuilt step of the
    # plan. The switch stored a setting nothing reads, and every screen reported
    # it back as though something had happened - so it says what is true instead.
    write = button("✍️ writing — not built yet", "brain", "w", "1")
    return View(
        "\n".join(lines),
        keyboard(
            [select],
            [write],
            [button("🏠 back to factory", "brain", "home")],
            [button("🗑 forget everything", "brain", "wipe")],
            back_row(),
        ),
    )


def switch(ctx, field: str, on: bool) -> View:
    """Turn selecting on or off. Stored, so it survives a restart."""
    if field == "brain_writes":
        # Nothing reads `brain_writes`: the writer is the one step of the brain
        # that was never built. Storing the setting made the screen claim a
        # capability the bot does not have.
        view = overview(ctx)
        view.alert = "the writer is not built yet; there is nothing to switch on"
        return view
    settings_store.set_override(ctx.rt.db, field, "1" if on else "0", by=ctx.user.id)
    audit(ctx.rt, ctx.user, f"{field}_{'on' if on else 'off'}")
    view = overview(ctx)
    view.alert = f"selecting is {'on' if on else 'off'}"
    view.extras["reload"] = True
    return view


def home(ctx) -> View:
    """Stop exploring: every family back on the recipe it would have used anyway.

    The counters are kept. This is a pause, not an erasure - it is the button for
    "something is wrong and I want today back", and the evidence is still there
    tomorrow.
    """
    ctx.rt.brain.breaker.tripped = "sent home from the panel"
    audit(ctx.rt, ctx.user, "brain_home")
    view = overview(ctx)
    view.alert = "every family is on the factory recipe"
    return view


def wipe(ctx, confirmed: bool) -> View:
    """Forget everything learned. Asked twice, because it cannot be undone."""
    if not confirmed:
        return View(
            "Forget everything the brain has learned?\n"
            "Every family starts from nothing and needs its samples again.",
            keyboard(*confirm_rows("forget it all", "brain", "wipe!")),
        )
    brain = ctx.rt.brain
    brain.arms.clear()
    brain.breaker.families.clear()
    brain.breaker.baseline.clear()
    brain.breaker.paused.clear()
    brain.breaker.tripped = ""
    brain.dirty = True
    ctx.rt.save_brain()
    audit(ctx.rt, ctx.user, "brain_wipe")
    view = overview(ctx)
    view.alert = "a clean sheet"
    return view


def route(ctx, rest: list[str]) -> View:
    action = rest[0] if rest else ""
    if action == "on":
        return switch(ctx, "brain", rest[1] == "1")
    if action == "w":
        return switch(ctx, "brain_writes", rest[1] == "1")
    if action == "home":
        return home(ctx)
    if action in ("wipe", "wipe!"):
        return wipe(ctx, confirmed=action.endswith("!"))
    return overview(ctx)


# Named here so the screen and the recipes it prints cannot drift apart.
KNOWN_RECIPES = tuple(recipes.FACTORY)
