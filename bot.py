"""Network-specific Telegram sponsor-promotion bot with clear billing disclosure.

Designed for one Railway worker with a Railway PostgreSQL database.
Never requests login codes, OTPs, PINs, passwords, or unredacted SMS messages.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger("network_promo")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
NETWORKS = ("MTN", "GLO", "AIRTEL")
REVIEW_HOURS = 24
DATA_RETENTION_DAYS = 30

router = Router()
pool: asyncpg.Pool | None = None


class OfferFlow(StatesGroup):
    offer = State()
    proof_one = State()
    proof_two = State()
    reward = State()
    phone = State()


class AdminFlow(StatesGroup):
    edit_url = State()
    guide_photo = State()
    edit_billing = State()


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def network_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [("MTN", "network:MTN"), ("Glo", "network:GLO"), ("Airtel", "network:AIRTEL")],
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [("Edit MTN link", "admin:edit:MTN")],
        [("Edit Glo link", "admin:edit:GLO")],
        [("Edit Airtel link", "admin:edit:AIRTEL")],
        [("Edit official terms link", "admin:edit:TERMS")],
        [("MTN billing details", "admin:billing:MTN")],
        [("Glo billing details", "admin:billing:GLO")],
        [("Airtel billing details", "admin:billing:AIRTEL")],
        [("Set subscription step image", "admin:guide:SUBSCRIBE")],
        [("Set success step image", "admin:guide:SUCCESS")],
        [("Set SMS proof example", "admin:guide:SMS")],
        [("Clear subscription image", "admin:clear:SUBSCRIBE")],
        [("Clear success image", "admin:clear:SUCCESS")],
        [("Clear SMS example", "admin:clear:SMS")],
        [("Pending reviews", "admin:pending"), ("Statistics", "admin:stats")],
    )


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def valid_public_https_url(value: str) -> bool:
    """Only allow HTTPS public hostnames; no credentials or local/private IPs."""
    try:
        p = urlparse(value)
        if p.scheme.lower() != "https" or not p.hostname or p.username or p.password:
            return False
        if p.hostname.lower() in ("localhost",) or p.hostname.endswith((".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                return False
        except ValueError:
            pass  # Public DNS name (not an IP literal)
        return True
    except ValueError:
        return False


def normalize_nigeria_phone(text: str) -> str | None:
    digits = re.sub(r"[\s\-()]", "", text.strip()).lstrip("+")
    if not digits.isdigit():
        return None
    if digits.startswith("234") and len(digits) == 13:
        digits = "0" + digits[3:]
    if len(digits) != 11 or not digits.startswith("0") or digits[1] not in "789":
        return None
    return "+234" + digits[1:]


async def setting(key: str) -> str | None:
    assert pool is not None
    async with pool.acquire() as db:
        return await db.fetchval("SELECT value FROM settings WHERE key=$1", key)


async def save_setting(key: str, value: str) -> None:
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO settings (key,value) VALUES ($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            key, value,
        )


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        LOG.debug("Could not delete temporary instruction: %s", exc)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception as exc:
        LOG.warning("Message delivery to %s failed: %s", chat_id, exc)


async def create_tables() -> None:
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                network TEXT NOT NULL,
                proof_file_id TEXT NOT NULL,
                extra_file_id TEXT,
                reward_preference TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reminder_at TIMESTAMPTZ,
                reminder_sent BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS submissions_user_idx ON submissions(user_id);
            CREATE INDEX IF NOT EXISTS submissions_reminder_idx
                ON submissions(reminder_at) WHERE reminder_sent=FALSE;
        """)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎁 Welcome to Network Rewards!\n\n"
        "You’re one step away from entering for a chance to win up to ₦300,000. "
        "Rewards are subject to the official offer rules, not guaranteed.\n\n"
        "Select your mobile network:",
        reply_markup=network_keyboard(),
    )


