# Cloud deployment (unified daily run, M3–M6)

GitHub Actions starts a temporary Python runner, discovers feed jobs and monitors employer APIs, updates
persistent PostgreSQL, and calls Telegram. The runner then stops. Telegram does
not run JobScout itself. Your laptop does not need to remain online.

Nothing is provisioned by this repository. Complete the following steps yourself;
never paste credentials into source code, issues, or workflow files.

## 1. Publish the implementation and check CI

Review and commit the implementation, tests, documentation, and both files in
`.github/workflows/`, then push/merge them to your repository's default branch.
Keep `.env`, private `config/companies.yaml`, private `config/profile.yaml`, and
`data/` out of the commit. They are ignored; do not force-add them.

In GitHub, open **Actions → Tests** and wait for a green run. This checks lint,
formatting, the offline test suite, and PostgreSQL integration tests against
a disposable PostgreSQL 17 service. It uses no production secrets and sends no
Telegram messages. The PostgreSQL tests skip locally unless TEST_POSTGRES_URL
points to a localhost database named jobscout_test.

If Actions is disabled, enable it in repository settings. Workflows must be on
the default branch for the manual Daily search button and schedules. Daily search
also rejects manual runs from other branches.

## 2. Create a persistent PostgreSQL database

Use a dedicated existing database or manually create a project on a free-tier
provider such as [Neon](https://neon.com/docs/get-started-with-neon/connect-neon).
Choose the Free plan and a suitable region (Europe is a reasonable default).
Do not enable paid upgrades without deciding on a budget. Review the provider's
current limits; free tiers and availability can change.

In the provider's connection dialog, copy the PostgreSQL connection URI, including
username, password, host, database name, and `sslmode=require` (or stricter).
Copy the URI itself, not a `psql` command or surrounding quotes. For example:

```text
postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Use the actual provider-generated URI so special password characters are encoded
correctly. The cloud wrapper rejects SQLite, incomplete URLs, and URLs without
TLS. Installations use psycopg even when the URI starts with `postgresql://`.

**Your local SQLite data is not transferred.** A new database starts with empty
job and notification history, so previously received local matches can appear in
the first cloud digest. Retain your SQLite database. There is no history import
utility in this milestone. To share history for future local/cloud runs, use the
same cloud DATABASE_URL and Telegram chat ID locally after setup.

Himalayas can be enabled through `providers.himalayas.enabled: true` in
`JOBSCOUT_DISCOVERY_YAML`; use `mode: search` for profile-driven role queries.
See [the provider guide](job-discovery.md#himalayas-m4). It needs no additional
credential, and stays disabled when the optional configuration is absent.

Brave is a separate optional metered provider. Add the `BRAVE_SEARCH_API_KEY`
Actions secret and set `providers.brave.enabled: true` in `JOBSCOUT_DISCOVERY_YAML`
to opt in. Queries use the selected profile's role list and configured country or
preferred regions. Defaults cap attempts at 3/run, 6/UTC day and 180/UTC month,
shared through the database. Preview mode skips Brave without spending or updating
its counters. The normal scheduled send can use it after explicit configuration.
See [M5 retention and budgeting](job-discovery.md#brave-search-m5).

Remotive can be enabled without another secret:

```yaml
providers:
  arbeitnow: {enabled: true}
  himalayas: {enabled: false}
  remotive: {enabled: true, limit: 100, refresh_hours: 24}
  brave: {enabled: false}
```

Paste this into `JOBSCOUT_DISCOVERY_YAML`, or merge the Remotive block into your
existing configuration. The shared PostgreSQL cooldown prevents duplicate refreshes
across catch-up/manual runs. A preview before the first normal send has no cached
Remotive jobs. To test Remotive without sending Telegram, use local `discover-jobs`
with a separate test database as described in [M6 testing](testing-m6.md).
For feedback against cloud jobs, run the local `feedback` command with the same
cloud DATABASE_URL and the profile's `feedback_scope`; no bot token is needed.

## Discovery upgrade (schema 6)

Before using this release against an existing database, back it up and run mode
**migrate**. The database registry is now canonical. Existing companies YAML seeds
missing registrations on the first normal run; later YAML changes do not override
registered sources. Use the [discovery guide](discovery.md) to add, review, enable,
or disable sources using the same cloud DATABASE_URL from your local CLI.
`JOBSCOUT_COMPANIES_YAML` is optional even on an empty registry: daily runs now
include Arbeitnow discovery. Optional `JOBSCOUT_DISCOVERY_YAML` supplies feed and
registration budgets using `config/discovery.example.yaml`. Defaults require no
API key. Schema 6 adds telemetry, refresh schedules and feedback alongside existing
request-budget counters and durable `job_links`; the migration preserves
jobs, source registrations and delivery history. Upgrade before enabling sends.

## 3. Add repository secrets

Open **Settings → Secrets and variables → Actions → Secrets → New repository
secret**. Add each name exactly:

- `DATABASE_URL`: the complete private PostgreSQL connection URI.
- `TELEGRAM_BOT_TOKEN`: the bot token from BotFather.
- `TELEGRAM_CHAT_ID`: your numeric destination chat ID, including a minus sign
  if applicable. Start the bot in Telegram first; see the
  [Telegram setup guide](telegram-setup.md) if these are not ready.
- `JOBSCOUT_COMPANIES_YAML` (optional): employer board seeds; omit for feed-only
  discovery or database-registered boards.
- `JOBSCOUT_DISCOVERY_YAML` (optional): feed and registration settings. Omit for
  bounded defaults. For ATS-only runs, disable all providers with
  `providers.arbeitnow.enabled: false`, `providers.himalayas.enabled: false`,
  `providers.brave.enabled: false`, and `providers.remotive.enabled: false`.
- `JOBSCOUT_PROFILE_YAML`: paste the entire contents of your working private
  `config/profile.yaml`.

YAML secrets are multiline text, not file paths or base64. Preserve indentation.
Use secrets, not repository variables, for these values. When you change a
local YAML file later, update the corresponding secret too; it is a separate copy.

The workflow materializes temporary configuration files with private permissions
and removes them afterwards. It prints only safe status/count summaries, not
profiles, digest bodies, chat IDs, or raw exceptions. No private artifacts are
uploaded. This matters especially when Actions logs are publicly visible.

## 4. Initialize the cloud database

Open **Actions → Daily search → Run workflow**. Select the default branch, choose
mode **initialize**, leave **retry_uncertain** unchecked, and run it. Wait for a
green result. This creates the schema in the DATABASE_URL database.

For an existing revision-1 through revision-5 JobScout cloud database, back it up first and use mode
**migrate** instead. Initialization is safe to repeat but does not silently upgrade
old tables. Do not run migrations against unrelated databases.

## 5. Preview, then send manually

Run Daily search again with mode **preview** (the default). This fetches enabled public
feeds/boards (Brave search and Remotive refreshes are skipped; recent cached
Remotive jobs remain eligible) and reads the cloud database but sends no Telegram messages and writes
no jobs, identity links, registrations or delivery success. A successful summary includes `dry_run`. Cloud previews
intentionally omit the message body from logs.

To inspect the actual preview privately, activate your local virtual environment,
install the PostgreSQL extra, and load your cloud DATABASE_URL and Telegram chat
ID into your local environment, then run:

```sh
python -m pip install -e '.[dev,postgres]'
job-scout run --profile config/profile.yaml --dry-run
```

`.env` is not loaded automatically; follow the environment-loading instructions
in the Telegram guide. Ensure the local YAML files match the uploaded secrets.

When the preview is satisfactory, manually run mode **send**. This makes a real
Telegram delivery and records confirmed message parts in PostgreSQL. Check the
chat. Rerunning send on the same local date returns `already_sent` after completion.
Mode **status** shows counts of recorded notification-part statuses without sending.
Mode **health** shows the last 30 days of discovery request, new-record and failure
counts. It requires only DATABASE_URL, and makes no provider or Telegram requests.

### Additional manual digests on the same day

After publishing this workflow version to the default branch, select mode **send**
and check **additional** in **Run workflow** to bypass `already_sent`. Select
**preview** with the same checkbox for a read-only preview. The checkbox defaults
to off; scheduled runs cannot enable it.

This option recollects jobs with the current profile and sends only previously
undelivered matches. A no-new-matches message is sent when nothing remains.
Existing confirmed notifications stay recorded. Unfinished deliveries resume
first, and uncertain parts still require explicit `retry_uncertain` after checking
Telegram. No new secrets or database migration are needed.

## 6. Enable the daily schedule

After the manual send works, open **Settings → Secrets and variables → Actions →
Variables → New repository variable** and create:

```text
JOBSCOUT_SCHEDULE_ENABLED = true
```

Use the lowercase value `true`. Leave it absent or set it to `false` to pause
scheduled work; manual runs remain available. No recurring collection or delivery
runs while the variable is disabled (scheduled workflow jobs are skipped).

The workflow has three daily triggers in **Europe/Rome**:

- **09:00**: morning run.
- **15:00**: catch-up if the earlier run was missed or definitely failed.
- **21:00**: a second catch-up opportunity.

GitHub's timezone-aware schedule follows Rome daylight saving time. The profile's
`digest.timezone` defines the digest date; keep it consistent with the workflow.
Optional `digest.send_after: "08:00"` (the default) prevents a scheduled run from
sending before that local time. Manual runs intentionally bypass this time gate.

To change the schedule, edit the cron expressions and timezone in
`.github/workflows/daily-search.yml`, then update the profile secret's timezone
and send_after. Changing send_after alone does not create a later trigger.

[GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
are best-effort, can be delayed or dropped, and run on the default branch. Public
repositories can have schedules disabled after 60 days without repository
activity; check Actions periodically and re-enable when needed. A catch-up run
checks the same persistent outbox, so a completed digest is not resent.

## 7. Recovery and routine operation

Check failed Actions runs and your Telegram digest's coverage notice. GitHub
notifications depend on your account notification preferences.

- Exit **1**, reduced coverage: some employers failed, but available results may
  already have been delivered. A completed digest, including an all-source-failure
  notice, is final for that date. Catch-up does not recollect it; recovered sources
  contribute to the next day's digest.
- Exit **2**, configuration error: check secret names, YAML indentation, enabled
  sources, chat/token settings, and TLS URL. Validate YAML locally before replacing
  its secret.
- Exit **3**, database/runtime error: check provider availability, database URL,
  initialization or migration, and installed dependencies.
- Exit **4**, delivery needs attention: check Telegram and run mode status. Definite
  failures are retryable with a normal send. For uncertain/sending outcomes, inspect
  the chat first. Only if you accept a possible duplicate, choose manual **send**
  with **retry_uncertain** checked. Scheduled runs never force uncertain retries.
- An interrupted sender can retain its lease for five minutes. Wait before retrying;
  do not delete delivery records to bypass the lock. Workflow runs serialize and
  have a 25-minute timeout; cancellation during a send may leave an uncertain result.
- An older unfinished digest resumes first. Once it completes, run again if you
  also need today's digest. Confirmed parts remain suppressed.

For private diagnostics, use your local CLI with the same cloud DATABASE_URL and
chat ID: `job-scout delivery-status`, `job-scout source-health`, and
`job-scout discovery-health --days 30`. Do not paste
private output into a public issue. Rotate compromised credentials in their
provider and update the matching repository secret.

Retain the database to retain delivery history. Back it up before future migrations.
Keep the enabled employer list bounded so collection completes within the runner
limit. Review GitHub Actions usage and your database provider's free-tier usage;
this design requires no paid service but does not guarantee unlimited free usage.

## Deployment verification checklist

- Tests workflow is green, including PostgreSQL integration.
- Initialize/migrate succeeds against your intended cloud database.
- Preview completes and the private local preview is useful.
- Manual send arrives in Telegram; a repeat does not duplicate it.
- Schedule variable is enabled and the next morning's run completes.
- Your laptop can be switched off; GitHub and PostgreSQL run independently.

These live deployment checks require your accounts and configuration. Local unit
checks alone do not verify production database access or real Telegram delivery.
