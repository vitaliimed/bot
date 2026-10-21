# Finland News Bot — editor 0.2

Telegram-only admin interface + automated RSS intake + AI Ukrainian summaries + channel publishing.

## What works
- Private admin authorization by an explicit Telegram ID or current channel posting rights
- Add, enable, disable, delete RSS sources from Telegram
- Built-in Yle starter feeds
- Fetch every 30 minutes using Cloud Scheduler
- Deduplication by source + RSS item ID
- Gemini, Groq and OpenAI text writers with ordered fallback and optional AI review
- RSS/article images, OpenAI or Gemini generated illustration fallback, private previews
- Validated publication settings: topics, keywords, days/hours, timezone, intervals, quotas and formatting
- Queue, approve, reject, edit text
- Autopublish toggle; sensitive articles, incomplete events and failed AI reviews stay in moderation
- Atomic publication and generation-attempt quotas; pending-queue draining and bounded retries
- Firestore persistence and Telegram publishing
- Secrets in Google Secret Manager

## Important limitations of MVP
- This is **not yet** a complete event aggregator or SEO website.
- RSS feeds may omit address, phone, ticket price or event date. Event items flagged as missing details are held for review. No invented data.
- /stats counts up to 500 stored items, not channel views/subscriber growth.
- Editing changes body text only.
- Automatic media rights verification, map/ticket extraction, digest generation and website SEO are not implemented. Only reuse images with the necessary permission; source attribution alone does not grant a license. Disable source images per feed with `/sourceimages ID off`.
- New custom feeds are disabled until an administrator enables them. Downloads use public HTTPS IP pinning with hostname TLS validation, redirect revalidation, streamed size limits and bounded image decoding. Only enable editorially trusted feeds.
- Manual /collect can take up to four minutes. A Firestore lease prevents overlapping collectors across instances. Each run processes a bounded batch; Cloud Scheduler remains the publication clock (30-minute granularity).
- A `needs_reconciliation` item may have reached Telegram; verify before retrying to prevent duplicates.
- Cloud Run is configured with a public endpoint for Telegram webhooks; webhook is protected by Telegram's secret token. Scheduler endpoint uses a separate header secret; protect job access.
- This repository contains no real credentials.

## Preparation
1. Create bot with Telegram @BotFather and copy token.
2. Create public Telegram channel; add bot as administrator with permission to post messages.
3. With `ADMIN_AUTH_MODE=channel_admins`, the channel owner and human channel administrators who can post messages can manage the bot. Telegram provides their IDs automatically; no manual ID entry is needed. With `ADMIN_AUTH_MODE=id`, only the configured positive `ADMIN_ID` is accepted; `0` closes access in that mode.
4. Create a Google Cloud project with billing enabled and gcloud CLI access.
5. Create Gemini API key in Google AI Studio and check free-tier limits and data terms. At least one supported provider key is required for drafting; otherwise items remain `awaiting_ai`. Image generation uses separately billed provider APIs; a ChatGPT subscription does not pay for API-key requests.
6. Deploy from Cloud Shell. The launcher reads credentials without displaying them and stores them in Secret Manager.

## Deploy (Cloud Shell)
For this channel, the launcher defaults to project `winged-ratio-377512`, Cloud Run region `europe-north1`, Scheduler location `europe-west1`, channel `@suominewsua`, model `gemini-3.5-flash-lite`, `ADMIN_AUTH_MODE=channel_admins`, and `ADMIN_ID=0`. Environment variables can override these values. Scheduler does not support `europe-north1`, so its location is configured separately through `SCHEDULER_LOCATION`.

```bash
cd "$HOME/projects/bot"
git pull --ff-only
bash scripts/launch-cloudshell.sh
```

