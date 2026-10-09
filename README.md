# JobScout

A personal job discovery and explainable matching service for internship,
graduate, and entry-level opportunities worldwide.

**Status: Milestone 6 — deployment implemented.** Collect, persist, match, and
deliver jobs through Telegram, locally or through scheduled GitHub Actions.
Follow the [cloud deployment guide](docs/deployment.md) to configure your database,
secrets, and opt-in schedule; cloud services are not provisioned automatically.

## Local setup

Requires Python 3.12 or newer. From the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
job-scout --help
job-scout collect --help
```

If already installed, activate the environment and repeat the installation
command to install the collection dependencies. `python -m job_scout` is an
alternative entry point. No credentials are needed for public collection.

## Try collection

First check the CLI using the disabled example sources (no API requests):

```sh
job-scout collect --config config/companies.example.yaml
```

It prints an empty report and a warning that no sources are enabled. Then
create a private registry, if you do not already have one:

```sh
cp config/companies.example.yaml config/companies.yaml
```

Start with a small set of employers you care about. Follow the careers link
from each employer's official website and confirm that it leads to a public
Greenhouse or Lever board. Copy the board identifier from that board's URL;
do not guess identifiers from company names. Check the employer's identity
and any access restrictions before enabling collection.

Edit each entry in `config/companies.yaml`:

- `source`: `greenhouse` or `lever`.
- `company`: the employer's display name.
- `board_id`: verified board token/site name, not a full URL.
- `enabled`: set to `true` once verified (defaults to `false`).
- `region`: `global` (default), or `eu` for a board on `jobs.eu.lever.co`.
  Greenhouse supports only `global` here; this is an API region, not a job-location filter.

The checked-in identifiers are deliberately unverified, disabled placeholders.
The loader rejects enabled placeholders, duplicate boards, unsupported providers,
unknown configuration fields, and invalid board identifiers.

```sh
job-scout collect --config config/companies.yaml
```

JSON goes to stdout; collection summaries and failures go to stderr. Save a
snapshot in the ignored local data directory if useful:

```sh
mkdir -p data
job-scout collect --config config/companies.yaml > data/collection.json
```

The report contains one result per enabled employer, with configuration,
attempt/completion timestamps, normalized `jobs`, and an `error` if collection
failed. An empty successful board is distinct from a failed board. Exit codes:
`0` all enabled sources succeeded (also when none are enabled), `1` at least one
source failed, `2` invalid command/configuration. Even with exit code `1`, stdout
contains successful employers' results.

## Collection behavior and limits

- Uses the documented public [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
  with descriptions and [Lever Postings API](https://github.com/lever/postings-api)
  with explicit pagination, including EU boards. No application submissions.
- At most three employers are collected concurrently, with request starts paced
  at least 0.5 seconds apart. Each request uses a 20-second HTTPX timeout.
  Transport failures, HTTP 429, and 5xx responses get up to three attempts with
  exponential backoff and jitter. `Retry-After` is respected; waits over 60
  seconds fail the source so a later run can retry. Other HTTP errors and
  malformed JSON are not retried; redirects are not followed.
- Each adapter validates responses and produces the same `Job` model. Invalid
  postings fail that employer's batch instead of reporting incomplete data as
  a successful collection; other employers continue. Lever is limited to 100
  pages of 100 postings. Repeated posting IDs or reaching the page limit report
  incomplete collection as a failure.
- Descriptions become plain text, excluding script/style/template contents.
  Original IDs, board identifiers, posting/application links, and selected
  metadata are retained. HTTP(S) URLs are validated but never visited by the
  collector. Unknown employment type, remote policy, posting date, and
  eligibility remain unknown. No matching or eligibility inference runs yet.
- Times are timezone-aware UTC. Greenhouse update times are not treated as
  posting times. In `collect` output, first/last-seen values describe that fetch;
  `ingest` preserves their history in the database.
- Every collection fetches all published postings again. `collect` does not
  write to the database; `ingest` stores the results. Both make live requests
  for enabled boards. For a read-only notification preview, use `run --dry-run`.

For a 404, recheck the board identifier and Lever region. For a validation
failure, compare the response shape with the official API documentation. For
429/5xx/timeouts, retry later; successful sources remain in the report.

## Persistent storage

Update your installation and initialize a local database:

```sh
source .venv/bin/activate
python -m pip install -e '.[dev]'
export DATABASE_URL=sqlite:///data/jobscout.db
job-scout db-init
job-scout ingest --config config/companies.yaml
job-scout jobs
job-scout source-health
```

The SQLite URL shown is also the default when `DATABASE_URL` is unset. Relative
paths resolve from your current directory; parent directories are created as
needed. `db-init` creates the initial schema, is safe to rerun, and never clears
existing data. Use a dedicated JobScout database. Database failures return exit
code `3` with credential-safe error messages. An uninitialized database asks you
to run `db-init` before ingestion, without first fetching boards.

`ingest` prints inserted/updated/unchanged/closed counts, failed source counts,
and ignored snapshot counts. Source failures still return exit code `1`; jobs
from successful sources and failure health are saved. Each employer snapshot
is committed atomically. A database write failure rolls back that employer;
previously committed employers remain saved and the command exits `3`.

`jobs` prints stored open postings; `jobs --include-closed` includes historical
closed postings. `source-health` includes each attempted board's last attempt,
last success, latest error, and consecutive failure count. Disabled boards are
not fetched or aged out; their historical records remain unchanged.

Persistence rules:

- Identity is provider + API region + board identifier + source posting ID.
  Re-ingesting the same posting updates it without adding another row.
  Distinct openings with matching titles or application URLs remain distinct;
  cross-board repost merging is deferred until an occurrence/link model can
  preserve independent source history safely.
- `first_seen_at` is retained; `last_seen_at` advances only when the job is
  actually fetched. Description, title, links, and source metadata are updated.
  Source-supplied posting/update dates remain separate from these timestamps.
- Three successful checks that omit a posting mark it closed. Failed checks
  neither increment nor reset its missing count. Reappearance resets the
  count and reopens the same record, preserving its first-seen time.
- Replaying a report, or ingesting an older source attempt after a newer one,
  does not change jobs or health. Source transactions serialize concurrent
  writers so repeated ingestion cannot create duplicate identities.
- Schema revision 2 retains `sources`, `jobs`, and `schema_version`, and adds
  `digests`, `notification_log`, and `delivery_locks`. Job data
  stores canonical fields and selected source metadata as JSON, with indexed
  identity, freshness, and lifecycle fields in columns. No profiles or
  credentials are stored in job rows. Delivery snapshots contain rendered messages
  and hashed recipient/job identities, never bot tokens or full profiles.
  `db-migrate` upgrades revision 1 without losing jobs/source history.
  `db-init` does not silently alter existing tables.

### PostgreSQL pathway

Install the optional driver, then set `DATABASE_URL` to your existing private
connection string before running the same commands:

```sh
python -m pip install -e '.[dev,postgres]'
# Set DATABASE_URL privately to postgresql+psycopg://... with required TLS options.
job-scout db-init
job-scout ingest --config config/companies.yaml
```

Plain `postgresql://` (and `postgres://`) URLs select psycopg automatically.
No cloud resources are provisioned. Changing the URL selects a different
database; it does not transfer existing SQLite records. SQLite integration is
tested locally; GitHub Actions also runs integration tests against a disposable
PostgreSQL 17 service. Production connectivity remains a deployment check. Back up an existing database before
future migrations.

