# Network Rewards Bot V4.2 — admin command diagnostics

This patch preserves your PostgreSQL database, stored sponsor URLs, images, and participation progress.

## Why this patch exists
The V4 generic user text handler matched `/admin` before the admin handler. The V4.1 file corrected the filter, but if Telegram still replies “Send /start to begin or /resume...”, the live Railway service may still be running V4 or an outdated GitHub commit. V4.2 moves `/admin` and `/myid` above the participant text handler and introduces `/version`.

## Deploy to Railway
1. Open your *bot's* GitHub repository (not the PostgreSQL service). Replace the root `bot.py` with the `bot.py` in this ZIP. Do **not** upload the ZIP itself as a file called `bot.py`.
2. Commit to the **same branch** Railway deploys. In Railway > bot service > Settings, check the linked repository, branch and root directory.
3. Deploy the latest commit. In bot service > Deployments > latest deployment, wait for **Active/Success**; ensure the start command uses `python bot.py` and that there is one running bot service per token.
4. Open Railway logs and search for `BUILD VERSION: Network Rewards Bot V4.2`.
5. Send `/version` to your Telegram bot. The exact response must be `Network Rewards Bot V4.2 — ADMIN ROUTE FIX ACTIVE`.
6. Send `/myid` to find your numeric Telegram account ID. Enter it in the bot service's `ADMIN_IDS` variable as a number; separate multiple IDs using commas.
7. Send `/admin`. The panel should display.

## Interpreting the test
- `/version` replies with the old start/resume message: your deployed bot is still running an older codebase or a separate service is consuming this token. Check the Railway deployed commit, root directory, bot start command, and other services using the token.
- `/version` works, but `/admin` says `Admin access only`: update `ADMIN_IDS` on the **bot service**, not PostgreSQL, then redeploy.
- `/version` works but `/admin` has no response: inspect bot-service logs immediately after sending `/admin` for a traceback.

The sponsor's real subscription charges and prize rules must remain visible to users. Do not remove them in the name of simpler UX.