@router.callback_query(F.data.startswith("network:"))
async def network_chosen(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    network = (query.data or "").split(":", 1)[-1]
    if network not in NETWORKS:
        return
    link = await setting("URL_" + network)
    terms = await setting("URL_TERMS")
    billing = await setting("BILLING_" + network)
    if not link or not terms or not billing:
        await safe_send(
            bot, query.from_user.id,
            "This network offer is not ready. The administrator must set its link, "
            "subscription charge/renewal details and official terms first."
        )
        return
    await state.clear()
    await state.set_state(OfferFlow.offer)
    await state.update_data(network=network)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await bot.send_message(
        query.from_user.id,
        f"📱 {network} — Step 1 of 3\n\n"
        "Open the sponsor website and enter your mobile number. "
        "You may need at least ₦250 airtime.\n\n"
        f"Subscription charge and renewal: {billing}\n"
        "Signing up may deduct airtime. Cancelling a bot entry does not cancel a mobile subscription.\n\n"
        "🏆 Prize entry is subject to eligibility and verification; a prize is not guaranteed.\n"
        f"Official terms: {terms}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🌐 Open {network} sponsor offer", url=link)],
            [InlineKeyboardButton(text="🔄 Choose another network", callback_data="offer:back")],
        ]),
    )
    # The short picture walkthrough runs separately so Telegram callbacks stay responsive.
    asyncio.create_task(show_walkthrough(bot, query.from_user.id, state, network, link, billing, terms))


async def show_walkthrough(bot: Bot, user_id: int, state: FSMContext,
                           network: str, link: str, billing: str, terms: str) -> None:
    """Show each configurable guide for three seconds, then present the offer."""
    guides = (
        ("GUIDE_SUBSCRIBE_FILE_ID", "Step 2 of 3: Check the subscription page, price and auto-renewal setting."),
        ("GUIDE_SUCCESS_FILE_ID", "Step 3 of 3: Complete the sponsor page and wait for its confirmation."),
    )
    for key, caption in guides:
        if await state.get_state() != OfferFlow.offer.state:
            return
        if (await state.get_data()).get("network") != network:
            return
        guide_file = await setting(key)
        if not guide_file and key == "GUIDE_SUBSCRIBE_FILE_ID":
            guide_file = await setting("GUIDE_FILE_ID")  # Existing image from V1.
        try:
            if guide_file:
                guide_message = await bot.send_photo(user_id, guide_file, caption=caption)
            else:
                guide_message = await bot.send_message(user_id, caption)
            await asyncio.sleep(3)
            await safe_delete(bot, user_id, guide_message.message_id)
        except Exception as exc:
            LOG.warning("Could not display guide %s: %s", key, exc)
            await asyncio.sleep(3)
    if await state.get_state() != OfferFlow.offer.state:
        return
    if (await state.get_data()).get("network") != network:
        return
    offer = await bot.send_message(user_id, "Your sponsor offer is ready.")
    asyncio.create_task(countdown(bot, user_id, offer.message_id, state,
                                  network, link, billing, terms))


@router.callback_query(F.data == "offer:back")
async def back_to_networks(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    await state.clear()
    await bot.send_message(query.from_user.id, "Choose your network:", reply_markup=network_keyboard())


async def countdown(bot: Bot, user_id: int, message_id: int, state: FSMContext,
                    network: str, link: str, billing: str, terms: str) -> None:
    """Visual walkthrough timer; the sponsor link stays active after zero."""
    for remaining in (30, 25, 20, 15, 10, 5, 0):
        if await state.get_state() != OfferFlow.offer.state:
            return
        data = await state.get_data()
        if data.get("network") != network:
            return
        caption = (
            f"🎁 {network} OFFER\n\n"
            "Tap the button to finish registering on the sponsor website.\n\n"
            f"Charge/auto-renewal: {billing}\n"
            "Rewards depend on the sponsor's rules and are not guaranteed.\n"
            f"Terms: {terms}\n\n"
            f"Guide countdown: {remaining}s (not an offer deadline)."
        )
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=message_id, text=caption,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"🎁 OPEN {network} OFFER", url=link)],
                    [InlineKeyboardButton(text="✅ I've completed registration", callback_data="offer:done")],
                ]),
            )
        except Exception as exc:
            LOG.warning("Offer countdown update skipped: %s", exc)
        if remaining:
            await asyncio.sleep(5)