It prompts for the Telegram bot token and Gemini API key. For an already deployed service, `bash scripts/enable-channel-admin-access.sh` deploys the new access check and finishes Scheduler setup using the stored secrets; no token entry is needed. To finish or repair only the scheduled job, run `PROJECT_ID=winged-ratio-377512 bash scripts/configure-scheduler.sh`. See `LAUNCH-RU.md` for access options. For a different project with an explicit administrator ID, configure the deployment directly:

```bash
cd finland-news-bot
export PROJECT_ID='your-project-id'
export ADMIN_ID='your-numeric-telegram-id'
export ADMIN_AUTH_MODE='id'
export CHANNEL_ID='@your_channel'
read -rsp 'Telegram bot token: ' BOT_TOKEN; echo; export BOT_TOKEN
read -rsp 'Gemini API key: ' GEMINI_API_KEY; echo; export GEMINI_API_KEY
export WEBHOOK_SECRET="$(openssl rand -hex 24)"
export CRON_SECRET="$(openssl rand -hex 24)"
bash scripts/deploy.sh
unset BOT_TOKEN GEMINI_API_KEY WEBHOOK_SECRET CRON_SECRET
```
Cloud Shell environment variables and shell history should be treated carefully. `read -s` avoids printing the token, but secret creation and scheduler configurations should be restricted to authorized operators.

## Existing-service editor upgrade
```bash
cd "$HOME/projects/bot"
git pull --ff-only
bash scripts/upgrade-editor.sh
```
Open `/settings`. Optional provider setup: `bash scripts/connect-ai.sh openai` or `bash scripts/connect-ai.sh groq`; keys are read silently and stored in Secret Manager. See [SETTINGS-RU.md](SETTINGS-RU.md) for settings, image modes and cost controls. The upgrade preserves existing secrets and access configuration.

## Telegram commands
- `/start` — admin dashboard
- `/sources` — source list
- `/addsource Name | https://example.org/rss | news` — add source disabled
- `/enable ID`, `/disable ID`, `/deletesource ID`
- `/queue` — items awaiting approval
- `/auto_on`, `/auto_off`
- `/collect` — fetch immediately
- `/stats`, `/help`, `/failed`
- `/settings`, `/set KEY value`, `/reset KEY`
- `/sourceimages ID on|off`, `/retry ID`

## Operational notes
- In channel access mode, Telegram membership is checked on every command and callback. API failures deny access; revoked posting rights take effect on the next request. Each administrator has a separate editing state.
- Telegram and Gemini HTTP clients reuse connections for the lifetime of the service instance. Firestore calls and RSS parsing run outside the event loop, so they do not block other commands. Help and statistics buttons check permissions once; command handlers skip unnecessary editing-state reads.
- Button presses are acknowledged before the access check, while all management actions still require authorization. Expired callbacks are ignored; HTTP 429 responses receive one bounded retry using Telegram's retry delay. Request durations are recorded as `Telegram update ... processed in ...s`.
- Initial default: auto-publishing OFF. Enable after validating translations and sources.
- To avoid paid usage, configure budget alerts and quotas at Google Cloud and Gemini provider. Alerts are not hard spend caps; services can still incur costs.
- Verify that a selected Gemini model supports your API key, free tier, and structured output; change `GEMINI_MODEL` if necessary.
- News and events must be summarized in original words with attribution. Respect source licenses and rate limits.
- For high volume, move collection and processing into Cloud Tasks/Cloud Run Jobs. The current collector has a four-minute budget, at most three automatic preparation attempts per item, and a configurable batch size. Quotas reserve attempts before external calls, so failed/uncertain requests also count.
- Images are normalized to JPEG <=450 KB and stored temporarily in the item document. After successful channel publication, the bytes are removed and a Telegram file ID retained. Unpublished images continue to consume Firestore storage; apply a retention policy if volume grows.
- AI review checks the draft against the supplied RSS facts; it is not independent verification of real-world events.
- Photo captions are bounded to 1024 UTF-16 code units. Long source URLs use a source button. Extra body text is shortened to keep one atomic Telegram post.

## Access checks
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```
