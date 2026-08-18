"""Donations through Telegram Stars.

Stars need no payment provider, no merchant account and no card on the sender's
side, which makes them the one payment rail that works for everyone in a chat.
"""

from __future__ import annotations

import logging

from telegram import LabeledPrice, Update
from telegram.ext import ContextTypes

from . import runtime

log = logging.getLogger(__name__)

CURRENCY = "XTR"  # Telegram Stars
PAYLOAD = "astolfo-donation"
MIN_STARS = 1
MAX_STARS = 100_000


def _amount(rt, args: list[str]) -> int | None:
    """Requested star count, or None when it is not a usable number."""
    if not args:
        return rt.settings.donate_amounts[0] if rt.settings.donate_amounts else 15
    try:
        value = int(args[0])
    except ValueError:
        return None
    return value if MIN_STARS <= value <= MAX_STARS else None


async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    message = update.effective_message

    if not rt.settings.donate_enabled:
        return

    amount = _amount(rt, context.args or [])
    if amount is None:
        suggestions = ", ".join(str(a) for a in rt.settings.donate_amounts)
        await message.reply_text(rt.strings("donate_bad_amount", amounts=suggestions))
        return

    try:
        await context.bot.send_invoice(
            chat_id=message.chat_id,
            title=rt.strings("donate_title"),
            description=rt.strings("donate_description"),
            payload=PAYLOAD,
            provider_token="",  # Stars need no provider
            currency=CURRENCY,
            prices=[LabeledPrice(label=rt.strings("donate_label"), amount=amount)],
        )
    except Exception as exc:
        log.warning("could not send the donation invoice: %s", exc)
        await message.reply_text(rt.strings("donate_unavailable"))


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram asks for confirmation seconds before charging; always accept ours."""
    query = update.pre_checkout_query
    if query.invoice_payload == PAYLOAD:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="unknown invoice")


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime.get(context)
    message = update.effective_message
    payment = message.successful_payment
    stars = payment.total_amount

    rt.budget.record_donation(stars)
    log.info("received %d stars from chat %s", stars, message.chat_id)
    await message.reply_text(rt.strings("donate_thanks", stars=stars))
