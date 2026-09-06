"""One text file with everything worth asking the server about.

Every diagnosis so far has gone through somebody copying a screen into a chat,
which loses the part that scrolled away and never includes the tables. This is
what a shell on the box would have been used for, written out in one go:
what each model did, what each service last refused, what the brain has learned,
and how the database is holding up.

It is deliberately read-only and deliberately dull. Nothing here reads a
credential, a chat message, or a person's name: the counters are about models
and services, and the two places a message could leak in - the notes and the
learned style - are not in it. What comes out can be pasted anywhere.
"""

from __future__ import annotations

import time
from datetime import date

MAX_ROWS = 60


def _rule(title: str) -> str:
    return f"\n{'=' * 62}\n{title}\n{'=' * 62}"


def _table(rows: list[dict], columns: tuple[str, ...]) -> list[str]:
    """A fixed-width table, because this is read in a monospace bubble."""
    if not rows:
        return ["(nothing yet)"]
    widths = {
        name: max(len(name), *(len(str(row.get(name, ""))) for row in rows))
        for name in columns
    }
    out = ["  ".join(name.ljust(widths[name]) for name in columns)]
    out.append("  ".join("-" * widths[name] for name in columns))
    for row in rows:
        out.append("  ".join(str(row.get(name, "")).ljust(widths[name]) for name in columns))
    return out


def _ago(when: float | None) -> str:
    if not when:
        return "never"
    gap = max(0.0, time.time() - float(when))
    if gap < 3600:
        return f"{gap / 60:.0f}m ago"
    if gap < 86400:
        return f"{gap / 3600:.1f}h ago"
    return f"{gap / 86400:.1f}d ago"


def _settings(rt) -> list[str]:
    """The switches that change behaviour, and not one value that is a secret."""
    s = rt.settings
    return [
        f"version         {_version()}",
        f"free mode       {'on' if s.free_mode else 'off'}",
        f"prompt weight   {getattr(s, 'prompt_tier', 'auto')}",
        f"brain           {'on' if getattr(s, 'brain', False) else 'off'}"
        f" (writing {'on' if getattr(s, 'brain_writes', False) else 'off'})",
        f"heavy lifting   {'on' if s.heavy_lifting else 'off'}",
        f"reply mode      {s.reply_mode}",
        f"providers       {', '.join(p.name for p in rt.llm.providers) or 'none'}",
        f"pinned          {s.pinned_service or '(none)'}",
    ]


def _version() -> str:
    from . import __version__

    return __version__


def _services(rt) -> list[str]:
    """Which services are usable, and for the rest, why not.

    `last_ok` belongs to a credential, not to a service - the two tables are not
    the same shape, and reading it off the wrong one cost this whole section the
    first time it ran against a real database.
    """
    now = time.time()
    working: dict[str, float] = {}
    for row in rt.db.credentials():
        try:
            when = float(row["last_ok"] or 0.0)
        except (IndexError, KeyError, TypeError, ValueError):
            break  # an older database without the column; the rest still reads
        working[row["service"]] = max(working.get(row["service"], 0.0), when)
    rows = []
    for row in rt.db.services():
        resting = float(row["rested_until"] or 0)
        rows.append({
            "service": row["name"],
            "on": "yes" if row["enabled"] else "no",
            "resting": f"{(resting - now) / 60:.0f}m" if resting > now else "-",
            "last ok": _ago(working.get(row["name"])),
            "why it is out": str(row["last_error"] or "")[:78],
        })
    return _table(rows, ("service", "on", "resting", "last ok", "why it is out"))


def _faults(rt) -> list[str]:
    """What each service last said when it refused, in its own words."""
    try:
        recent = rt.llm.recent_faults()
    except Exception:  # a client that does not keep them is not a failure here
        return ["(not recorded by this client)"]
    if not recent:
        return ["(nothing refused since the last restart)"]
    return [f"{_ago(at):>9}  {fault.summary}"[:160] for at, fault in recent[:MAX_ROWS]]


def _usage(rt) -> list[str]:
    today = date.today().isoformat()
    rows = [
        {
            "service": name,
            "calls": row["requests"],
            "failed": row["failures"],
            "tokens": row["tokens"],
            "cost": f"${row['cost']:.4f}",
        }
        for name, row in sorted(rt.db.service_usage(today).items())
    ]
    return _table(rows, ("service", "calls", "failed", "tokens", "cost"))