async def delete_after(bot: Bot, chat_id: int, msg_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    await safe_delete(bot, chat_id, msg_id)


@router.callback_query(F.data == "offer:done", StateFilter(OfferFlow.offer))
async def done_offer(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    await state.set_state(OfferFlow.proof_one)
    guide_file = await setting("GUIDE_SMS_FILE_ID")
    if guide_file:
        try:
            guide = await bot.send_photo(
                query.from_user.id, guide_file,
                caption="Example: successful carrier subscription confirmation SMS.",
            )
            asyncio.create_task(delete_after(bot, query.from_user.id, guide.message_id, 3))
        except Exception as exc:
            LOG.warning("Could not display SMS example: %s", exc)
    await bot.send_message(
        query.from_user.id,
        "📸 Upload your proof here.\n\n"
        "Send a screenshot of the *successful subscription confirmation SMS* "
        "(not a verification code or login OTP). "
        "Hide personal messages and any security codes before sending. "
        "Your screenshot will be sent to campaign administrators.",
        parse_mode="Markdown",
    )


@router.message(StateFilter(OfferFlow.proof_one), F.photo)
async def first_proof(message: Message, state: FSMContext, bot: Bot) -> None:
    assert message.from_user is not None
    await state.update_data(proof_file_id=message.photo[-1].file_id)
    await message.answer("✅ Screenshot received. The team will check your entry.")
    await asyncio.sleep(2)
    if await state.get_state() != OfferFlow.proof_one.state:
        return
    await show_reward_choices(bot, message.from_user.id, state)


@router.message(StateFilter(OfferFlow.proof_one))
async def first_proof_missing(message: Message) -> None:
    await message.answer("Please upload a picture of a redacted receipt, or use /start to cancel.")


async def show_reward_choices(bot: Bot, user_id: int, state: FSMContext) -> None:
    await state.set_state(OfferFlow.reward)
    await bot.send_message(
        user_id,
        "🎁 Which reward would you prefer if selected as a winner?\n\n"
        "This is a preference only. Selection does not confirm winning or an award of ₦10,000.",
        reply_markup=keyboard(
            [("Cash preference", "reward:cash")],
            [("Airtime preference", "reward:airtime")],
        ),
    )


@router.message(StateFilter(OfferFlow.proof_two), F.photo)
async def extra_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    assert message.from_user is not None
    await state.update_data(extra_file_id=message.photo[-1].file_id)
    await message.answer("Additional redacted receipt received.")
    await show_reward_choices(bot, message.from_user.id, state)


@router.callback_query(F.data == "proof:skip", StateFilter(OfferFlow.proof_two))
async def skip_extra(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await show_reward_choices(bot, query.from_user.id, state)


@router.message(StateFilter(OfferFlow.proof_two))
async def extra_receipt_missing(message: Message) -> None:
    await message.answer("Send a redacted screenshot or tap 'Skip extra receipt' above. Never send OTPs.")


@router.callback_query(F.data.startswith("reward:"), StateFilter(OfferFlow.reward))
async def choose_reward(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    preference = (query.data or "").split(":")[-1]
    if preference not in ("cash", "airtime"):
        return
    await state.update_data(reward_preference=preference)
    await state.set_state(OfferFlow.phone)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await bot.send_message(
        query.from_user.id,
        "Enter your Nigerian mobile number for verification/contact (for example, 08012345678).\n"
        "It will be shared only with campaign admins for review. "
        "You can withdraw your submission with /delete_my_data.",
    )


@router.message(StateFilter(OfferFlow.phone), F.text)
async def save_submission(message: Message, state: FSMContext, bot: Bot) -> None:
    assert message.from_user is not None
    phone = normalize_nigeria_phone(message.text or "")
    if not phone:
        await message.answer("Invalid Nigerian mobile number. Use 08012345678 or +2348012345678.")
        return
    data = await state.get_data()
    assert pool is not None
    async with pool.acquire() as db:
        existing = await db.fetchval(
            "SELECT id FROM submissions WHERE user_id=$1 AND status='pending' LIMIT 1",
            message.from_user.id,
        )
        if existing:
            await state.clear()
            await message.answer(
                f"You already have pending entry #{existing}. We review one entry at a time."
            )
            return
        submission_id = await db.fetchval("""
            INSERT INTO submissions
              (user_id,network,proof_file_id,extra_file_id,reward_preference,phone)
            VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
        """, message.from_user.id, data["network"], data["proof_file_id"],
            data.get("extra_file_id"), data["reward_preference"], phone)
    await state.clear()
    await message.answer(
        f"Entry #{submission_id} received. Our team aims to review your submission "
        "within 24 hours; reward eligibility is not guaranteed. "
        "We will notify you when an admin approves or declines the entry.\n\n"
        "Would you like one reminder about your review status in 2 hours?",
        reply_markup=keyboard(
            [("Yes, remind me once", f"reminder:{submission_id}")],
            [("No reminder", "reminder:no")],
        ),
    )
    username = "@" + message.from_user.username if message.from_user.username else "(none)"
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"NEW ENTRY #{submission_id}\n"
                f"Telegram user: {message.from_user.id} ({username})\n"
                f"Network: {data['network']}\n"
                f"Preference: {data['reward_preference']}\n"
                f"Phone: {phone}\nStatus: pending\n"
                "Check receipts for privacy issues. Do not request SMS codes.",
                reply_markup=keyboard(
                    [("View receipts", f"review:proofs:{submission_id}")],
                    [("Approve", f"review:approve:{submission_id}"),
                     ("Decline", f"review:decline:{submission_id}")],
                ),
            )
            await bot.send_photo(admin, data["proof_file_id"], caption=f"Entry #{submission_id}: receipt")
            if data.get("extra_file_id"):
                await bot.send_photo(admin, data["extra_file_id"],
                                     caption=f"Entry #{submission_id}: optional redacted receipt")
        except Exception as exc:
            LOG.error("Failed to notify admin %s about entry %s: %s", admin, submission_id, exc)


@router.message(StateFilter(OfferFlow.phone))
async def missing_phone(message: Message) -> None:
    await message.answer("Please enter the phone number as text (not a contact card).")


@router.callback_query(F.data == "reminder:no")
async def skip_reminder(query: CallbackQuery) -> None:
    await query.answer("No reminder will be sent.")
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("reminder:"))
async def opt_in_reminder(query: CallbackQuery) -> None:
    try:
        submission_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        await query.answer("Invalid submission.")
        return
    assert pool is not None
    async with pool.acquire() as db:
        result = await db.execute("""
            UPDATE submissions SET reminder_at=NOW()+INTERVAL '2 hours', reminder_sent=FALSE
            WHERE id=$1 AND user_id=$2 AND status='pending' AND reminder_at IS NULL
        """, submission_id, query.from_user.id)
    await query.answer("One-time status reminder enabled." if result.endswith("1") else "No eligible pending entry.")
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    await message.answer(
        "Privacy: We collect your Telegram ID, chosen network, redacted proof images, "
        "reward preference and phone number solely to review your entry. Proof is sent "
        "to configured admins. Non-pending database entries are erased after 30 days. "
        "Admin Telegram message copies are not automatically removed. "
        "Deleting a bot entry does NOT cancel a sponsor/mobile subscription. "
        "Use /delete_my_data to erase your database submissions and stop reminders. "
        "Do not send passwords, OTPs or authentication SMS screenshots."
    )


@router.message(Command("delete_my_data"))
async def delete_my_data(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute("DELETE FROM submissions WHERE user_id=$1", message.from_user.id)
    await state.clear()
    await message.answer(
        "Your entries and scheduled reminders have been deleted from the bot database. "
        "If you previously sent proof, copies in administrators' Telegram chats require separate deletion."
    )


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Admin access only.")
        return
    await state.clear()
    await message.answer("Campaign admin panel:", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_action(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin access only", show_alert=True)
        return
    await query.answer()
    action = (query.data or "").split(":")
    if len(action) > 2 and action[1] == "edit":
        name = action[2]
        if name not in (*NETWORKS, "TERMS"):
            return
        await state.set_state(AdminFlow.edit_url)
        await state.update_data(edit_key="URL_" + name)
        await bot.send_message(query.from_user.id, f"Send the new HTTPS URL for {name}. Use /admin to cancel.")
    elif action[1] == "billing" and len(action) > 2 and action[2] in NETWORKS:
        network = action[2]
        await state.set_state(AdminFlow.edit_billing)
        await state.update_data(billing_key="BILLING_" + network)
        await bot.send_message(
            query.from_user.id,
            f"Send the ACTUAL {network} charge and auto-renewal/cancellation details "
            "(example: ₦50/day, auto-renews daily; cancel by ...). "
            "Verify this against the active sponsor landing page. /admin to cancel."
        )
    elif action[1] == "guide" and len(action) > 2 and action[2] in ("SUBSCRIBE", "SUCCESS", "SMS"):
        slot = action[2]
        await state.set_state(AdminFlow.guide_photo)
        await state.update_data(guide_key=f"GUIDE_{slot}_FILE_ID")
        await bot.send_message(query.from_user.id, f"Upload {slot} step image to display for 3 seconds.")
    elif action[1] == "clear" and len(action) > 2 and action[2] in ("SUBSCRIBE", "SUCCESS", "SMS"):
        key = f"GUIDE_{action[2]}_FILE_ID"
        await save_setting(key, "")
        if action[2] == "SUBSCRIBE":
            # Also clear the V1 image fallback so the removed image won't reappear.
            await save_setting("GUIDE_FILE_ID", "")
        await bot.send_message(query.from_user.id, f"Removed {action[2]} image.",
                               reply_markup=admin_keyboard())
    elif action[1] == "stats":
        assert pool is not None
        async with pool.acquire() as db:
            rows = await db.fetch("SELECT status,COUNT(*) AS total FROM submissions GROUP BY status")
        stats = "\n".join(f"{r['status']}: {r['total']}" for r in rows) or "No submissions yet"
        await bot.send_message(query.from_user.id, "Entries by status:\n" + stats)
    elif action[1] == "pending":
        assert pool is not None
        async with pool.acquire() as db:
            rows = await db.fetch(
                "SELECT id,user_id,network,reward_preference,phone FROM submissions "
                "WHERE status='pending' ORDER BY created_at DESC LIMIT 10"
            )
        if not rows:
            await bot.send_message(query.from_user.id, "No pending entries.")
        for row in rows:
            await bot.send_message(
                query.from_user.id,
                f"#{row['id']} | {row['network']} | {row['reward_preference']}\n"
                f"User: {row['user_id']} | Phone: {row['phone']}",
                reply_markup=keyboard(
                    [("View receipts", f"review:proofs:{row['id']}")],
                    [("Approve", f"review:approve:{row['id']}"),
                     ("Decline", f"review:decline:{row['id']}")],
                ),
            )


@router.message(StateFilter(AdminFlow.edit_url), F.text)
async def admin_new_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    url = (message.text or "").strip()
    if not valid_public_https_url(url):
        await message.answer("Use a full public https:// URL (no private/local addresses).")
        return
    key = (await state.get_data())["edit_key"]
    await save_setting(key, url)
    await state.clear()
    await message.answer(f"Updated {key} successfully.", reply_markup=admin_keyboard())


@router.message(StateFilter(AdminFlow.edit_billing), F.text)
async def admin_new_billing(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    details = (message.text or "").strip()
    if len(details) < 15 or len(details) > 240:
        await message.answer("Provide 15–240 characters with the cost, renewal and cancellation instructions.")
        return
    key = (await state.get_data())["billing_key"]
    await save_setting(key, details)
    await state.clear()
    await message.answer(f"Saved {key}.", reply_markup=admin_keyboard())


@router.message(StateFilter(AdminFlow.guide_photo), F.photo)
async def admin_new_guide(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    key = (await state.get_data()).get("guide_key", "GUIDE_SUBSCRIBE_FILE_ID")
    await save_setting(key, message.photo[-1].file_id)
    await state.clear()
    await message.answer(f"Saved {key}.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("review:"))
async def review_entry(query: CallbackQuery, bot: Bot) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin access only", show_alert=True)
        return
    fields = (query.data or "").split(":")
    if len(fields) != 3 or fields[1] not in ("approve", "decline", "proofs") or not fields[2].isdigit():
        await query.answer("Invalid action")
        return
    submission_id = int(fields[2])
    assert pool is not None
    if fields[1] == "proofs":
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT proof_file_id,extra_file_id FROM submissions WHERE id=$1", submission_id
            )
        if not row:
            await query.answer("Entry not found or deleted")
            return
        await query.answer("Sending stored receipts")
        await bot.send_photo(query.from_user.id, row["proof_file_id"],
                             caption=f"Entry #{submission_id}: registration receipt")
        if row["extra_file_id"]:
            await bot.send_photo(query.from_user.id, row["extra_file_id"],
                                 caption=f"Entry #{submission_id}: additional receipt")
        return
    new_status = "approved" if fields[1] == "approve" else "declined"
    async with pool.acquire() as db:
        user_id = await db.fetchval("""
            UPDATE submissions SET status=$1,updated_at=NOW(),reminder_at=NULL
            WHERE id=$2 AND status='pending' RETURNING user_id
        """, new_status, submission_id)
    if user_id is None:
        await query.answer("Entry already reviewed or deleted")
        return
    await query.answer(f"Entry #{submission_id} {new_status}")
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await safe_send(
        bot, user_id,
        f"Update on entry #{submission_id}: {new_status.upper()}.\n"
        + ("Your entry passed the initial review. Check the published terms for "
           "reward eligibility and timing; no payment has been made by this bot."
           if new_status == "approved" else
           "Your entry was not approved. Contact the sponsor through its official support channel for details."),
    )


async def reminder_worker(bot: Bot) -> None:
    assert pool is not None
    while True:
        try:
            async with pool.acquire() as db:
                # Claim due reminders safely, once; one worker replica is still recommended.
                rows = await db.fetch("""
                    UPDATE submissions SET reminder_sent=TRUE
                    WHERE id IN (
                        SELECT id FROM submissions
                        WHERE status='pending' AND reminder_sent=FALSE
                          AND reminder_at IS NOT NULL AND reminder_at<=NOW()
                        ORDER BY reminder_at ASC LIMIT 100
                        FOR UPDATE SKIP LOCKED
                    ) RETURNING id,user_id
                """)
                await db.execute("""
                    DELETE FROM submissions
                    WHERE created_at < NOW() - INTERVAL '30 days'
                """)
            for row in rows:
                await safe_send(
                    bot, row["user_id"],
                    f"Two-hour review reminder for entry #{row['id']}: your submission "
                    "is still awaiting admin review. You do not need to subscribe or pay again."
                )
        except Exception as exc:
            LOG.exception("Reminder worker error: %s", exc)
        await asyncio.sleep(60)


async def main() -> None:
    global pool
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is missing")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL environment variable is missing")
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_IDS must list at least one numeric Telegram user ID")
    logging.basicConfig(level=logging.INFO)
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    await create_tables()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    reminder_task = asyncio.create_task(reminder_worker(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
