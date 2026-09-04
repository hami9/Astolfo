"""The bridge between the stored services and the client that uses them.

The client should not know about SQL or encryption, and the database should not
know about HTTP. This is the piece in the middle: it hands the client plain rows
to build providers from, and writes back what the client learns — which key was
refused, which service is out of allowance until when.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

from .crypto import SecretBox, SecretsUnavailable
from .db import Database

log = logging.getLogger(__name__)

# Below this many calls a service's numbers are noise, so it keeps its place.
ENOUGH_CALLS = 8
# What a reliable service is worth against a cheap one. Reliability wins: a service
# that answers is worth more than one that saves a tenth of a cent and 402s.
COST_WEIGHT = 0.35


@dataclass(frozen=True)
class Score:
    """How a service has actually behaved today."""

    name: str
    requests: int = 0
    failures: int = 0
    tokens: int = 0
    cost: float = 0.0
    resting: bool = False

    @property
    def calls(self) -> int:
        return self.requests + self.failures

    @property
    def reliability(self) -> float:
        return self.requests / self.calls if self.calls else 0.0

    @property
    def cost_per_call(self) -> float:
        return self.cost / self.requests if self.requests else 0.0

    @property
    def tokens_per_call(self) -> int:
        return round(self.tokens / self.requests) if self.requests else 0

    def value(self, dearest: float) -> float:
        """A number in [0, 1]; higher is a better first choice.

        Reliability carries it. Cost only separates services that are otherwise
        alike, and on free models every cost is zero so it drops out entirely.
        """
        if self.resting:
            return 0.0
        if self.calls < ENOUGH_CALLS:
            return 0.5  # not enough to judge: neither promoted nor demoted
        cheapness = 1.0 - (self.cost_per_call / dearest if dearest > 0 else 0.0)
        return (1 - COST_WEIGHT) * self.reliability + COST_WEIGHT * cheapness

    def verdict(self) -> str:
        if self.resting:
            return "resting"
        if self.calls < ENOUGH_CALLS:
            return f"only {self.calls} calls, too early to say"
        parts = [f"{self.reliability * 100:.0f}% answered"]
        if self.cost_per_call:
            parts.append(f"${self.cost_per_call:.5f}/call")
        if self.tokens_per_call:
            parts.append(f"{self.tokens_per_call}t/call")
        return ", ".join(parts)


class ServiceRegistry:
    def __init__(self, db: Database, box: SecretBox) -> None:
        self._db = db
        self._box = box

    # -- reading ----------------------------------------------------------
    def rows(self) -> list[dict]:
        """Every stored service with its keys decrypted, ready for `discover`."""
        by_service: dict[str, list[dict]] = {}
        for row in self._db.credentials():
            value = self._box.decrypt(bytes(row["value"]))
            if not value:
                log.warning(
                    "a stored key for %s cannot be read with the current encryption key",
                    row["service"],
                )
                continue
            by_service.setdefault(row["service"], []).append(
                {
                    "id": int(row["id"]),
                    "value": value,
                    "label": row["label"],
                    "enabled": row["enabled"],
                    "rested_until": row["rested_until"],
                    "last_error": row["last_error"],
                }
            )

        services = {row["name"]: dict(row) for row in self._db.services()}
        for name, credentials in by_service.items():
            services.setdefault(name, {"name": name})["credentials"] = credentials
        for row in services.values():
            row.setdefault("credentials", [])
        return sorted(services.values(), key=lambda row: row.get("position", 100))

    def known_names(self) -> list[str]:
        return [row["name"] for row in self._db.services()]

    # -- writing back what the client learns -------------------------------
    def rest_service(self, name: str, seconds: float, error: str = "") -> None:
        self._db.save_service(
            name, rested_until=time.time() + seconds, last_error=error[:200]
        )

    def rest_credential(self, credential_id: int | None, seconds: float, error: str) -> None:
        if credential_id is None:
            return  # it came from .env; there is no row to write to
        self._db.update_credential(
            credential_id, rested_until=time.time() + seconds, last_error=error[:200]
        )

    def note_use(self, credential_id: int | None, *, failed: bool = False) -> None:
        if credential_id is not None:
            self._db.count_credential_use(credential_id, failed=failed)

    def note_ok(self, credential_id: int | None) -> None:
        if credential_id is not None:
            self._db.update_credential(credential_id, last_ok=time.time(), last_error="")

    def record_call(
        self, service: str, *, failed: bool = False, tokens: int = 0, cost: float = 0.0
    ) -> None:
        self._db.add_service_usage(
            date.today().isoformat(),
            service,
            requests=0 if failed else 1,
            failures=1 if failed else 0,
            tokens=tokens,
            cost=cost,
        )

    def usage_today(self) -> dict:
        return self._db.service_usage(date.today().isoformat())

    # -- which one is actually doing best ---------------------------------
    def scores(self) -> list[Score]:
        """Every configured service ranked by how it has behaved today."""
        usage = self.usage_today()
        now = time.time()
        resting = {
            row["name"]: float(row["rested_until"] or 0) > now for row in self._db.services()
        }
        found: list[Score] = []
        for name in sorted({*usage, *resting}):
            row = usage.get(name)
            found.append(
                Score(
                    name=name,
                    requests=int(row["requests"]) if row else 0,
                    failures=int(row["failures"]) if row else 0,
                    tokens=int(row["tokens"]) if row else 0,
                    cost=float(row["cost"]) if row else 0.0,
                    resting=resting.get(name, False),
                )
            )
        dearest = max((score.cost_per_call for score in found), default=0.0)
        return sorted(found, key=lambda score: (-score.value(dearest), score.name))

    def auto_order(self, current: list[str] | None = None) -> list[str]:
        """Put the best-behaved service first. Returns the new order, or [].

        `current` is the order things are actually tried in, which the caller knows
        and this does not: a preset with no stored row still has a place in it.
        Only services with enough calls to judge are moved; the rest keep their
        relative places, so a brand new key is neither promoted nor buried.
        """
        scores = self.scores()
        judged = {score.name for score in scores if score.calls >= ENOUGH_CALLS}
        if len(judged) < 2:
            return []

        if current is None:
            current = [row["name"] for row in self.rows()]
        # A judged service with no row of its own still has to be placed, or the
        # order would silently drop it.
        current = current + [name for name in judged if name not in current]
        ranked = [score.name for score in scores if score.name in judged]
        # Walk the existing order and drop the judged ones back in, best first.
        wanted, taking = [], iter(ranked)
        for name in current:
            wanted.append(next(taking) if name in judged else name)
        if wanted == current:
            return []
        for position, name in enumerate(wanted):
            self._db.save_service(name, position=position)
        log.info("service order is now %s", ", ".join(wanted))
        return wanted

    # -- what the panel changes -------------------------------------------
    def add_key(self, service: str, value: str, *, label: str = "") -> int:
        if not self._box.available:
            raise SecretsUnavailable("encryption is not available on this install")
        self._db.save_service(service)
        return self._db.add_credential(service, self._box.encrypt(value), label=label)

    def reveal(self, credential_id: int) -> str:
        """The plain key, for masking in the panel. Never logged."""
        row = self._db.credential(credential_id)
        return (self._box.decrypt(bytes(row["value"])) or "") if row else ""

    def remove_key(self, credential_id: int) -> None:
        self._db.delete_credential(credential_id)

    def set_key_enabled(self, credential_id: int, enabled: bool) -> None:
        self._db.update_credential(
            credential_id, enabled=1 if enabled else 0, rested_until=0.0
        )

    def wake(self, service: str) -> None:
        """Forget every rest for a service, so the next message tries it again."""
        self._db.save_service(service, rested_until=0.0, last_error="")
        for row in self._db.credentials(service):
            self._db.update_credential(int(row["id"]), rested_until=0.0)

    def set_service_enabled(self, name: str, enabled: bool) -> None:
        self._db.save_service(name, enabled=1 if enabled else 0)

    def move(self, name: str, direction: int) -> None:
        """Shift a service one place in the order things are tried."""
        order = [row["name"] for row in self.rows()]
        if name not in order:
            return
        index = order.index(name)
        target = max(0, min(len(order) - 1, index + direction))
        if target == index:
            return
        order.insert(target, order.pop(index))
        for position, service in enumerate(order):
            self._db.save_service(service, position=position)

    def add_service(self, name: str, base_url: str, models: list[str]) -> None:
        self._db.save_service(
            name,
            base_url=base_url,
            models=",".join(models),
            vision_models="",
            custom=1,
            enabled=1,
        )

    def edit_service(self, name: str, **columns) -> None:
        self._db.save_service(name, **columns)

    def delete_service(self, name: str) -> None:
        self._db.delete_service(name)
