"""Who may touch the panel, and what happens to everyone else.

Two rules do the work. The panel only exists in the owner's private chat, so a
group can never see it, and every button press is checked again against the
owner's numeric id — a panel message can be forwarded, and the buttons travel
with it.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from telegram import Update
from telegram.constants import ChatType

from .. import master, runtime

log = logging.getLogger(__name__)

# Someone poking at the panel gets silence, not an error message: a reply would
# confirm the command exists. The count is kept only to stop the log filling up.
ATTEMPT_WINDOW = 600.0
LOG_AFTER = 3

_attempts: dict[int, list[float]] = defaultdict(list)


def note_refusal(rt, user, what: str) -> None:
    if user is None:
        return
    now = time.monotonic()
    recent = [t for t in _attempts[user.id] if now - t < ATTEMPT_WINDOW]
    recent.append(now)
    _attempts[user.id] = recent

    if len(recent) <= LOG_AFTER:
        log.warning(
            "refused %s for @%s (id %s), attempt %d",
            what,
            user.username or "?",
            user.id,
            len(recent),
        )
    if len(recent) == LOG_AFTER + 1:
        log.warning("id %s keeps trying the panel; further attempts are not logged", user.id)
        rt.db.record(actor=user.id, action="panel_refused", detail=f"@{user.username or '?'}")


def allowed(update: Update, context) -> bool:
    """True only for the owner, and only in their own private chat."""
    rt = runtime.get(context)
    user = update.effective_user
    chat = update.effective_chat

    if chat is not None and chat.type != ChatType.PRIVATE:
        # No refusal counted: this is usually somebody's honest mistake in a group.
        return False
    if not master.is_master(rt, user):
        note_refusal(rt, user, "the panel")
        return False
    return True


def audit(rt, user, action: str, detail: str = "") -> None:
    rt.db.record(actor=getattr(user, "id", None), action=action, detail=detail)