## Match stored jobs

Use your existing private profile, or copy the example if you have not created
one. Keep unknown graduation, authorization, sponsorship, and enrollment fields
as YAML `null`; do not use the string `"unknown"`.

```sh
job-scout match --profile config/profile.yaml --limit 20
job-scout match --profile config/profile.yaml --min-score 60
job-scout match --profile config/profile.yaml --include-ineligible
# The sanitized example includes an optional systems track:
job-scout match --profile config/profile.example.yaml --track systems
```

Matching reads open jobs from `DATABASE_URL`. It makes no network requests,
changes no stored jobs, stores no profile, and sends no notifications. Results
are recomputed using the current profile each time; no schema migration is
needed. Run `ingest` first to refresh jobs. Output includes last-seen timestamps,
application URLs, scores, factor values, normalized weights, 1–3 explanations,
eligibility evidence, rejection reasons, and unknowns. It also reports the
number of open and ineligible jobs before filtering. `--limit` defaults to the
profile's `digest.maximum_matches`. Invalid profiles/options return exit `2`.

Eligibility rules are intentionally narrow:

- `ineligible`: clear seniority in the title, incompatible mandatory degree,
  explicitly required enrollment contradicted by the profile, incompatible
  graduation window, or explicitly unavailable sponsorship when required.