def _outcomes(rt) -> list[str]:
    """What each model actually produced, which is the whole argument about them."""
    rows = []
    for row in rt.db.outcomes(limit=MAX_ROWS):
        calls = int(row["calls"] or 0) or 1
        rows.append({
            "day": row["day"],
            "model": str(row["model"])[:38],
            "variant": str(row["variant"] or "")[:12],
            "mode": row["mode"],
            "calls": row["calls"],
            "broken": f"{int(row['broken'] or 0)} ({int(row['broken'] or 0) * 100 // calls}%)",
            "repaired": row["repaired"],
            "answered": row["answered"],
        })
    return _table(
        rows, ("day", "model", "variant", "mode", "calls", "broken", "repaired", "answered")
    )


def _weights(rt) -> list[str]:
    """Each model against itself on a different prompt weight.

    The table above is keyed by day and by mode, so this comparison had to be
    summed by hand - and a model's older rows fall off the sixty-row cap while
    its newer ones stay, which shows a partial comparison that looks whole.

    Only ever within one model. `gemini-flash-lite` on `tight` against
    `command-r` on `compact` measures the models, not the weights, and reading
    it the other way is the mistake this section exists to stop.
    """
    from .brain import ENOUGH

    rows: list[dict] = []
    per_model: dict[str, list] = {}
    for row in rt.db.outcomes_by_variant():
        calls = int(row["calls"] or 0)
        broken = int(row["broken"] or 0)
        rows.append({
            "model": str(row["model"])[:38],
            "weight": row["variant"],
            "calls": calls,
            "broken": f"{broken} ({broken * 100 // calls}%)" if calls else "0",
            "answered": row["answered"],
        })
        per_model.setdefault(str(row["model"]), []).append((row["variant"], calls, broken))

    if not rows:
        return ["(nothing yet)"]

    out = _table(rows[:MAX_ROWS], ("model", "weight", "calls", "broken", "answered"))
    out.append("")
    for model, arms in sorted(per_model.items()):
        if len(arms) < 2:
            continue
        short = [f"{name} has {calls} of {ENOUGH}" for name, calls, _ in arms if calls < ENOUGH]
        if short:
            out.append(f"{model}: not a comparison yet, {'; '.join(short)}")
            continue
        best = min(arms, key=lambda arm: arm[2] / arm[1])
        worst = max(arms, key=lambda arm: arm[2] / arm[1])
        gap = (worst[2] / worst[1] - best[2] / best[1]) * 100
        out.append(
            f"{model}: {best[0]} beats {worst[0]} by {gap:.0f} points of broken replies"
        )
    return out


def _health(rt) -> list[str]:
    strikes = rt.db.model_strikes()
    if not strikes:
        return ["(no model has been caught yet)"]
    rows = [
        {"model": model, "strikes": count}
        for model, count in sorted(strikes.items(), key=lambda pair: -pair[1])
    ]
    return _table(rows[:MAX_ROWS], ("model", "strikes"))


def _brain(rt) -> list[str]:
    brain = getattr(rt, "brain", None)
    if brain is None:
        return ["(this build has no brain)"]
    out = [
        f"selecting  {'on' if brain.on else 'off'}",
        f"tripped    {brain.breaker.tripped or '-'}",
        f"paused     {', '.join(sorted(brain.breaker.paused)) or '-'}",
        "",
    ]
    rows = [
        {
            "family": family,
            "recipe": recipe,
            "samples": arm.samples,
            "mean": f"{arm.mean:.0%}",
        }
        for (family, recipe), arm in sorted(brain.arms.items())
    ]
    return out + _table(rows[:MAX_ROWS], ("family", "recipe", "samples", "mean"))


def _database(rt) -> list[str]:
    counts = rt.db.counts()
    size = rt.db.size_bytes()
    width = max((len(name) for name in counts), default=8)
    return [f"{'on disk'.ljust(width)}  {size / (1024 * 1024):.1f} MB"] + [
        f"{name.ljust(width)}  {count}" for name, count in counts.items()
    ]


def report(rt) -> str:
    """Everything, in one string. Never raises: a broken section says so."""
    parts = [
        f"Astolfo diagnostics — {time.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "Read-only. No credentials, no chat text, no names.",
    ]
    for title, section in (
        ("settings", _settings),
        ("services", _services),
        ("last refusals, in each service's own words", _faults),
        ("today's usage", _usage),
        ("what each model produced", _outcomes),
        ("prompt weight, each model against itself", _weights),
        ("model health", _health),
        ("brain", _brain),
        ("database", _database),
    ):
        parts.append(_rule(title))
        try:
            parts.extend(section(rt))
        except Exception as exc:  # one bad table must not cost the whole report
            parts.append(f"(this section could not be read: {exc})")
    return "\n".join(parts) + "\n"


def write(rt, path: str) -> str:
    """The report on disk, or "" when it could not be written."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report(rt))
    except OSError:
        return ""
    return path
