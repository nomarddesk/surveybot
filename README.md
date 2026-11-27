# Network Rewards Bot — V4 (two tasks per network)

Telegram bot for a **transparent, sponsor-supported** rewards campaign using Python 3, aiogram 3, and Railway PostgreSQL. It supports MTN/Glo/Airtel, two separately configured sponsor offers per network, two receipt uploads, a 10-second transition, admin review, and a Telegram winner-announcement channel.

**No fake verification:** the 10-second timer only unlocks Task 2. It does not confirm a paid subscription. Both receipts stay pending until real review. The bot does not automatically pay winnings or guarantee a prize. Never label a paid mobile subscription as free.

## Participant flow

1. `/start`: select MTN, Glo or Airtel. The bot explains that each sponsor offer may charge/renew separately.
2. Displays the actual charges/renewals for **both tasks**, including the promotion terms URL, before proceeding.
3. **Task 1:** sponsor URL + charge/renewal/cancellation disclosure + any configured example images + 30-second *instruction* countdown (not an offer deadline). User taps **Submit Task 1 proof** and uploads a redacted carrier subscription-confirmation screenshot, **not** an SMS login/verification code.
4. Confirmation states **“proof received, manual verification pending.”** Ten seconds later, PostgreSQL automatically unlocks **Task 2**. This is not an approval of Task 1.
5. **Task 2:** a *different, separately configured* sponsor URL and its billing disclosure, followed by a **second** proof upload.
6. User selects **cash/airtime preference**, provides a Nigerian contact number, and opens the partner Telegram channel where winners may be announced.
7. **Check my channel membership** checks actual membership via Telegram's `getChatMember`. The bot must be an admin of that channel. If Telegram verification is unavailable, the user can submit for **manual** channel review; the bot does not falsely claim membership was verified.
8. The user and admins receive an entry number. Admins see **both** proof screenshots and manually approve or decline the *entry review*. Approving does not mean money was paid.
9. One automatic follow-up reminder is scheduled about 2 hours after start, and rescheduled after completion. Its default text makes no unverified cash-award or free-subscription claims. The prior bank-notification image is **not** automatically circulated as proof of a payout.

## Deploy V4 on Railway

1. Download and extract the ZIP. **Replace your existing bot.py** in the GitHub repository with the V4 one. Keep `requirements.txt` and `Procfile`. Commit/push so Railway builds the updated code.
2. Keep the existing Railway Postgres service and these bot-service variables:
   - `BOT_TOKEN`: your @BotFather token, never share it.
   - `ADMIN_IDS`: your Telegram numeric user ID(s), comma-separated.
   - `DATABASE_URL`: reference to your project's Railway Postgres `DATABASE_URL` (e.g. `${{Postgres.DATABASE_URL}}` if the database service is named `Postgres`).
3. Start command: `python bot.py`. Keep **one Railway worker replica** for long polling.
4. The bot migrates the existing database automatically with `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE IF NOT EXISTS`. It does **not** overwrite your existing Task 1 links, billing settings, stored receipts or completed entries.
5. Send `/admin` in Telegram. Configure all required items below.

## Admin checklist (do this before testing)

| Network | Existing Task 1 link | New Task 2 link | Task 1 billing | Task 2 billing |
|---|---|---|---|---|
| MTN | `URL_MTN` | `URL_MTN_2` | `BILLING_MTN` | `BILLING_MTN_2` |
| Glo | `URL_GLO` | `URL_GLO_2` | `BILLING_GLO` | `BILLING_GLO_2` |
| Airtel | `URL_AIRTEL` | `URL_AIRTEL_2` | `BILLING_AIRTEL` | `BILLING_AIRTEL_2` |

Also set:

- **Official promotion terms** (`URL_TERMS`) with genuine, current giveaway rules (eligibility, method of selecting winners, deadlines, prizes and sponsor terms). An affiliate link is **not** a substitute for published prize rules.
- **Partner channel ID/@username** (`PARTNER_CHANNEL`), e.g. `@ExampleAnnouncements` or private channel numeric `-100...` ID.
- **Partner channel join URL** (`PARTNER_JOIN_URL`), e.g. `https://t.me/ExampleAnnouncements` or a valid Telegram invite URL.
- Add the bot as a **channel administrator**. Membership checking is only reliable for other users when the bot is a channel admin.
- Optionally set `GUIDE_SUBSCRIBE_FILE_ID`, `GUIDE_SUCCESS_FILE_ID`, `GUIDE_SMS_FILE_ID` via image buttons. Examples must match the actual sponsor and charge. These are not auto-published from the ZIP.
- Optionally change the reminder text or upload a **factual reminder picture** (truthful claims only). If a reminder image is set, the bot labels it a promotional illustration rather than evidence that anyone received a payout.

Task 2 will not open if its link or its price/renewal/cancellation details are missing. Participants see both tasks' price terms upfront.

## Test

1. On a test Telegram account, send `/start`, choose MTN and verify both prices are displayed.
2. Follow Task 1 **without sharing an OTP**, upload a **redacted dummy/test proof**, and verify the bot says **not yet verified**.
3. Confirm Task 2 unlocks automatically in ~10 seconds; upload a separate test image.
4. Choose a reward preference and contact number.
5. Join the configured channel, click **Check my channel membership**, and verify the admin receives **two** images.
6. Review the submission from `/admin`. Try `/resume`, `/restart`, `/privacy` and `/delete_my_data`.
7. In Railway logs, confirm successful polling and check for errors. In PostgreSQL, inspect `campaign_progress`, `submissions` and `reminder_subscribers`.

## Commands

- `/start` choose a network
- `/resume` continue an interrupted task after a Railway redeploy
- `/restart` discard an unfinished bot entry (does **not** cancel sponsor billing)
- `/admin` manage offers and review entries
- `/privacy` learn what data is stored
- `/delete_my_data` delete database entries and cancel reminders; already-forwarded admin chat messages require separate removal

## Privacy and accuracy

The bot stores Telegram user ID, chosen network, two screenshots, prize preference, phone number and channel-check status. Admins receive screenshots for manual review. It does not collect authentication OTPs, PINs or passwords. It does not know whether either subscription was genuinely completed without carrier/sponsor verification. Only use genuine authorized prize claims; do not use simulated bank alerts to suggest payments have already occurred.

For the partner channel check, Telegram's official API documents that `getChatMember` is guaranteed for other users only when the bot is an administrator in the channel: https://core.telegram.org/bots/api#getchatmember