- `unknown`: missing or unsupported eligibility evidence, including unstated
  sponsorship, authorization, enrollment, and graduation policies. Unknown
  jobs remain in recommendations. Location alone never establishes authorization.
- `eligible`: all supported checks are resolved without a mismatch. This is
  compatibility with the modeled checks, not a guarantee of employer eligibility.

Text extraction recognizes full sentences such as `A PhD is required`,
`Applicants must be currently enrolled`, `Visa sponsorship is not available`,
and `Candidates must graduate between 2027-01-01 and 2027-12-31`. Windows are
inclusive. Preferences, negations, alternative qualifications, malformed dates,
and unsupported wording do not trigger these exclusions. Degree level does
not establish degree completion; that uncertainty is reported separately.

For adapters that supply explicit facts, `eligibility_information` supports
`minimum_degree` (profile degree enum), `enrollment_required` and
`sponsorship_available` (strings `true`/`false`), `graduation_start` and
`graduation_end` (ISO dates), and `work_authorization_country` (matched to the
configured country name, case-insensitively). Current adapters generally leave
this mapping empty. Extraction is an English-language baseline, not a general
language parser; manual review remains necessary.

Ranking is a weighted average on a 0–100 scale. Unknown factors are shown as
`null` and contribute a neutral 50, including compensation/career potential;
no salary or employer-prestige values are invented. Default weights remain
35/25/20/10/10. Configure nonnegative finite weights from 0 to 100 with at least
one positive weight; they are normalized, so fractions or percentages work.

- Role alignment: primary title phrase 100, primary description phrase 80,
  secondary title phrase 70, secondary description phrase 55; otherwise 0
  when role preferences exist. The best match wins.
- Skills: each distinct configured skill mentioned contributes an equal share,
  reaching 100 at four matches (or all skills when fewer than four are listed).
  Missing descriptions without a skill match remain unknown.
- Education/experience: a preferred internship/graduate/entry-level label scores
  100; another recognized early-career label scores 50. Otherwise unknown.
  Eligibility exclusions are evaluated separately from relevance scores.
- Location: a recognized preferred place scores 100; other or missing locations
  remain neutral. The small Europe vocabulary covers common country/city names;
  extend it with `preferences.location_terms`, mapping region names to lists
  of explicit place phrases. This is not geocoding or a relocation assessment.

Phrase matching uses word boundaries and a small alias vocabulary, rather than
embeddings. Reasons say what the posting mentions, not that the candidate meets
all requirements. Unknown jobs are retained; ineligible jobs are omitted unless
`--include-ineligible` is requested, then appear after other jobs. Ties use a
stable company/title/source identity ordering. Closed jobs are never ranked.

Optional `career_tracks` entries override primary/secondary roles, and optionally
skills and weights, without changing the base profile. Other profile fields
remain shared. Only worldwide search is supported in this milestone;
`preferences.search_worldwide` must remain `true`. Region preferences affect
ranking rather than excluding Hong Kong or other regions.

## Daily Telegram digest

Follow the detailed [Telegram setup and recovery guide](docs/telegram-setup.md)
for database backup/migration, BotFather setup, private credentials, chat ID
discovery, previewing, sending, and troubleshooting.

For an existing database, back it up before running `job-scout db-migrate`.
Then, with your private environment loaded:

```sh
job-scout run --dry-run --limit 5
job-scout run --limit 5
job-scout delivery-status
```

`run` collects enabled boards, stores collection results, ranks freshly observed
jobs from successful sources, and sends only previously undelivered matches.
It respects `digest.minimum_score` (default 60), `maximum_matches`, and `timezone`.
CLI `--min-score`, `--limit`, and `--track` allow overrides. Clear ineligibility
is excluded; unknown eligibility is disclosed and remains eligible for inclusion.

