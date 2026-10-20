# Finland News Bot — MVP 0.1

Telegram-only admin interface + automated RSS intake + AI Ukrainian summaries + channel publishing.

## What works
- Private admin authorization by Telegram numeric user ID
- Add, enable, disable, delete RSS sources from Telegram
- Built-in Yle starter feeds
- Fetch every 30 minutes using Cloud Scheduler
- Deduplication by source + RSS item ID
- Gemini API Ukrainian summaries (JSON output)
- Queue, approve, reject, edit text
- Autopublish toggle; sensitive articles and incomplete events stay in moderation
- Firestore persistence and Telegram publishing
- Secrets in Google Secret Manager

## Important limitations of MVP
- This is **not yet** a complete event aggregator or SEO website.
- RSS feeds may omit address, phone, ticket price or event date. Event items flagged as missing details are held for review. No invented data.
- /stats counts up to 500 stored items, not channel views/subscriber growth.
- Editing changes body text only.
- AI fallback providers, media rights checking, automatic map/ticket extraction, source health checks, per-category toggles, digest scheduling, and website SEO are phase 2.
- New custom feed is disabled by default until admin explicitly enables it. Only add sources you trust. RSS endpoints can still pose SSRF risks; production should implement domain allowlisting and DNS/IP checks before enabling arbitrary feeds.
- Manual /collect or inline Collect can take time; Cloud Scheduler is preferred for regular runs.
- A `needs_reconciliation` item may have reached Telegram; verify before retrying to prevent duplicates.
- Cloud Run is configured with a public endpoint for Telegram webhooks; webhook is protected by Telegram's secret token. Scheduler endpoint uses a separate header secret; protect job access.
- This repository contains no real credentials.

## Preparation
1. Create bot with Telegram @BotFather and copy token.
2. Create public Telegram channel; add bot as administrator with permission to post messages.
3. Find the administrator's numeric Telegram user ID, or leave `ADMIN_ID=0` to assign one later. With `0`, all Telegram admin commands are disabled.
4. Create a Google Cloud project with billing enabled and gcloud CLI access.
5. Create Gemini API key in Google AI Studio and check free-tier limits and data terms. AI key required for AI drafting; without it collected items remain `awaiting_ai`.
6. Deploy from Cloud Shell. The launcher reads credentials without displaying them and stores them in Secret Manager.

## Deploy (Cloud Shell)
For this channel, the launcher defaults to project `winged-ratio-377512`, Cloud Run region `europe-north1`, Scheduler location `europe-west1`, channel `@suominewsua`, model `gemini-3.5-flash-lite`, and `ADMIN_ID=0`. Environment variables can override these values. Scheduler does not support `europe-north1`, so its location is configured separately through `SCHEDULER_LOCATION`.

```bash
cd "$HOME/projects/bot"
git pull --ff-only
bash scripts/launch-cloudshell.sh
```

It prompts for the Telegram bot token and Gemini API key. To finish or repair only the scheduled job after Cloud Run is already deployed, run `PROJECT_ID=winged-ratio-377512 bash scripts/configure-scheduler.sh`. This reads the existing `cron-secret` from Secret Manager and creates or updates the job without rebuilding the service or rotating its secrets. See `LAUNCH-RU.md` to assign an administrator later. For a different project, configure the deployment directly:

```bash
cd finland-news-bot
export PROJECT_ID='your-project-id'
export ADMIN_ID='your-numeric-telegram-id'
export CHANNEL_ID='@your_channel'
read -rsp 'Telegram bot token: ' BOT_TOKEN; echo; export BOT_TOKEN
read -rsp 'Gemini API key: ' GEMINI_API_KEY; echo; export GEMINI_API_KEY
export WEBHOOK_SECRET="$(openssl rand -hex 24)"
export CRON_SECRET="$(openssl rand -hex 24)"
bash scripts/deploy.sh
unset BOT_TOKEN GEMINI_API_KEY WEBHOOK_SECRET CRON_SECRET
```
Cloud Shell environment variables and shell history should be treated carefully. `read -s` avoids printing the token, but secret creation and scheduler configurations should be restricted to authorized operators.

## Telegram commands
- `/start` — admin dashboard
- `/sources` — source list
- `/addsource Name | https://example.org/rss | news` — add source disabled
- `/enable ID`, `/disable ID`, `/deletesource ID`
- `/queue` — items awaiting approval
- `/auto_on`, `/auto_off`
- `/collect` — fetch immediately
- `/stats`, `/help`

## Operational notes
- Initial default: auto-publishing OFF. Enable after validating translations and sources.
- To avoid paid usage, configure budget alerts and quotas at Google Cloud and Gemini provider. Alerts are not hard spend caps; services can still incur costs.
- Verify that a selected Gemini model supports your API key, free tier, and structured output; change `GEMINI_MODEL` if necessary.
- News and events must be summarized in original words with attribution. Respect source licenses and rate limits.
- For high volume, move collection and processing into background Cloud Tasks/Cloud Run Jobs to avoid request timeouts and add retries.
