"""Network Rewards: two transparent sponsor tasks, receipt review and channel join.

Railway-ready aiogram 3 long polling with PostgreSQL.
Important: proof submission is NOT automatic sponsor verification or prize approval.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
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
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("network_rewards")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {int(v.strip()) for v in os.getenv("ADMIN_IDS", "").split(",") if v.strip().isdigit()}
NETWORKS = ("MTN", "GLO", "AIRTEL")
DEFAULT_REMINDER = (
    "🎁 Your Network Rewards entry is still available. Complete both sponsor tasks "
    "and submit your receipts to enter the draw, subject to the published terms. "
    "Check any subscription charges or auto-renewal before joining."
)

router = Router()
pool: asyncpg.Pool | None = None


class AdminState(StatesGroup):
    url = State()
    billing = State()
    guide = State()
    channel = State()
    channel_url = State()
    reminder = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=data) for title, data in row]
        for row in rows
    ])


def url_kb(link: str, button: str, next_label: str, next_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button, url=link)],
        [InlineKeyboardButton(text=next_label, callback_data=next_data)],
    ])


def network_kb() -> InlineKeyboardMarkup:
    return kb([("MTN", "network:MTN"), ("Glo", "network:GLO"), ("Airtel", "network:AIRTEL")])


def admin_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    for network in NETWORKS:
        rows.append([(f"{network} — Task 1 link", f"admin:url:{network}:1")])
        rows.append([(f"{network} — Task 2 link", f"admin:url:{network}:2")])
        rows.append([(f"{network} — Task 1 billing", f"admin:billing:{network}:1")])
        rows.append([(f"{network} — Task 2 billing", f"admin:billing:{network}:2")])
    rows.extend([
        [("Official promotion terms", "admin:url:TERMS:0")],
        [("Partner channel ID / @username", "admin:channel")],
        [("Partner channel join URL", "admin:channelurl")],
        [("Subscription guide image", "admin:guide:SUBSCRIBE")],
        [("Success page guide image", "admin:guide:SUCCESS")],
        [("SMS receipt guide image", "admin:guide:SMS")],
        [("Reminder text", "admin:reminder")],
        [("Set reminder picture", "admin:guide:REMINDER")],
        [("Clear reminder picture", "admin:clear:REMINDER")],
        [("Pending entries", "admin:pending"), ("Statistics", "admin:stats")],
    ])
    return kb(*rows)


def is_admin(uid: int | None) -> bool:
    return uid is not None and uid in ADMIN_IDS


def valid_public_https_url(value: str) -> bool:
    try:
        p = urlparse(value)
        if p.scheme.lower() != "https" or not p.hostname or p.username or p.password:
            return False
        if p.hostname.lower() == "localhost" or p.hostname.lower().endswith((".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"[\s()\-+]", "", value.strip())
    if digits.startswith("234") and len(digits) == 13:
        digits = "0" + digits[3:]
    if len(digits) != 11 or not digits.isdecimal() or not digits.startswith("0") or digits[1] not in "789":
        return None
    return "+234" + digits[1:]


def task_key(network: str, task: int, kind: str) -> str:
    # Legacy settings remain Task 1 so Railway upgrades preserve saved links.
    return f"{kind}_{network}" + ("_2" if task == 2 else "")


async def get_setting(key: str) -> str | None:
    assert pool is not None
    async with pool.acquire() as db:
        return await db.fetchval("SELECT value FROM settings WHERE key=$1", key)


async def set_setting(key: str, val: str) -> None:
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES($1,$2) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, val
        )


async def init_db() -> None:
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS submissions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL, network TEXT NOT NULL,
            proof_file_id TEXT NOT NULL, extra_file_id TEXT,
            reward_preference TEXT NOT NULL, phone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reminder_at TIMESTAMPTZ, reminder_sent BOOLEAN NOT NULL DEFAULT FALSE
        );
        ALTER TABLE submissions ADD COLUMN IF NOT EXISTS channel_verified BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE INDEX IF NOT EXISTS submissions_user_idx ON submissions(user_id);
        CREATE TABLE IF NOT EXISTS reminder_subscribers (
            user_id BIGINT PRIMARY KEY, stage TEXT NOT NULL DEFAULT 'started',
            remind_at TIMESTAMPTZ NOT NULL, reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS campaign_progress (
            user_id BIGINT PRIMARY KEY, network TEXT NOT NULL,
            stage TEXT NOT NULL, proof1 TEXT, proof2 TEXT,
            reward_preference TEXT, phone TEXT, unlock_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS progress_unlock_idx
            ON campaign_progress(unlock_at) WHERE stage='unlock2';
        """)


