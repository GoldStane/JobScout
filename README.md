# JobScout

On-demand CV tooling is available as a deterministic first increment: validate a
private evidence profile, render a factual selection into a fixed LaTeX template,
and compile/check a PDF. See [CV Tailor setup and limits](docs/cv-tailor.md).
AI rewriting and job-specific selection are not implemented yet.

A configurable job discovery and explainable matching service for opportunities
worldwide, guided by a private candidate profile.

**Status: database-backed company discovery, ATS monitoring and daily digests implemented.**
Discover, persist and rank jobs without preconfigured companies. Existing ATS
monitoring and Telegram delivery remain available locally or through GitHub Actions.
Follow the [cloud deployment guide](docs/deployment.md) to configure your database,
secrets, and opt-in schedule; cloud services are not provisioned automatically.


## Profile-independent design

JobScout's product objective is to support any profession, educational background
and career stage. Roles, skills, experience goals, locations, eligibility facts,
ranking priorities and digest settings belong in each user's private profile.
No particular major, study year, graduation date, residence or career preference
is a product requirement. Education and enrollment may be unknown or irrelevant.
Committed examples must be fictional and must not describe an actual candidate.

**Current limitation:** the matcher still contains early-career assumptions,
including a restricted career-level vocabulary and automatic senior-title
exclusions. Full support for experienced and other profiles requires changes to
the profile schema and matching rules; these documentation changes do not add
that runtime support. The requirements in `AGENTS.md` prohibit carrying those
assumptions forward as universal rules. Provider coverage also varies by region
and profession.

## Discover jobs with zero preconfigured companies

After the local installation below, copy `config/profile.example.yaml` to your
private `config/profile.yaml`, uncomment the needed YAML sections and fill in your
own preferences. The example is an intentionally non-runnable, comments-only
template. Then initialize a new database and run:

```sh
job-scout db-init
job-scout discover-jobs --profile config/profile.yaml
job-scout match --profile config/profile.yaml
```

Use `db-migrate` instead of `db-init` for an existing older schema. No employer
list, company YAML, board ID or API key is required. Supply your private profile
for personal interests, skills, career tracks and locations. Optional budgets
are in [config/discovery.example.yaml](config/discovery.example.yaml).
`discover-jobs --dry-run` fetches and previews without database writes or sends.

Discovery reuses the existing job repository and matcher. Its digest preview
uses **View listing** for aggregator URLs and credits each provider; it never invents
a direct application link. Read the [discovery guide](docs/job-discovery.md) for
setup, limits, terms, storage compatibility and expected output.

When a posting description contains a recognized ATS link, discovery now validates
and registers that board automatically for subsequent monitoring. Jobs without
ATS links remain usable. Registration has separate request/runtime budgets and
preserves disabled/rejected sources; `--dry-run` registers nothing.

`run` now discovers jobs and monitors registered ATS boards in the same daily
flow, including when the employer registry is empty. It reconciles verified
feed/ATS posting identities before scoring and delivery. Newly registered boards
join monitoring on the next run. Use `--no-discovery` for ATS-only operation;
`--registry-only` skips employer YAML seeds but still runs global discovery.

`discover-jobs` defaults to readable terminal output: run counts, ATS results,
and numbered matches with normal Unicode and separate reasons/uncertainties.
Use `--limit 5` for a shorter preview or `--format json` for scripts. JSON includes
structured `matches` as well as the existing digest preview. Standalone discovery
never sends messages and does not claim its matches are previously undelivered.
Routine HTTP request logs are suppressed.

Existing databases must be upgraded with `job-scout db-migrate` before this release.
Schema 6 adds discovery metrics, refresh scheduling and feedback alongside existing
request budgets and identity links. It preserves jobs, boards and delivery history.

## Discover companies, then monitor their jobs

```sh
# Preview company search queries without API requests or database writes:
job-scout discover-companies --config config/discovery.example.yaml --profile config/profile.yaml --dry-run
# After configuring companies.enabled and BRAVE_SEARCH_API_KEY:
job-scout discover-companies --config config/discovery.yaml --profile config/profile.yaml
job-scout companies list
job-scout monitor --registry-only --profile config/profile.yaml --dry-run
job-scout monitor --registry-only --profile config/profile.yaml
```