Dry-run fetches boards but writes no jobs, health, digest plans, or delivery
success and makes no Telegram requests. It requires schema 2 but no bot token.
Set `TELEGRAM_CHAT_ID` for a preview that respects your delivery history;
without it, all matches are treated as undelivered. A saved unfinished digest
is previewed/resumed before any new collection.

One logical digest per recipient/local date may contain multiple numbered
messages, split between whole job entries. Plain text avoids interpreting
untrusted HTML/Markdown. Original links are never truncated. An empty result
produces a useful no-match message; failed-source coverage is disclosed, and
an all-source failure is reported as unavailable results.

A durable outbox records each part's attempt count, latest outcome, and confirmed
Telegram message ID. Confirmed parts/jobs are not resent. Partial failures resume
the saved content. Timeouts, ambiguous server responses, and interrupted sends
require an explicit `--retry-uncertain` after inspecting your chat; duplicates
are possible for those unconfirmed parts. Five-minute renewable recipient leases
prevent concurrent senders. Same-day completed runs return `already_sent`, even
after reduced coverage; recovered sources contribute to the next day's digest.

For an explicit additional same-day digest, use `job-scout run --additional`
(or `--additional --dry-run` to preview). This collects again and sends only jobs
not already confirmed delivered. If none remain, it sends a no-new-matches message.
Unfinished digests resume first; uncertain outcomes still require the separate
`--retry-uncertain` recovery option. Additional parts are appended to the day's
outbox without deleting history; no database migration is required. Scheduled
runs keep their once-per-day behavior.

Delivery identity is source/region/board/posting ID; cross-board reposts can
still appear separately. Keep the same database to preserve notification history.
The [deployment guide](docs/deployment.md) explains unattended cloud execution.
Scheduling stays disabled until you set the repository enable variable.

## Private configuration

`config/profile.example.yaml` is a sanitized, validated matching template.
`null` eligibility fields mean unknown. `.env.example` shows database
and Telegram settings. The `match` command loads the requested profile; `.env` is not automatically
loaded. Database commands read `DATABASE_URL` from the process environment.

Private `config/*.yaml` files (except `*.example.yaml`), `.env` files, `private/`,
and `data/` are ignored by Git. Keep actual profiles, CVs, and credentials out
of committed files. Review changes before committing.

## Development checks

With the virtual environment activated:

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

Use `python -m ruff format .` to apply formatting. Tests use synthetic fixtures
and HTTPX MockTransport; real HTTP transports are blocked in in-process tests.
Tests cover parsing, unknown/malformed fields, URL and timezone validation,
empty boards, retries, pacing, pagination, bounded concurrency, partial
failure, registry loading, and CLI output. SQLite tests cover repeat ingestion,
updates, UTC round trips, source health, rollback, closure/reopening, report
replays, and schema initialization. Matching tests cover exclusions versus
unknown eligibility, date boundaries, score ordering, neutral factors, profile
validation, tracks, and CLI integration. Digest tests cover read-only previews,
Unicode limits, confirmed-only deduplication, partial recovery, uncertain outcomes,
daily timezone gates, source failures, and revision 1 migration.
No live employer or Telegram calls are required.

## Architecture and next step

- `models.py`, `config.py`, `normalize.py`: shared records, registry loading,
  and text normalization.
- `sources/`: adapter protocol, shared HTTP behavior, Greenhouse, and Lever.
- `collection.py`: bounded orchestration and independent employer outcomes.
- `main.py`: CLI; provider parsing stays inside adapters.
- `storage/`: SQLAlchemy schema/setup and transactional repository.
- `matching/`: private profile validation, eligibility, phrase matching, and scoring.
- `notifications/`: formatting, Telegram transport, and resumable daily workflow.
- `storage/delivery.py`: persistent outbox, recipient leases, and delivery history.
- `tests/fixtures/`: documented synthetic API responses.

- `cloud.py`: private cloud configuration and sanitized execution summaries.
- `.github/workflows/`: offline CI with PostgreSQL, and scheduled/manual delivery.

**Next: deploy and observe the MVP.** Complete the deployment guide and confirm a
manual send and a scheduled run. Milestone 7 expansion is optional after that.
See `AGENTS.md` for scope and milestone requirements.