async def progress(uid: int) -> asyncpg.Record | None:
    assert pool is not None
    async with pool.acquire() as db:
        return await db.fetchrow("SELECT * FROM campaign_progress WHERE user_id=$1", uid)


async def schedule_reminder(uid: int, stage: str) -> None:
    assert pool is not None
    async with pool.acquire() as db:
        await db.execute("""
        INSERT INTO reminder_subscribers(user_id,stage,remind_at,reminder_sent)
        VALUES($1,$2,NOW()+INTERVAL '2 hours',FALSE)
        ON CONFLICT(user_id) DO UPDATE SET stage=EXCLUDED.stage,
            remind_at=EXCLUDED.remind_at,reminder_sent=FALSE,updated_at=NOW()
        """, uid, stage)


async def safe_send(bot: Bot, uid: int, text: str, **opts: Any) -> None:
    try:
        await bot.send_message(uid, text, **opts)
    except Exception as exc:
        LOG.warning("Cannot deliver to %s: %s", uid, exc)


async def offer_settings(network: str, task: int) -> tuple[str | None, str | None, str | None]:
    return (
        await get_setting(task_key(network, task, "URL")),
        await get_setting(task_key(network, task, "BILLING")),
        await get_setting("URL_TERMS"),
    )


async def show_task(bot: Bot, uid: int, network: str, task: int) -> bool:
    link, billing, terms = await offer_settings(network, task)
    if not link or not billing or not terms:
        await safe_send(bot, uid, f"{network} Task {task} isn't configured yet. Contact the organizer.")
        return False
    heading = "FIRST CHALLENGE" if task == 1 else "SECOND CHALLENGE"
    text = (
        f"🎯 {network} REWARDS • {heading}\n"
        f"Progress: {'□□' if task == 1 else '■□'} (proofs received out of 2)\n\n"
        f"TASK {task} OF 2\n"
        "1. Open the official sponsor website.\n"
        "2. Read the charge and renewal information before registering.\n"
        "3. If you choose to subscribe, finish the registration and obtain the carrier receipt.\n"
        "4. Return here and tap 'Submit task proof'.\n\n"
        f"Cost / renewal / cancellation: {billing}\n"
        f"Official promotion terms: {terms}\n\n"
        "Entry and prize eligibility are subject to manual review. "
        "Submitting a screenshot does not mean your subscription was verified or that you won."
    )
    sent = await bot.send_message(uid, text,
        reply_markup=url_kb(link, f"🌐 OPEN {network} TASK {task}",
                            f"📸 Submit Task {task} proof", f"task:done:{task}"))
    if task == 1:
        for key, caption in (
            ("GUIDE_SUBSCRIBE_FILE_ID", "Example subscription page: check the actual price and renewal setting."),
            ("GUIDE_SUCCESS_FILE_ID", "Example success page: verify the service and look for the carrier receipt."),
        ):
            fid = await get_setting(key)
            if not fid and key == "GUIDE_SUBSCRIBE_FILE_ID":
                fid = await get_setting("GUIDE_FILE_ID")
            if fid:
                try:
                    await bot.send_photo(uid, fid, caption=caption)
                except Exception as exc:
                    LOG.warning("Guide unavailable: %s", exc)
    asyncio.create_task(countdown(bot, uid, sent.message_id, network, task, link, billing, terms))
    return True


async def countdown(bot: Bot, uid: int, msgid: int, network: str, task: int,
                    link: str, billing: str, terms: str) -> None:
    # Countdown is for walkthrough pacing, not a time-limited deal or urgency claim.
    for seconds in (30, 25, 20, 15, 10, 5, 0):
        data = await progress(uid)
        if not data or data["stage"] != f"task{task}" or data["network"] != network:
            return
        try:
            await bot.edit_message_reply_markup(chat_id=uid, message_id=msgid,
                reply_markup=url_kb(link, f"🎁 OPEN {network} TASK {task} • GUIDE {seconds}s",
                                    f"📸 Submit Task {task} proof", f"task:done:{task}"))
        except Exception as exc:
            LOG.debug("Countdown edit ignored: %s", exc)
        if seconds:
            await asyncio.sleep(5)