Company search uses engineering domains or profile roles/skills, verifies official
organization metadata, reciprocal careers/ATS links and the public ATS endpoint,
then registers the board in the database even with no current vacancies. It never
sends Telegram messages. `monitor` fetches registered boards and uses the existing
matcher and Telegram outbox; it is equivalent to `run --no-discovery`. `run` keeps
job-first discovery enabled for broader coverage. `companies` aliases the existing
`company` administration commands; `discover-sources --input` remains a manual importer.

PostgreSQL is authoritative in hosted runs. `companies.yaml` is an optional local
seed input and is never rewritten by discovery. Missing YAML does not prevent
monitoring database sources. Repeated seeds preserve explicit disables and existing
registrations. The obsolete `JOBSCOUT_COMPANIES_YAML` Secret is no longer read;
import any remaining manual seeds once against the hosted database before upgrading
scheduled runs. Existing schema 6 needs no new migration.

See [company discovery](docs/company-discovery.md) for opt-in settings, identity
requirements, retention limits, the weekly workflow and recovery.

## Coverage, feedback and health (M6)

Enable the no-key Remotive provider with `providers.remotive.enabled: true` in
`config/discovery.yaml`, then pass that file to `discover-jobs --config` or
`run --discovery-config`. It adds remote jobs across employers, credits Remotive
and preserves geographic restrictions. Its public feed is delayed by 24 hours.
Refreshes default to once per 24 hours, with a persisted six-hour minimum; failures
also consume the refresh interval. Dry-run uses recent cached jobs without fetching
Remotive. Arbeitnow remains the default, requiring no employer list.

```sh
job-scout discovery-health --days 30
job-scout source-health
job-scout jobs
# Copy a full job_key from jobs, match, or the discovery output:
job-scout feedback irrelevant JOB_KEY
job-scout feedback list
job-scout feedback clear JOB_KEY
```

`irrelevant` hides a vacancy and verified aliases from new matching/digest plans.
`relevant` records a positive review without overriding scores or eligibility.
Feedback is reversible and scoped: set `feedback_scope: search-a` in a profile and
pass `--scope search-a` to feedback commands to keep independent searches separate.
The default scope is `default`. Use `match --include-dismissed` to inspect hidden
jobs. Already prepared digests retain their saved content for reliable resumption.

Health reports count requests, observed jobs, qualifying observations and newly
stored records, with failures, pagination limits, last success and refresh due time.
These measure the bounded sample, not total market coverage. Records can overlap
across providers; repeated matches are not unique relevant vacancies. Existing daily
Himalayas role rotation and Brave role/location rotation remain deterministic and
bounded. No model API is required.

