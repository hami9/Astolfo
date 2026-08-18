"""Donations through Telegram Stars."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astolfo import donate
from tests.conftest import FakeBot, FakeContext, FakeMessage, make_update


class InvoiceBot(FakeBot):
    def __init__(self, fail: bool = False):
        super().__init__()
        self.invoices: list[dict] = []
        self.fail = fail

    async def send_invoice(self, **kwargs):
        if self.fail:
            raise RuntimeError("payments unavailable")
        self.invoices.append(kwargs)


async def run_donate(rt, args, bot=None):
    bot = bot or InvoiceBot()
    message = FakeMessage("/donate")
    context = FakeContext(rt, bot)
    context.args = args
    await donate.donate(make_update(message), context)
    return message, bot


async def test_default_amount_is_the_first_suggestion(rt):
    message, bot = await run_donate(rt, [])

    invoice = bot.invoices[0]
    assert invoice["currency"] == "XTR", "Stars, so no payment provider is needed"
    assert invoice["provider_token"] == ""
    assert invoice["prices"][0].amount == rt.settings.donate_amounts[0]
    assert invoice["payload"] == donate.PAYLOAD
    assert not message.sent, "the invoice itself is the message"


async def test_explicit_amount_is_used(rt):
    _, bot = await run_donate(rt, ["50"])
    assert bot.invoices[0]["prices"][0].amount == 50


@pytest.mark.parametrize("bad", [["abc"], ["0"], ["-5"], ["999999999"]])
async def test_unusable_amounts_are_refused(rt, bad):
    message, bot = await run_donate(rt, bad)
    assert not bot.invoices
    assert message.sent, "the user is told what a valid amount looks like"


async def test_disabled_donations_stay_silent(rt):
    rt.settings = rt.settings.replace(donate_enabled=False)
    message, bot = await run_donate(rt, [])
    assert not bot.invoices and not message.sent


async def test_failure_to_invoice_is_reported_in_character(rt):
    message, bot = await run_donate(rt, [], bot=InvoiceBot(fail=True))
    assert not bot.invoices
    assert message.sent == [rt.strings("donate_unavailable")]


async def test_precheckout_accepts_only_our_invoice(rt):
    answers: list[dict] = []

    def query(payload):
        return SimpleNamespace(
            invoice_payload=payload,
            answer=lambda **kw: answers.append(kw) or _done(),
        )

    async def _done():
        return None

    update = SimpleNamespace(pre_checkout_query=query(donate.PAYLOAD))
    await donate.precheckout(update, FakeContext(rt, FakeBot()))
    assert answers[-1]["ok"] is True

    update = SimpleNamespace(pre_checkout_query=query("someone-elses-invoice"))
    await donate.precheckout(update, FakeContext(rt, FakeBot()))
    assert answers[-1]["ok"] is False


async def test_payment_is_thanked_and_recorded(rt):
    message = FakeMessage("")
    message.successful_payment = SimpleNamespace(total_amount=50, currency="XTR")

    await donate.paid(make_update(message), FakeContext(rt, FakeBot()))

    assert "50" in message.sent[0]
    assert rt.budget.summary()["stars_today"] == 50


async def test_donations_survive_a_restart(rt, settings):
    from astolfo.budget import BudgetTracker

    rt.budget.record_donation(120)
    assert BudgetTracker(settings).summary()["stars_today"] == 120
