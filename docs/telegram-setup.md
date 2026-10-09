# Telegram setup and recovery

Milestone 5 adds `run`, `telegram-chats`, `delivery-status`, and `db-migrate`.
Tests use mocked Telegram responses; they do not create bots or send messages.

## 1. Update and migrate your existing database

From the repository root:

```sh
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Use the same `DATABASE_URL` as before. If unset, the default is
`sqlite:///data/jobscout.db`. For that default SQLite file, make a timestamped
consistent backup before migration:

```sh
python - <<'PY'
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
source_path = Path("data/jobscout.db")
if not source_path.exists():
    raise SystemExit("No existing default SQLite database; use db-init instead.")
backup_path = source_path.with_name(
    "jobscout-before-v2-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
)
with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as backup:
    source.backup(backup)
print(backup_path)
PY
job-scout db-migrate
```

For a custom SQLite path, adjust the backup path. For PostgreSQL, use your
database's backup facilities. The additive migration preserves jobs and source
history and is safe to rerun. A brand-new database uses `job-scout db-init`
instead. Schema 2 is required for all database commands. This setup does not
provision cloud resources or require new database credentials.

## 2. Create your Telegram bot

Open the official [@BotFather](https://t.me/BotFather) in Telegram. Send
`/newbot`, choose a display name and an available bot username, and save the
token privately. Open your new bot's chat, press **Start**, and send a short
message such as `JobScout setup`. See Telegram's
[official setup guide](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
A dedicated personal bot avoids interference with other bot integrations.

## 3. Configure the token and find your chat ID

Create `.env` from `.env.example` if it does not already exist. Do not overwrite
an existing private file. Edit it locally, enter your bot token, and keep your
existing database URL. Leave the chat ID blank initially:

```dotenv
DATABASE_URL='sqlite:///data/jobscout.db'
TELEGRAM_BOT_TOKEN='PASTE_YOUR_BOT_TOKEN_HERE'
TELEGRAM_CHAT_ID=''
```

Keep the token out of Git and chat messages. `.env` is ignored by Git but is
not automatically loaded. Load your own file into the current shell:

```sh
set -a
source .env
set +a
job-scout telegram-chats
```

Expect output resembling `{"private_chat_ids": [123456789]}`. Copy your numeric
private chat ID into `TELEGRAM_CHAT_ID` in `.env`, then repeat the three shell
loading commands. Use the numeric ID, not your Telegram username or token.

If the list is empty, send another message to your new bot and retry. This
helper only reads incoming updates; it sends no messages and does not change
webhooks. An existing webhook prevents polling, as described in
[Telegram's getUpdates documentation](https://core.telegram.org/bots/api#getupdates).
Use a dedicated bot if another integration owns the webhook. If several private
IDs appear, confirm which is yours before selecting one; do not guess a recipient.

## 4. Choose digest preferences

Your private `config/profile.yaml` accepts:

```yaml
digest:
  timezone: Europe/Rome
  maximum_matches: 10
  minimum_score: 60
```

Keep your desired existing limit; 10 is just an example. `minimum_score` defaults
to 60 when omitted. `--limit 5` is a convenient first test without editing the
profile. `--min-score` and `--track NAME` also override the profile for a run.
Unknown eligibility is allowed and disclosed; clear ineligibility is excluded.

These settings do not schedule execution. Only freshly collected postings from
successful enabled sources qualify. The first real digest may include older
postings that have never been delivered. `match` can show stored jobs below the
digest threshold because its default minimum score remains zero.

## 5. Preview, then send

```sh
job-scout run --dry-run --limit 5
```

Review the messages, links, uncertainties, and coverage summary. Dry-run fetches
boards but writes no jobs, source health, digest plans, or delivery outcomes. It
makes no Telegram requests. It requires schema 2 but no bot token; without a
chat ID it cannot consult your chat's delivery history. If a digest is already
partially sent, preview displays only the saved remaining messages. A completed
same-day digest produces `already_sent` rather than another preview.

When ready, this command actually sends to your configured Telegram chat:

```sh
job-scout run --limit 5
job-scout delivery-status
```

Expect `sent`, with confirmed parts and Telegram message IDs in delivery status.
A long digest arrives as numbered messages. Entries and original application
URLs stay intact. Formatting is plain text, so job HTML/Markdown remains literal;
links remain clickable. Fields can be shortened, but links are never truncated.
Messages use a conservative 4,000 UTF-16-unit budget below the
[sendMessage limit](https://core.telegram.org/bots/api#sendmessage).

## 6. Understand reruns and recovery

- One completed digest per recipient and calendar date in the profile timezone.
  Same-day reruns return `already_sent`, including after a no-match or reduced
  coverage digest. No collection occurs on that path. Recovered sources wait
  until the next day; use `ingest` and `match` to inspect newer jobs meanwhile.
- Confirmed jobs stay suppressed after updates or reopening. Jobs beyond the
  daily limit can appear on later days. Identity is provider/region/board/posting;
  cross-board reposts may still appear separately. Retain the database.
- Failed digests resume saved content, even if you changed the profile. Confirmed
  parts are skipped. An older partial digest resumes first; run again afterwards
  to prepare today's digest. Old saved content may need a fresh link check.
- `failed` means a definite Telegram rejection. Correct credentials/chat access
  or wait for rate limiting, then rerun `job-scout run`. Short HTTP 429 delays get
  up to three requests within one send operation; other rejections are not
  retried immediately. Paid broadcast mode is never enabled.
- `uncertain`, or `sending` left by a crashed process, means the message may have
  arrived without a saved confirmation. Check your chat and `delivery-status`.
  Automatic retries stop. Use `job-scout run --retry-uncertain` only when you
  accept a possible duplicate of the unconfirmed part. This is also the recovery
  path if it arrived but you want to finish the saved digest; confirmed parts
  remain skipped. Exactly-once external delivery cannot be guaranteed.
- Concurrent senders are blocked by a renewable lease. A crashed sender's lease
  can take five minutes to expire. Do not delete the database to bypass it.
- Source failures appear separately in the digest. Jobs from successful sources
  still arrive. If all sources fail, the message reports unavailable results
  rather than saying no matching jobs exist.
- If a database write fails after a Telegram send, treat the part as uncertain.
  An operation records its attempt count and latest outcome; internal short
  rate-limit retries do not create separate log rows.

Exit codes: `0` success/preview/already sent; `1` some source collection failed
although the digest was previewed or sent; `2` invalid input/credentials; `3`
database error; `4` delivery failure, ambiguous outcome, or active sender.

To request another digest today, run `job-scout run --additional`; preview with
`job-scout run --additional --dry-run`. Already-delivered jobs remain suppressed,
and existing unfinished digests resume first. This does not force uncertain
retries or erase history. In GitHub Actions, use send/preview mode with the
**additional** checkbox.

## 7. Unattended scheduling

Follow the [Milestone 6 deployment guide](deployment.md) for GitHub Actions,
persistent PostgreSQL, secrets, manual runs, and Europe/Rome scheduling. Your
computer can stay off after setup. Employer and Telegram HTTP calls remain mocked
in tests; CI uses a disposable PostgreSQL service for database integration tests.
