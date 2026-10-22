# Finland News Bot — two-channel editor

Telegram administration, RSS and Linked Events intake, AI drafts, source images or generated illustrations, and separate publication to news/events channels. Runs on Google Cloud Run with Firestore, Secret Manager and Cloud Scheduler.

**Cloud Shell installation, OpenAI keys, all provider connections, settings and operating instructions: [START-HERE-RU.md](START-HERE-RU.md).**

```bash
cd "$HOME/projects/bot"
git pull --ff-only
bash scripts/diagnose.sh
bash scripts/upgrade-all.sh
bash scripts/connect-ai.sh all
```

The diagnostic is read-only. The upgrade preserves existing secrets/settings, enables channel-admin authentication, adds missing catalog sources, migrates legacy item identities and configures separate Scheduler jobs. It prints a previous-revision rollback command. Events autopublishing starts disabled.

- News → `CHANNEL_ID`; events → `EVENTS_CHANNEL_ID`. Mandatory channel hashtag in every post.
- Ukrainian/Finnish event posts, safe Telegram HTML, visible-character caption limits.
- Independent schedules, quotas, filters, queues, image budgets and models per channel.
- Six text providers: Gemini, Groq, OpenAI, DeepSeek, Mistral, OpenRouter. Ordered fallback, bounded requests and per-instance cooldowns.
- Images: source/API/RSS/article, then OpenAI/Gemini. Configurable generation-attempt quota; generated artwork labeled as AI.
- Official events rechecked before publication. Missing, cancelled, past or changed event details cannot silently autopublish.
- Cross-feed canonical URL deduplication, transactional publish claims, no automatic retry of ambiguous Telegram sends.
- Sectioned settings, paginated sources/queues, `/collect` through Scheduler, `/ask` for editorial assistance.

Catalog and coverage limitations: [SOURCES-RU.md](SOURCES-RU.md). Exact settings: [SETTINGS-RU.md](SETTINGS-RU.md). Legacy initial infrastructure setup: [LAUNCH-RU.md](LAUNCH-RU.md).

Run tests:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

No real credentials belong in this repository. API-key usage is billed by providers separately from consumer chat subscriptions. Source images require appropriate reuse permission. A configured key is not proof of access to every model. Large volumes require retention/queue scaling; this version uses bounded batches, pagination and four-minute collector leases.
