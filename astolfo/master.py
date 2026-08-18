"""Who is allowed to run the bot.

One person owns the panel. `MASTER_ID` names them by numeric Telegram id and
settles it; without it, the first person to appear whose username matches
`MASTER_USERNAME` is claimed once and their id written down. From that moment the
username is never consulted again — usernames can be given up and taken by
somebody else, ids cannot.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def current(rt) -> int | None:
    """The master's numeric id, or None while nobody has been recognised yet."""
    return rt.settings.master_id or rt.db.master_id()


def is_master(rt, user) -> bool:
    if user is None or getattr(user, "is_bot", False):
        return False

    configured = rt.settings.master_id
    if configured:
        return user.id == configured

    claimed = rt.db.master_id()
    if claimed:
        return user.id == claimed

    wanted = (rt.settings.master_username or "").lstrip("@").lower()
    if wanted and (user.username or "").lower() == wanted:
        rt.db.claim_master(user.id, name=user.first_name or "", username=user.username or "")
        rt.db.record(actor=user.id, action="claim_master", detail=f"@{user.username}")
        log.warning("master claimed by @%s (id %s)", user.username, user.id)
        return True
    return False


def describe(rt) -> str:
    """How the owner is recognised, for the panel and the startup log."""
    if rt.settings.master_id:
        return f"id {rt.settings.master_id} (from the environment)"
    claimed = rt.db.master_id()
    if claimed:
        return f"id {claimed} (claimed by @{rt.settings.master_username})"
    return f"nobody yet — waiting for @{rt.settings.master_username} to say something"
