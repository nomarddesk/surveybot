# Network Rewards — Railway Telegram Bot (v2)

Python 3 / aiogram 3 / PostgreSQL. The bot supports MTN, Glo and Airtel with different HTTPS sponsor links and a three-part walkthrough. The promotion must have real terms and accurate subscription-charge disclosures. **It is not a payment bot and does not award prizes automatically.**

## Updated participant flow

1. `/start` shows **MTN**, **Glo**, **Airtel** buttons.
2. A selection removes the network buttons and immediately displays the chosen sponsor **URL button**, the charge/auto-renewal/cancellation explanation and a link to the official terms. There is no additional consent-button screen.
3. An optional **subscription page** example photo appears for 3 seconds, followed by an optional **successful-registration page** example photo for 3 seconds. Each photo then disappears. If an image isn't configured, an instructional text message is shown instead.
4. A large **OPEN MTN/GLO/AIRTEL OFFER** button appears with a 30-second visual countdown (updated every 5 seconds). **It is not a registration deadline**: the button still works after the countdown ends.
5. **I've completed registration** shows a brief example of the carrier's successful subscription confirmation SMS, if configured. The bot requests **one screenshot of a successful subscription notification**, not a login code or security OTP. The user must hide private details.
6. User selects cash or airtime *preference*, supplies a Nigerian phone number, and the entry is sent to admins for review. This does not guarantee winning any prize.
7. Admin receives entry and proof with **Approve / Decline** controls. The bot tells the user that the team aims to review the submission within 24 hours. User may opt into a single 2-hour **review-status** reminder, not a prompt to purchase another subscription.

## Railway setup or upgrade

1. Replace the bot's existing `bot.py` with the **v2** file from this archive (or deploy the entire folder).
2. Keep the existing Railway PostgreSQL service and variables as-is:
   - `BOT_TOKEN`: Telegram BotFather token.
   - `ADMIN_IDS`: numeric Telegram user ID(s), comma-separated.
   - `DATABASE_URL`: Postgres connection (e.g. Railway reference `${{Postgres.DATABASE_URL}}` if the service is named Postgres).
3. Start command: `python bot.py`. Deploy **one replica** for polling.
4. Open `/admin`. Your existing database records, network links and terms link remain intact.
5. **NEW, REQUIRED:** Set **MTN billing details**, **Glo billing details**, and **Airtel billing details**, with each offer's true price, renewal behavior and cancellation instructions. The user flow will not open until that network has its sponsor URL, official terms URL and billing details saved.
6. Optionally configure the three new image slots through `/admin`:
   - **Set subscription step image**: example sponsor signup/confirmation page.
   - **Set success step image**: example of the successful-registration webpage.
   - **Set SMS proof example**: example carrier SMS screenshot showing a completed subscription (no security codes, personal numbers, or login OTPs).
7. Test the complete flow using a separate Telegram account and read Railway deployment logs.

For convenience, the three supplied reference images are included in the `reference_images/` folder of this ZIP. They are not published until uploaded through `/admin`.

**Important about the supplied example screenshots:** One refers to *Winna Play Daily 200*, while a different page shows *Tapestry Flash* at **₦50/day with auto-renewal**. These may not be the same offer. For user-facing guides, upload screenshots that genuinely match the active network sponsor link and the charge you've configured. The bot deliberately does **not** auto-display the supplied images without your verification.

## Commands

- `/start` — Select network and begin an entry.
- `/admin` — Network URLs, official terms URL, each network's billing details, step screenshots, review queue and stats.
- `/privacy` — Data handling description.
- `/delete_my_data` — Remove user's database submissions and reminders.

## Security and operations

- Never ask for or store security OTPs, PINs, passwords or full authentication messages. Photos go to admins, so users should redact sensitive details before uploading.
- Guide photo IDs are stored in PostgreSQL using Telegram's file IDs. Existing `GUIDE_FILE_ID` (from v1) is used as a fallback for the first guide image unless that image is cleared.
- Completed entries and settings are saved in PostgreSQL; in-progress conversations use memory FSM and can reset after deployments.
- The bot does not verify sponsor transactions through an external API. Admin approval is a manual review status, not confirmation of a payout.
- Non-pending database entries are purged after 30 days; saved copies of messages in admin Telegram chats must be cleared separately.
- Automatic recurring-subscription reminders or hidden recurring charges are not part of the bot.