See the [M6 test procedure](docs/testing-m6.md),
[M6 behavior and limitations](docs/job-discovery.md#m6-coverage-feedback-and-health),
and [GitHub Actions deployment](docs/deployment.md).

## Optional: add Himalayas remote jobs

M3 is implemented: `run` combines global discovery, ATS monitoring, shared matching,
deduplication and Telegram delivery, including in GitHub Actions. M4 adds a second
free provider to that same flow. Arbeitnow remains enabled by default; Himalayas
is opt-in and needs no API key. Copy `config/discovery.example.yaml` to the ignored
`config/discovery.yaml`, then set `providers.himalayas.enabled: true`.

```sh
job-scout discover-jobs --config config/discovery.yaml --profile config/profile.yaml
job-scout run --discovery-config config/discovery.yaml --profile config/profile.yaml --dry-run
```

Use `mode: browse` for a bounded feed with opaque cursor pagination, or `mode: search`
for paged keyword searches generated from the selected profile's primary/secondary
roles. Search rotates a bounded subset daily; `--track` selects a career track.
Optional search filters are `country` or `worldwide_only`, plus `sort: recent` or
`relevant`. Page, request, retry, response-size and runtime limits are configurable.
In GitHub Actions, put these settings in the optional `JOBSCOUT_DISCOVERY_YAML` secret.

Each report includes unique observations, matching jobs, requests and expired
postings per provider, plus storage inserts/updates. These measure the fetched
sample, not total market coverage or guaranteed cross-provider uniqueness.
Himalayas covers **remote roles only**, and remote postings can restrict countries
and timezones. The digest preserves those restrictions, reported salary and visible
Himalayas attribution; unknown work authorization stays unknown. Explicitly expired
postings are excluded from new observations. Data refreshes daily; avoid polling
more frequently. See the [provider guide](docs/job-discovery.md#himalayas-m4) and
[official API documentation](https://himalayas.app/docs/remote-jobs-api).

M4 added backward-compatible JSON fields. This release requires schema 6 for
request accounting and M6 operations, so existing databases need `db-migrate`. Update every runner before ingesting Himalayas
jobs because older application versions do not recognize this provider.

## Optional: search roles in a selected country (M5)

Brave Search is an **opt-in, potentially billed API**. Free-feed operation still
works without it. If you choose to use your own Brave account, export
`BRAVE_SEARCH_API_KEY` securely and set `providers.brave.enabled: true` in your
private discovery YAML. A key by itself does not enable the provider.
Review [Brave’s current plans and storage terms](https://brave.com/search/api/).

```sh
job-scout db-migrate
job-scout discover-jobs --config config/discovery.yaml --track selected_track --country Canada
job-scout run --discovery-config config/discovery.yaml --country Canada
```

`--track` is optional; omit it to use the profile's primary/secondary roles.
`--country` overrides the country for enabled search providers for this run. It
switches an enabled Himalayas provider to search mode but never enables Brave or
Himalayas. Use full country names; common aliases include `HK`, `US`, `GB`, `CA`,
`AU`, `DE`, `FR`, `IT`, `ES`, `JP`, `SG`, `IN` and `CN`. Without an override, Brave
uses its configured country or rotates through the profile's preferred regions.
Arbeitnow remains a feed ranked locally; it cannot run a country search.

Brave searches profile role phrases and location text on supported public ATS
domains. This can discover on-site as well as remote opportunities at previously
unknown employers. A country search targets relevant indexed pages; it is **not a
verified job-location or work-authorization filter**. The returned posting's actual
location remains visible, and existing profile filters still apply.

Search URLs/snippets/titles stay transient. Only jobs independently fetched and
verified through Greenhouse, Lever or Ashby enter storage, matching, registration
and the daily digest. Unsupported links and stale index hits are discarded.
Canonical ATS identities prevent rediscovery from generating repeat notifications.

Default Brave caps are **3 search attempts/run, 6/day and 180/month**, including
retries. UTC daily/monthly counters persist in your database and are shared by
runners using it. Verification has separate request/runtime/board limits. These
are request ceilings, not a dollar-price guarantee or an account-wide billing cap.
Dry runs skip metered search and make no quota writes; free-feed/ATS previews
continue normally. For a live search without sending messages, use `discover-jobs`.

In GitHub Actions, add `BRAVE_SEARCH_API_KEY` as a secret and enable Brave in
`JOBSCOUT_DISCOVERY_YAML`. No account, subscription or deployment is created by this
implementation. See [the M5 guide](docs/job-discovery.md#brave-search-m5) for retention,
budget semantics and limitations.

## Optional: register known employer boards

Follow the [source discovery guide](docs/discovery.md). `company add <careers-url>`
detects Greenhouse, Lever, and Ashby boards, validates their public endpoints, and
activates unambiguous sources or stores a review candidate. Use `--dry-run` first.
Existing databases need `job-scout db-migrate` for schema 6 before other commands.

For a validation-backed seed import without job collection or notifications, use
`job-scout companies import-seeds --config private/companies.yaml --dry-run`, then
repeat without `--dry-run`. Existing registrations and explicit disables are
preserved. See [M7 registry work](docs/registry-m7.md) for limits and remaining work.

`run` and `ingest` use the database registry, seeded from optional YAML.
An empty or comment-only seed file is valid and adds no sources.
Registered disabled sources stay disabled on subsequent seed imports. Use
`company list`, `company candidates`, `company approve`, `company disable`, and
`company enable` to manage them. `--registry-only` monitors without a YAML file.
For a text list of careers URLs, use:

```sh
job-scout discover-sources --input employers.txt --dry-run
job-scout discover-sources --input employers.txt
```

Put one URL per line; blank lines and `#` comments are ignored. See
[employers.example.txt](config/employers.example.txt) for the format. Duplicate
boards are validated once; invalid inputs and temporary failures are reported
without discarding successful registrations. Unsupported career-page URLs remain
in review (no general website crawler). CSV/YAML imports remain later extensions. This command imports supplied URLs;
`discover-jobs` independently finds postings across employers.

## Local setup

Requires Python 3.12 or newer. From the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,postgres]'
job-scout --help
job-scout collect --help
```

If already installed, activate the environment and repeat the installation
command to install the collection dependencies. `python -m job_scout` is an
alternative entry point. No credentials are needed for public collection.

## Optional: monitor known employer boards

First check the CLI using the disabled example sources (no API requests):

```sh
job-scout collect --config config/companies.example.yaml
```

It prints an empty report and a warning that no sources are enabled. Then
create a private registry, if you do not already have one:

```sh
cp config/companies.example.yaml config/companies.yaml
```

To supplement global discovery with direct board monitoring, choose employers
you care about. Follow the careers link
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

Local commands and GitHub Actions use the same shared PostgreSQL database by
default. Set `DATABASE_URL` to the **same connection string** in your private local
`.env` and the GitHub Actions Secret. SQLite is never an implicit fallback.
See [deployment](docs/deployment.md#use-the-same-database-locally-and-in-actions)
for setup and how to verify both environments target the same database.

```sh
source .venv/bin/activate
python -m pip install -e '.[dev,postgres]'
# Load only your own trusted, private .env file (the CLI does not load it):
set -a
source .env
set +a
# New empty shared database only; existing older databases use db-migrate:
job-scout db-init
job-scout db-info
job-scout jobs
```

A missing/invalid PostgreSQL URL or failed connection stops the command; it never
creates or switches to a local database. Cloud URLs require TLS and complete
connection credentials. Database configuration errors return exit code `2`;
connection/schema failures return `3`, with credentials suppressed.

Your existing SQLite file is preserved. Select it explicitly for each command:

```sh
job-scout --local-db data/jobscout.db jobs
job-scout --local-db data/jobscout.db monitor --dry-run
# Optional isolated test database, created only by explicit request:
job-scout --local-db data/test.db db-init
```

`--local-db PATH` can appear before or after the command, including nested company
commands. It overrides `DATABASE_URL` for that invocation only. Relative paths
resolve from the current directory. This does not copy or synchronize data between
SQLite and PostgreSQL. Normal runs, including local previews, use shared jobs,
source registrations, feedback, quotas and delivery history. Use the same Telegram
chat ID to share notification suppression. Profiles remain separate configuration.
`db-init` never clears existing data; use `db-migrate` for older schemas.

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
  Distinct openings with matching titles remain distinct. In the daily run,
  an exact recognized ATS posting URL plus an agreeing title can link a feed
  occurrence to an independently fetched publisher posting. Ambiguous links,
  board-only URLs and title/location similarity do not cause merges.
- `first_seen_at` is retained; `last_seen_at` advances only when the job is
  actually fetched. Description, title, links, and source metadata are updated.
  Source-supplied posting/update dates remain separate from these timestamps.
- Three successful checks that omit a posting mark it closed. Failed checks
  neither increment nor reset its missing count. Reappearance resets the
  count and reopens the same record, preserving its first-seen time.
- Replaying a report, or ingesting an older source attempt after a newer one,
  does not change jobs or health. Source transactions serialize concurrent
  writers so repeated ingestion cannot create duplicate identities.
- Schema revision 6 adds `discovery_runs`, `discovery_schedule` and `job_feedback`.
  These store aggregate counters, refresh deadlines and scoped relevance decisions.
  Schema revision 5 adds `discovery_budgets` for atomic daily/monthly request counts.
  It contains no search-result content or credentials. Revision 4 retains `sources`, `jobs`, and `schema_version`, and adds
  `digests`, `notification_log`, and `delivery_locks`. Job data
  stores canonical fields and selected source metadata as JSON, with indexed
  identity, freshness, and lifecycle fields in columns. No profiles or
  credentials are stored in job rows. Delivery snapshots contain rendered messages
  and hashed recipient/job identities, never bot tokens or full profiles.
  `db-migrate` upgrades revisions 1 through 5 without losing jobs/source or delivery history.
  Revision 3 added companies, discovery candidates and source registration metadata.
  Revision 4 adds durable feed-to-publisher identity links and verification timestamps.
  `db-init` does not silently alter existing tables.

### PostgreSQL connection

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
# Replace TRACK_NAME with a career_tracks key in your private profile:
job-scout match --profile config/profile.yaml --track TRACK_NAME
```

Matching reads open jobs from `DATABASE_URL`. It makes no network requests,
changes no stored jobs, stores no profile, and sends no notifications. Results
are recomputed using the current profile each time; no schema migration is
needed. Run `ingest` first to refresh jobs. Output includes last-seen timestamps,
application URLs, scores, factor values, normalized weights, 1–3 explanations,
eligibility evidence, rejection reasons, and unknowns. It also reports the
number of open and ineligible jobs before filtering. `--limit` defaults to the
profile's `digest.maximum_matches`. Invalid profiles/options return exit `2`.

Current eligibility rules are narrow and include the legacy limitation noted above:

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
and explicit graduation-window sentences containing two ISO dates. Windows are
inclusive; these are posting requirements, not assumed candidate facts. Preferences, negations, alternative qualifications, malformed dates,
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
ranking rather than automatically excluding other regions. Explicit location
filters can narrow results separately.

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

`monitor` (or `run --no-discovery`) skips discovery configuration, provider calls,
and automatic board registration entirely. It monitors registered boards and uses
the same matching and digest flow.

`run` collects the enabled discovery feed and registered boards independently,
stores observations, reconciles verified duplicate identities, and ranks freshly
observed jobs before sending previously undelivered matches. A feed or ATS failure
is reported as reduced coverage without suppressing healthy results. Earlier feed
pages remain usable if a later page fails. `--discovery-config` selects optional
feed/registration budgets; company YAML and registered boards are optional.
It respects `digest.minimum_score` (default 60), `maximum_matches`, and `timezone`.
CLI `--min-score`, `--limit`, and `--track` allow overrides. Clear ineligibility
is excluded; unknown eligibility is disclosed and remains eligible for inclusion.

Dry-run fetches feeds/boards and previews registration, but writes no jobs, health,
identity links, source registrations, digest plans or delivery success and makes
no Telegram requests. It requires schema 6 but no bot token. Brave search and Remotive refreshes are skipped
in dry-run; recent cached Remotive jobs remain available.
`run --no-discovery --additional --dry-run` also leaves all delivery state unchanged.
`--additional` bypasses the completed-digest gate for the preview; it does not mark
previewed jobs as sent. Repeated previews fetch live boards, so changed postings,
source failures or another process sending a real digest can change their results.
With the same source data, profile and delivery history, previews select the same jobs.
The CLI explicitly labels previews and reports `dry_run`, including when today's
actual digest was already completed.

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

Delivery preserves existing source/region/board/posting keys. Verified equivalent
feed/ATS keys are saved together on digest parts and checked against older delivery
history, so either occurrence being previously sent suppresses another notification.
Separate source records remain available for provenance. Jobs without an exact,
independently confirmed identity link may still appear separately; names and titles
alone are insufficient. Keep the same database to preserve notification history.
The [deployment guide](docs/deployment.md) explains unattended cloud execution.
Scheduling stays disabled until you set the repository enable variable.

## Private configuration

`config/profile.example.yaml` is a schema example, not a required candidate
background or a source of personal defaults. Replace its preferences with your
own and keep actual candidate information in private, ignored files.
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
- `sources/`: adapter protocol, shared dispatch, HTTP budgets, Greenhouse, Lever, and Ashby.
- `discovery/http.py`: shared credential-free public API client and pinned-IP transport.
- `discovery/`: separate global job providers/orchestration and existing manual
  ATS registration; bounded Arbeitnow fetching uses the shared job repository.
- `collection.py`: bounded orchestration and independent employer outcomes.
- `main.py`: CLI; provider parsing stays inside adapters.
- `storage/`: SQLAlchemy schema/setup and transactional repository.
- `matching/`: private profile validation, eligibility, phrase matching, and scoring.
- `notifications/`: formatting, Telegram transport, and resumable daily workflow.
- `storage/delivery.py`: persistent outbox, recipient leases, and delivery history.
- `tests/fixtures/`: documented synthetic API responses.

- `cloud.py`: private cloud configuration and sanitized execution summaries.
- `.github/workflows/`: offline CI with PostgreSQL, and scheduled/manual delivery.

**Next: measure company-discovery yield and review unresolved identity/unsupported portals.**
Company discovery and weekly scheduling are opt-in; static-page verification is conservative.
See `AGENTS.md` and [the discovery guide](docs/job-discovery.md) for scope and limitations.
