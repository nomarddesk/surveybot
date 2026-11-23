# Network Sponsor Promotion Bot

Consent-first Telegram bot (Python, aiogram 3, PostgreSQL) for **MTN, Glo and Airtel** sponsor offers. Works as a **single Railway background worker** using long polling.

## What it does

- `/start`: user selects MTN, Glo, or Airtel.
- Displays **cost/recurring-billing disclosure** and a mandatory official-terms URL before offering the sponsor link.
- Removes the selected network buttons, shows the network-specific sponsor URL after consent.
- Displays admin-provided instruction photo (automatically hides after **3 seconds**).
- Displays a **30-second visual walkthrough timer** updated every 5 seconds. It is explicitly **not an offer deadline**.
- `I've completed registration` requests a **redacted registration receipt** image.
- Two seconds after the first receipt, optionally requests a second **redacted billing/registration receipt**. It never asks for SMS login codes, OTPs, PINs, or passwords.
- Lets the user express **cash or airtime** preference; asks for their Nigerian phone number and sends the entry and photos to the admin.
- Admins can approve/decline pending entries with buttons. Approval **does not** automatically send a prize or money.
- User may opt into **one status reminder in 2 hours**, not repeated pressure to incur subscription charges.
- `/privacy` shows the data policy; `/delete_my_data` deletes the bot's database records and reminders. Admins' Telegram copies require separate deletion. All entries are purged after 30 days.

## Setup

1. Create a bot using [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a project in [Railway](https://railway.com/) and attach a **PostgreSQL** service.
3. Upload these project files to a GitHub repository and deploy the repository as a Railway service.
4. Set your Railway service variables:
   - `BOT_TOKEN`: BotFather token.
   - `ADMIN_IDS`: comma-separated **numeric** user IDs of your admin(s).
   - `DATABASE_URL`: use Railway Postgres's `${{Postgres.DATABASE_URL}}` variable reference, adjusting the service name if different.
5. Set Railway start command to `python bot.py`. Deploy **one replica**. No public URL or port is needed for long polling.
6. Open your bot, send `/admin`, edit **MTN**, **Glo**, **Airtel** sponsor URLs and the **official terms URL**. Only valid public HTTPS URLs are accepted. Add the instruction image in the same panel.
7. Test the complete flow with a second Telegram account before promoting the bot.

**Important:** The user cannot proceed without a configured network URL and sponsor terms URL. Verify the published terms support any stated prize amounts, and clearly disclose subscription cost, recurring charges, cancellation rights, eligibility and real prize conditions. The code intentionally does **not** collect SMS verification codes or create repeating paid-subscription reminders.

## Commands

- `/start` — Choose a mobile network and start a new entry.
- `/admin` — Admin-only button panel.
- `/privacy` — Data handling summary.
- `/delete_my_data` — Delete submissions and stop reminders in the database.

## Security/operations

- Admin IDs are checked on every admin action; never paste the bot token in chats or screenshots.
- Only HTTPS links to public hosts can be entered through the panel.
- The bot uses in-memory progress state, so an in-progress form may reset after a redeploy. Completed submissions and settings survive in Postgres.
- The database stores Telegram file IDs (not downloaded image files), and passes those images to admins via Telegram. Ask users to redact identifying details.
- **Admin photo messages persist in Telegram**; review your retention practices and remove copies as required.
- This is a foundation, not a payment processor, prize-selection system, identity verification system, or anti-fraud guarantee.