async def choose_network(bot: Bot, uid: int, network: str) -> None:
    if network not in NETWORKS:
        return
    incomplete = []
    for task in (1, 2):
        link, billing, terms = await offer_settings(network, task)
        if not link or not billing or not terms:
            incomplete.append(f"Task {task}: link, billing, or terms")
    if incomplete:
        await safe_send(bot, uid, f"{network} is not ready. Admin must configure:\n" + "\n".join(incomplete))
        return
    # Preserve any unfinished work instead of silently clearing uploaded receipts.
    unfinished = await progress(uid)
    if unfinished and unfinished["stage"] != "done":
        await safe_send(bot, uid,
            f"You already have an unfinished {unfinished['network']} entry. "
            "Use /resume to continue or /restart to discard the bot entry and pick another network. "
            "Restarting does not cancel sponsor subscriptions.")
        return
    # Existing pending submissions cannot silently be restarted or counted twice.
    assert pool is not None
    async with pool.acquire() as db:
        existing = await db.fetchval(
            "SELECT id FROM submissions WHERE user_id=$1 AND status='pending' LIMIT 1", uid)
    if existing:
        await safe_send(bot, uid, f"Your entry #{existing} is already under manual review. "
                        "We cannot create another entry until that review is finished.")
        return
    first_charge = await get_setting(task_key(network, 1, "BILLING"))
    second_charge = await get_setting(task_key(network, 2, "BILLING"))
    terms = await get_setting("URL_TERMS")
    await safe_send(bot, uid,
        f"📋 {network} CHALLENGE — TWO SPONSOR TASKS\n\n"
        f"Task 1 cost / renewal: {first_charge}\n\n"
        f"Task 2 cost / renewal: {second_charge}\n\n"
        "These are two separate offers; completing both may result in separate airtime charges. "
        "The organizer only reviews evidence; the bot cannot confirm subscriptions automatically.\n"
        f"Promotion terms: {terms}")
    async with pool.acquire() as db:
        await db.execute("""
        INSERT INTO campaign_progress(user_id,network,stage)
        VALUES($1,$2,'task1')
        ON CONFLICT(user_id) DO UPDATE SET network=EXCLUDED.network,stage='task1',
            proof1=NULL,proof2=NULL,reward_preference=NULL,phone=NULL,
            unlock_at=NULL,updated_at=NOW()
        """, uid, network)
    await show_task(bot, uid, network, 1)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user:
        await schedule_reminder(message.from_user.id, "started")
    await message.answer(
        "🎁 NETWORK REWARDS CHALLENGE\n\n"
        "Pick your network, complete two sponsor tasks and submit your proof "
        "for review. Eligible entries may participate in the prize draw; prizes are not guaranteed.\n\n"
        "These are two separate sponsor tasks. Each can have its own airtime cost "
        "and auto-renewing subscription. You will see both prices before starting.\n\n"
        "Choose your network:", reply_markup=network_kb()
    )


