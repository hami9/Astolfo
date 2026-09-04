"""The credit is display-only, and it does not move."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astolfo import branding, master, settings_store
from astolfo.commands import about
from astolfo.config import ConfigError
from astolfo.db import open_database
from tests.conftest import FakeContext, FakeMessage, make_update


# -- fixed values ---------------------------------------------------------
def test_the_channel_and_creator_are_exactly_these(settings):
    """Pinned on purpose: a silent change to either is a bug, not a preference."""
    assert branding.CHANNEL == "hami294"
    assert branding.CHANNEL_URL == "https://t.me/hami294"
    assert branding.CREATOR == "ham1235i"
    assert branding.CREATOR_URL == "https://t.me/ham1235i"
    assert branding.SITE == "hami9.ir"
    assert branding.SITE_URL == "https://hami9.ir"
    assert branding.DISCORD_URL == "https://discord.gg/K33PnNafcD"


def test_every_about_text_names_both(settings):
    for locale in ("en", "fa"):
        text = branding.about(locale)
        assert branding.CHANNEL_URL in text
        assert branding.CREATOR_URL in text
        assert branding.SITE_URL in text
        assert branding.DISCORD_URL in text
        assert branding.CREATOR in branding.credit(locale)
        assert branding.SITE in branding.credit(locale)


def test_the_credit_cannot_be_overridden_from_the_panel(settings):
    """There is no setting behind it, so there is nothing to point at it."""
    assert "channel" not in settings_store.editable()
    assert "creator" not in settings_store.editable()


async def test_about_answers_in_the_configured_language(settings, rt, bot):
    rt.strings.locale = "fa"
    message = FakeMessage("/about")
    await about(make_update(message), FakeContext(rt, bot))
    assert branding.CHANNEL_URL in message.sent[0]


# -- ownership ------------------------------------------------------------
def _user(user_id: int, username: str | None = None):
    return SimpleNamespace(id=user_id, username=username, first_name="Someone", is_bot=False)


def test_a_configured_id_settles_it(settings):
    from astolfo.runtime import Runtime

    rt = Runtime.build(settings.replace(master_id=555))
    assert master.is_master(rt, _user(555)) is True
    assert master.is_master(rt, _user(556, username="ham1235i")) is False, "id wins over name"


def test_the_username_is_used_once_and_then_the_id(settings):
    from astolfo.runtime import Runtime

    rt = Runtime.build(settings.replace(master_id=0, master_username="ham1235i"))
    assert master.is_master(rt, _user(101, username="ham1235i")) is True
    assert rt.db.master_id() == 101

    # Whoever picks the username up later is a stranger to the bot.
    assert master.is_master(rt, _user(202, username="ham1235i")) is False
    assert master.is_master(rt, _user(101)) is True


def test_nobody_is_master_by_default(settings):
    from astolfo.runtime import Runtime

    rt = Runtime.build(settings.replace(master_id=0, master_username="ham1235i"))
    assert master.current(rt) is None
    assert master.is_master(rt, _user(303, username="someone_else")) is False
    assert master.is_master(rt, None) is False


def test_the_master_cannot_be_reassigned_through_settings(settings):
    db = open_database(settings.data_dir)
    for key in ("master_id", "master_username"):
        with pytest.raises(ConfigError):
            settings_store.set_override(db, key, "999", by=1)