@router.message(Command("restart"))
async def restart(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    assert pool is not None
    async with pool.acquire() as db:
        existing = await db.fetchval(
            "SELECT id FROM submissions WHERE user_id=$1 AND status='pending' LIMIT 1", uid)
        if existing:
            await message.answer(f"Entry #{existing} is already pending review and cannot be restarted.")
            return
        await db.execute("DELETE FROM campaign_progress WHERE user_id=$1", uid)
    await message.answer(
        "Your unfinished bot entry has been reset; any sponsor subscription continues "
        "until you cancel it with the mobile provider. Choose your network:",
        reply_markup=network_kb())


@router.message(Command("resume"))
async def resume(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    data = await progress(message.from_user.id)
    if not data or data["stage"] == "done":
        await message.answer("Start a new entry with /start.")
        return
    uid, stage, network = message.from_user.id, data["stage"], data["network"]
    if stage == "task1":
        await show_task(bot, uid, network, 1)
    elif stage == "proof1":
        await ask_for_proof(bot, uid, 1)
    elif stage == "unlock2":
        await message.answer("Task 1 proof received. Task 2 will unlock shortly; it is not yet verified.")
    elif stage == "task2":
        await show_task(bot, uid, network, 2)
    elif stage == "proof2":
        await ask_for_proof(bot, uid, 2)
    elif stage == "reward":
        await show_rewards(bot, uid)
    elif stage == "phone":
        await message.answer("Please send your Nigerian mobile number (e.g. 08012345678).")
    elif stage == "channel":
        await show_channel(bot, uid)


@router.callback_query(F.data.startswith("network:"))
async def network_chosen(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    network = (query.data or "").split(":")[-1]
    if network not in NETWORKS:
        return
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await choose_network(bot, query.from_user.id, network)


@router.callback_query(F.data.startswith("task:done:"))
async def task_done(query: CallbackQuery, bot: Bot) -> None:
    taskstr = (query.data or "").split(":")[-1]
    if taskstr not in ("1", "2"):
        await query.answer("Invalid task")
        return
    task = int(taskstr)
    uid = query.from_user.id
    assert pool is not None
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            UPDATE campaign_progress SET stage=$1,updated_at=NOW()
            WHERE user_id=$2 AND stage=$3 RETURNING network
        """, f"proof{task}", uid, f"task{task}")
    if not row:
        await query.answer("This task has already moved on. Use /resume.", show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await ask_for_proof(bot, uid, task)


async def ask_for_proof(bot: Bot, uid: int, task: int) -> None:
    if task == 1:
        fid = await get_setting("GUIDE_SMS_FILE_ID")
        if fid:
            try:
                await bot.send_photo(uid, fid, caption="Example of a successful carrier subscription receipt.")
            except Exception:
                pass
    await safe_send(
        bot, uid,
        f"📸 TASK {task}/2 — UPLOAD PROOF\n\n"
        "Send a screenshot of the carrier's successful subscription confirmation. "
        "Hide security codes, OTPs, private conversations and unrelated personal data.\n\n"
        "Your proof will be reviewed by campaign admins. An uploaded photo does not prove registration automatically."
    )


@router.message(StateFilter(None), F.photo)
async def participant_photo(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    data = await progress(uid)
    if not data or data["stage"] not in ("proof1", "proof2"):
        await message.answer("To submit task proof, choose your network with /start or continue with /resume.")
        return
    task = 1 if data["stage"] == "proof1" else 2
    field = "proof1" if task == 1 else "proof2"
    next_stage = "unlock2" if task == 1 else "reward"
    assert pool is not None
    async with pool.acquire() as db:
        updated = await db.fetchval(f"""
            UPDATE campaign_progress SET {field}=$1, stage=$2,
                unlock_at=CASE WHEN $2='unlock2' THEN NOW()+INTERVAL '10 seconds' ELSE NULL END,
                updated_at=NOW()
            WHERE user_id=$3 AND stage=$4 RETURNING network
        """, message.photo[-1].file_id, next_stage, uid, f"proof{task}")
    if not updated:
        await message.answer("That task is already recorded. Use /resume.")
        return
    if task == 1:
        await message.answer(
            "✅ Task 1 screenshot received.\n"
            "Status: awaiting manual verification — not yet approved.\n\n"
            "⏳ Task 2 opens automatically in about 10 seconds. "
            "You can complete it while Task 1 is being reviewed."
        )
    else:
        await message.answer("✅ Both task screenshots received. Verification is pending; choose your prize preference.")
        await show_rewards(bot, uid)


async def show_rewards(bot: Bot, uid: int) -> None:
    await safe_send(bot, uid,
        "🏆 REWARD PREFERENCE\n\n"
        "Which prize would you prefer if selected as an eligible winner? "
        "This selection does not mean that you have won.",
        reply_markup=kb([("Cash prize", "reward:cash"), ("Airtime prize", "reward:airtime")])
    )


@router.callback_query(F.data.startswith("reward:"))
async def reward_chosen(query: CallbackQuery, bot: Bot) -> None:
    pref = (query.data or "").split(":")[-1]
    if pref not in ("cash", "airtime"):
        await query.answer("Invalid preference")
        return
    assert pool is not None
    async with pool.acquire() as db:
        result = await db.execute("""
            UPDATE campaign_progress SET stage='phone',reward_preference=$1,updated_at=NOW()
            WHERE user_id=$2 AND stage='reward'
        """, pref, query.from_user.id)
    if result.endswith("0"):
        await query.answer("This step has already been completed. Use /resume.", show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await safe_send(bot, query.from_user.id,
        "📱 Enter your Nigerian phone number for organizer contact and potential prize fulfillment. "
        "It will only be shared with campaign admins. You can delete your entry with /delete_my_data."
    )


@router.message(Command("myid"))
async def show_my_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Your Telegram user ID: {message.from_user.id}")


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Admin access only.")
        return
    await state.clear()
    await message.answer("Network Rewards • Admin panel", reply_markup=admin_kb())


@router.message(Command("version"))
async def version_check(message: Message) -> None:
    await message.answer("Network Rewards Bot V4.2 — ADMIN ROUTE FIX ACTIVE")


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def participant_text(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    data = await progress(uid)
    if not data or data["stage"] != "phone":
        await message.answer("Send /start to begin or /resume to continue an existing entry.")
        return
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer("Enter a valid Nigerian number, such as 08012345678.")
        return
    assert pool is not None
    async with pool.acquire() as db:
        result = await db.execute("""
            UPDATE campaign_progress SET phone=$1,stage='channel',updated_at=NOW()
            WHERE user_id=$2 AND stage='phone'
        """, phone, uid)
    if result.endswith("0"):
        await message.answer("Your contact details have already been received. Use /resume.")
        return
    await show_channel(bot, uid)


async def show_channel(bot: Bot, uid: int) -> None:
    channel = await get_setting("PARTNER_CHANNEL")
    join_url = await get_setting("PARTNER_JOIN_URL")
    if not channel or not join_url:
        await safe_send(bot, uid, "The announcement channel isn't configured yet. Your tasks are saved. Contact the organizer or use /resume later.")
        return
    await bot.send_message(uid,
        "📣 WINNER ANNOUNCEMENTS\n\n"
        "Our partner Telegram channel is where organizers plan to publish verified winners. "
        "If the published rules require channel membership, join the channel and confirm below. "
        "Joining does not guarantee selection or a prize.",
        reply_markup=url_kb(join_url, "📢 JOIN ANNOUNCEMENTS CHANNEL",
                            "✅ Check my channel membership", "channel:check")
    )


async def finalize_entry(bot: Bot, uid: int, channel_verified: bool) -> None:
    assert pool is not None
    async with pool.acquire() as db:
        async with db.transaction():
            data = await db.fetchrow("""
                SELECT * FROM campaign_progress WHERE user_id=$1 AND stage='channel' FOR UPDATE
            """, uid)
            if not data or not all(data[k] for k in ("proof1", "proof2", "reward_preference", "phone")):
                await safe_send(bot, uid, "This entry is already submitted or incomplete. Use /resume.")
                return
            existing = await db.fetchval("""
                SELECT id FROM submissions WHERE user_id=$1 AND status='pending' LIMIT 1
            """, uid)
            if existing:
                entry_id = existing
            else:
                entry_id = await db.fetchval("""
                    INSERT INTO submissions
                    (user_id,network,proof_file_id,extra_file_id,reward_preference,phone,channel_verified)
                    VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id
                """, uid, data["network"], data["proof1"], data["proof2"],
                    data["reward_preference"], data["phone"], channel_verified)
            await db.execute("""
                UPDATE campaign_progress SET stage='done',updated_at=NOW() WHERE user_id=$1
            """, uid)
    await schedule_reminder(uid, "completed")
    await safe_send(bot, uid,
        f"🎉 YOUR TWO TASKS ARE SUBMITTED\n\n"
        f"Entry number: #{entry_id}\n"
        "Proof for both tasks: received, awaiting admin review\n"
        f"Channel membership: {'verified' if channel_verified else 'requires manual review'}\n\n"
        "The organizer aims to review entries within 24 hours. "
        "Your entry is not automatically approved and winning is not guaranteed. "
        "Any winners will be announced through the official channel in accordance with the published rules."
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id,
                f"NEW TWO-TASK ENTRY #{entry_id}\n"
                f"User: {uid}\nNetwork: {data['network']}\n"
                f"Reward preference: {data['reward_preference']}\nPhone: {data['phone']}\n"
                f"Channel verified: {channel_verified}\nStatus: pending\n"
                "Sponsor verification has NOT been performed by the bot.",
                reply_markup=kb(
                    [("View both receipts", f"review:proofs:{entry_id}")],
                    [("Approve entry", f"review:approve:{entry_id}"),
                     ("Decline entry", f"review:decline:{entry_id}")],
                ))
            await bot.send_photo(admin_id, data["proof1"], caption=f"Entry #{entry_id} • Task 1 proof")
            await bot.send_photo(admin_id, data["proof2"], caption=f"Entry #{entry_id} • Task 2 proof")
        except Exception as exc:
            LOG.warning("Admin notification for %s failed: %s", entry_id, exc)


@router.callback_query(F.data == "channel:check")
async def channel_check(query: CallbackQuery, bot: Bot) -> None:
    uid = query.from_user.id
    data = await progress(uid)
    if not data or data["stage"] != "channel":
        await query.answer("Finish both tasks first; use /resume.", show_alert=True)
        return
    channel = await get_setting("PARTNER_CHANNEL")
    if not channel:
        await query.answer("The channel is not configured.", show_alert=True)
        return
    try:
        chat_id = int(channel) if channel.startswith("-100") else channel
        member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
        is_member = member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except Exception as exc:
        LOG.warning("Could not verify membership: %s", exc)
        await query.answer("Automatic membership verification unavailable.", show_alert=True)
        await safe_send(bot, uid,
            "The bot could not check your channel membership. "
            "You can still submit for manual organizer review.",
            reply_markup=kb([("Submit for manual channel review", "channel:manual")]))
        return
    if not is_member:
        await query.answer("Channel membership not detected yet. Join first, then try again.", show_alert=True)
        return
    await query.answer("Membership verified")
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await finalize_entry(bot, uid, True)


@router.callback_query(F.data == "channel:manual")
async def channel_manual(query: CallbackQuery, bot: Bot) -> None:
    await query.answer("Saved for manual review")
    await finalize_entry(bot, query.from_user.id, False)


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    await message.answer(
        "The bot stores your Telegram ID, network selection, both redacted proof images, "
        "reward preference, phone and channel-review status. Proofs are forwarded to admins. "
        "Non-pending entries are deleted after 30 days. "
        "Copies already sent to admins require separate deletion. "
        "Use /delete_my_data to erase your stored entry and cancel reminders. "
        "Deleting your entry does not cancel any sponsor subscription. "
        "Never upload PINs, login OTPs or authentication codes."
    )


@router.message(Command("delete_my_data"))
async def delete_my_data(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    assert pool is not None
    uid = message.from_user.id
    async with pool.acquire() as db:
        await db.execute("DELETE FROM submissions WHERE user_id=$1", uid)
        await db.execute("DELETE FROM campaign_progress WHERE user_id=$1", uid)
        await db.execute("DELETE FROM reminder_subscribers WHERE user_id=$1", uid)
    await state.clear()
    await message.answer("Your bot database entry and future reminders were deleted. "
                         "Admin chat copies must be removed separately; mobile subscriptions are unaffected.")


@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin access only", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in ("url", "billing") and len(parts) == 4:
        network, taskstr = parts[2:]
        if network == "TERMS" and action == "url":
            key = "URL_TERMS"
        elif network in NETWORKS and taskstr in ("1", "2"):
            key = task_key(network, int(taskstr), "URL" if action == "url" else "BILLING")
        else:
            return
        await state.set_state(AdminState.url if action == "url" else AdminState.billing)
        await state.update_data(edit_key=key)
        if action == "url":
            await bot.send_message(query.from_user.id, f"Send the public HTTPS URL for {key}. /admin to cancel.")
        else:
            await bot.send_message(query.from_user.id,
                f"Send the ACTUAL charge, renewal frequency and cancellation instructions for {key}. "
                "Do not claim that a paid subscription is free. /admin to cancel.")
    elif action == "guide" and len(parts) == 3 and parts[2] in ("SUBSCRIBE", "SUCCESS", "SMS", "REMINDER"):
        await state.set_state(AdminState.guide)
        await state.update_data(guide_key=f"GUIDE_{parts[2]}_FILE_ID")
        await bot.send_message(query.from_user.id,
            "Upload the correct example image. It must match the sponsor. "
            "Reminder images must not imply a payment was made unless genuine and authorized.")
    elif action == "clear" and len(parts) == 3 and parts[2] == "REMINDER":
        await set_setting("GUIDE_REMINDER_FILE_ID", "")
        await bot.send_message(query.from_user.id, "Reminder image cleared. Text-only reminders are enabled.",
                               reply_markup=admin_kb())
    elif action == "channel":
        await state.set_state(AdminState.channel)
        await bot.send_message(query.from_user.id,
            "Send the partner channel @username (public), or numeric -100... channel ID (private). "
            "Add this bot as a channel administrator so it can verify membership.")
    elif action == "channelurl":
        await state.set_state(AdminState.channel_url)
        await bot.send_message(query.from_user.id,
            "Send the HTTPS channel invite URL (e.g. https://t.me/yourchannel).")
    elif action == "reminder":
        await state.set_state(AdminState.reminder)
        await bot.send_message(query.from_user.id,
            "Send a factual reminder message (10–500 characters). Do not promise prizes or free offers when subscriptions can charge airtime.")
    elif action == "stats":
        assert pool is not None
        async with pool.acquire() as db:
            counts = await db.fetch("SELECT status,COUNT(*) AS n FROM submissions GROUP BY status")
            stages = await db.fetch("SELECT stage,COUNT(*) AS n FROM campaign_progress GROUP BY stage")
        await bot.send_message(query.from_user.id,
            "Entry statuses:\n" + ("\n".join(f"{r['status']}: {r['n']}" for r in counts) or "None") +
            "\n\nUser progress:\n" + ("\n".join(f"{r['stage']}: {r['n']}" for r in stages) or "None"))
    elif action == "pending":
        assert pool is not None
        async with pool.acquire() as db:
            entries = await db.fetch("""
                SELECT id,user_id,network,channel_verified FROM submissions
                WHERE status='pending' ORDER BY created_at DESC LIMIT 15
            """)
        if not entries:
            await bot.send_message(query.from_user.id, "No pending entries.")
        for r in entries:
            await bot.send_message(query.from_user.id,
                f"Entry #{r['id']} • {r['network']} • User {r['user_id']}\n"
                f"Channel verified: {r['channel_verified']}",
                reply_markup=kb(
                    [("View both receipts", f"review:proofs:{r['id']}")],
                    [("Approve", f"review:approve:{r['id']}"), ("Decline", f"review:decline:{r['id']}")],
                ))


@router.message(StateFilter(AdminState.url), F.text)
async def admin_new_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not valid_public_https_url(value):
        await message.answer("Enter a valid public https:// URL.")
        return
    key = (await state.get_data())["edit_key"]
    await set_setting(key, value)
    await state.clear()
    await message.answer(f"Saved {key}.", reply_markup=admin_kb())


@router.message(StateFilter(AdminState.billing), F.text)
async def admin_new_billing(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not (15 <= len(value) <= 240):
        await message.answer("Use 15–240 characters describing charge, renewal and cancellation.")
        return
    key = (await state.get_data())["edit_key"]
    await set_setting(key, value)
    await state.clear()
    await message.answer(f"Saved {key}.", reply_markup=admin_kb())


@router.message(StateFilter(AdminState.guide), F.photo)
async def admin_new_guide(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    key = (await state.get_data())["guide_key"]
    await set_setting(key, message.photo[-1].file_id)
    await state.clear()
    await message.answer(f"Saved {key}.", reply_markup=admin_kb())


@router.message(StateFilter(AdminState.channel), F.text)
async def admin_new_channel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    val = (message.text or "").strip()
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}|-100[0-9]{8,18}", val):
        await message.answer("Use @channelusername (5–32 chars) or the full numeric -100... channel ID.")
        return
    await set_setting("PARTNER_CHANNEL", val)
    await state.clear()
    await message.answer("Partner channel set. Also set its HTTPS join URL.", reply_markup=admin_kb())


@router.message(StateFilter(AdminState.channel_url), F.text)
async def admin_new_channel_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    val = (message.text or "").strip()
    if not valid_public_https_url(val) or urlparse(val).hostname not in ("t.me", "telegram.me"):
        await message.answer("Use an official Telegram HTTPS join URL, e.g. https://t.me/yourchannel.")
        return
    await set_setting("PARTNER_JOIN_URL", val)
    await state.clear()
    await message.answer("Join URL saved.", reply_markup=admin_kb())


@router.message(StateFilter(AdminState.reminder), F.text)
async def admin_new_reminder(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    val = (message.text or "").strip()
    if not (10 <= len(val) <= 500):
        await message.answer("Provide 10–500 characters.")
        return
    await set_setting("REMINDER_TEXT", val)
    await state.clear()
    await message.answer("Saved follow-up reminder text.", reply_markup=admin_kb())


@router.callback_query(F.data.startswith("review:"))
async def review(query: CallbackQuery, bot: Bot) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin only", show_alert=True)
        return
    p = (query.data or "").split(":")
    if len(p) != 3 or p[1] not in ("approve", "decline", "proofs") or not p[2].isdigit():
        await query.answer("Invalid action")
        return
    entry_id = int(p[2])
    assert pool is not None
    if p[1] == "proofs":
        async with pool.acquire() as db:
            row = await db.fetchrow("SELECT proof_file_id,extra_file_id FROM submissions WHERE id=$1", entry_id)
        if not row:
            await query.answer("Entry not found")
            return
        await query.answer("Sending both proofs")
        await bot.send_photo(query.from_user.id, row["proof_file_id"], caption=f"Entry #{entry_id} Task 1")
        if row["extra_file_id"]:
            await bot.send_photo(query.from_user.id, row["extra_file_id"], caption=f"Entry #{entry_id} Task 2")
        return
    new_status = "approved" if p[1] == "approve" else "declined"
    async with pool.acquire() as db:
        target = await db.fetchval("""
            UPDATE submissions SET status=$1, updated_at=NOW(), reminder_at=NULL
            WHERE id=$2 AND status='pending' RETURNING user_id
        """, new_status, entry_id)
    if target is None:
        await query.answer("Already processed or deleted")
        return
    await query.answer(f"Entry {new_status}")
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await safe_send(bot, target,
        f"Entry #{entry_id} review status: {new_status}. "
        "This is an organizer's entry review, not confirmation of a cash payment or prize. "
        "Check official terms and the announcement channel for winners.")


async def unlock_worker(bot: Bot) -> None:
    assert pool is not None
    while True:
        try:
            async with pool.acquire() as db:
                rows = await db.fetch("""
                UPDATE campaign_progress SET stage='task2', unlock_at=NULL, updated_at=NOW()
                WHERE user_id IN (
                    SELECT user_id FROM campaign_progress
                    WHERE stage='unlock2' AND unlock_at<=NOW()
                    ORDER BY unlock_at LIMIT 100 FOR UPDATE SKIP LOCKED
                ) RETURNING user_id,network
                """)
            for row in rows:
                await safe_send(bot, row["user_id"],
                    "🔓 TASK 2 UNLOCKED\n\n"
                    "Your Task 1 proof is recorded but has not been verified yet. "
                    "Task 2 is an independent sponsor offer and may have its own subscription charge.")
                await show_task(bot, row["user_id"], row["network"], 2)
        except Exception:
            LOG.exception("Task unlock worker failure")
        await asyncio.sleep(2)


async def reminder_worker(bot: Bot) -> None:
    assert pool is not None
    while True:
        try:
            async with pool.acquire() as db:
                rows = await db.fetch("""
                    UPDATE reminder_subscribers SET reminder_sent=TRUE,updated_at=NOW()
                    WHERE user_id IN (
                        SELECT user_id FROM reminder_subscribers
                        WHERE reminder_sent=FALSE AND remind_at<=NOW()
                        ORDER BY remind_at LIMIT 100 FOR UPDATE SKIP LOCKED
                    ) RETURNING user_id
                """)
                await db.execute("""
                    DELETE FROM submissions WHERE created_at < NOW() - INTERVAL '30 days'
                    AND status!='pending'
                """)
                await db.execute("""
                    DELETE FROM campaign_progress WHERE updated_at < NOW() - INTERVAL '30 days'
                """)
                await db.execute("""
                    DELETE FROM reminder_subscribers WHERE updated_at < NOW() - INTERVAL '30 days'
                """)
            reminder = await get_setting("REMINDER_TEXT") or DEFAULT_REMINDER
            reminder_img = await get_setting("GUIDE_REMINDER_FILE_ID")
            for row in rows:
                if reminder_img:
                    try:
                        await bot.send_photo(
                            row["user_id"], reminder_img,
                            caption=reminder + "\n\nPromotional illustration; not proof of any payout.",
                            reply_markup=network_kb(),
                        )
                        continue
                    except Exception as exc:
                        LOG.warning("Reminder image failed: %s", exc)
                await safe_send(bot, row["user_id"], reminder, reply_markup=network_kb())
        except Exception:
            LOG.exception("Reminder worker failure")
        await asyncio.sleep(30)


async def main() -> None:
    global pool
    if not BOT_TOKEN or not DATABASE_URL or not ADMIN_IDS:
        raise SystemExit("Configure BOT_TOKEN, DATABASE_URL and ADMIN_IDS in Railway variables")
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    LOG.info("BUILD VERSION: Network Rewards Bot V4.2 — ADMIN ROUTE FIX ACTIVE")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    workers = [asyncio.create_task(unlock_worker(bot)), asyncio.create_task(reminder_worker(bot))]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await bot.session.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
