# Employer-independent job discovery (M1–M4)

## Audit and integration decisions

The existing repository already provides Greenhouse, Lever and Ashby collectors,
a shared `Job` model, SQLAlchemy/SQLite/PostgreSQL repository, schema revisions
1–3, deterministic eligibility/scoring, Telegram outbox/delivery, GitHub Actions,
and manual ATS board detection/registration. These components are retained.

The old `discovery` package registers user-supplied ATS URLs. The new provider
protocol in `discovery/base.py` instead returns jobs across unknown employers.
`discover-jobs` never loads company YAML, reads an employer list, or guesses a
board from an employer name. Existing career tracks, skills and location settings
in `--profile` drive the same matcher used by `match` and `run`. Arbeitnow does
not provide keyword search here: the bounded feed is ranked locally.

## Start with zero companies

After installing the project, initialize a fresh database, or use `db-migrate`
for an existing revision 1/2/3 database. This release requires schema 4.

```sh
job-scout db-init
job-scout discover-jobs --profile config/profile.example.yaml
job-scout match --profile config/profile.example.yaml
```

The example profile is sanitized. For personal ranking, supply your private
profile. No `companies.yaml`, `employers.txt`, ATS board, account or API key is
required. Configure budgets separately from your interests:

```sh
job-scout discover-jobs --config config/discovery.example.yaml --profile config/profile.example.yaml
job-scout discover-jobs --profile config/profile.example.yaml --dry-run
```

Both modes require an initialized database. Dry-run fetches and ranks without
writing jobs, source health, or notification state. Neither mode sends Telegram
messages. The default terminal report includes ingestion counts, unique fetched jobs, matched count,
pages/requests, skipped records, an error, and a readable match preview. Use `--format json` for structured `matches` and the
backward-compatible `digest_preview` field; `--limit` controls how many are shown. The preview
does not check delivery history and does not claim actual delivery.
For the two-employer synthetic test fixture, the first run reports
`ingestion.inserted: 2`; the next reports `ingestion.unchanged: 2`.

Exit status is 0 for a successful bounded run (including an empty/disabled feed),
1 for reduced feed coverage or transient ATS validation failures,
2 for invalid configuration/profile, and 3 for database/setup failures.
Hitting the configured page limit is expected: `page_limit_reached` reports it.
Earlier valid pages survive a later request failure. Failed records are counted
without logging untrusted descriptions or credential-bearing links.

## Persistence and lifecycle

Feed jobs use the existing `jobs` table and common upsert logic. The required
`source_id` FK points to one automatically created provider namespace row:
`arbeitnow/global/feed` or `himalayas/global/feed`, with registration status `feed` and origin
`global_discovery`. This is **not an employer/ATS registration**. It creates no
company record, is excluded from active ATS monitoring, and cannot be enabled as
an ATS board. Source health includes this feed row.

Provider + posting ID is the stable identity (Arbeitnow slug; SHA-256 of the Himalayas GUID, with the original GUID retained in metadata). `company_identifier="feed"`
represents the provider namespace in the existing identity format; `company`
contains the actual employer name. Identical titles/locations are not merged.
Repeated observations preserve first-seen dates and notification keys. Bounded
feeds never increment ATS missing checks or close jobs by absence. Availability
is unverified after the last observation; inspect `last_seen_at`. Stored rows are not automatically closed on feed expiry; Himalayas explicitly expired observations are excluded from acquisition; exact feed/ATS identity reconciliation
is available in the unified daily run.

Canonical JSON gains optional `listing_url`, `attribution`, `attribution_url`
and nullable `application_url`. Older jobs still load; no existing rows, IDs,
source settings or notification history are rewritten by setup. Update all
runners before ingesting feed jobs: older application versions cannot read the
new provider/JSON fields. M3 adds the `job_links` table in schema 4; run `db-migrate` on older databases.

## Provider contract, terms and coverage

The [official API description](https://www.arbeitnow.com/blog/job-board-api),
[public endpoint](https://www.arbeitnow.com/api/job-board-api), and
[terms, section 11](https://www.arbeitnow.com/terms) were checked on 2026-10-11.
The bounded sample had `data`, `links.next`, and `meta`, with 325 postings on the
first page. IDs are `slug`; dates are Unix-second `created_at`; `job_types` is an
array. Page size is not assumed. Regional listing URLs in the sample included
Arbeitnow's `.com`, `.co.uk`, `.ch`, and `.fr` domains.

The free API requires no key. Its terms require a link back to Arbeitnow and
allow access to be revoked. JobScout retains attribution and renders
“Source: Arbeitnow — https://www.arbeitnow.com/” in each job block. This is a
personal job-search tool, not permission to republish a commercial job database;
review current terms before changing usage. API access/freshness is not guaranteed.

The adapter only contacts the fixed HTTPS API endpoint; pagination URLs are
validated and reduced to a page number, never followed. Production transport
pins public DNS targets with TLS hostname validation. Redirects, private DNS
addresses, credential-bearing/unknown listing hosts and oversized responses are
rejected. Listing pages and description links are never fetched. Defaults cap
requests at 9 (including retries), pages at 3, runtime at 120 seconds, and response
size at 5 MB. HTTP timeout is 20 seconds; bounded retry/backoff handles 429,
5xx and transport errors. Long Retry-After values defer to a later run.

No independent direct application link is promised by this feed. Its URL remains
a **View listing** link with `application_url=null`. HTML becomes safe plain
text. Missing salary, sponsorship, enrollment, geographic restrictions and
relocation information remain unknown. `remote=true` does not mean worldwide
work eligibility; `remote=false` does not distinguish on-site from hybrid.

Coverage is Europe-heavy and a bounded recent sample, not universal global
coverage. Jobs outside the candidate's tracks can be ingested but are evaluated
by the shared matcher. No paid provider, search-index retention, LLM, arbitrary
website crawler or application submission is introduced.

## Automatic ATS registration (M2)

Arbeitnow and Himalayas descriptions may contain original careers/ATS anchors. The adapter
extracts at most ten distinct recognized boards per posting from these links,
ignoring script/style/template contents. It never guesses from company names,
visits listing pages, follows tracking redirects, or crawls arbitrary sites.
Canonical board leads are retained in job `source_metadata.ats_board_leads`.
A feed without any such links is still ingested and ranked normally.

Jobs are committed before validation starts. The registration phase deduplicates
provider/region/board identities across postings, skips active or disabled boards
and rejected candidates, and reuses the manual registration validator. Only
Greenhouse, Lever (including EU), and Ashby are supported. Their fixed public API
endpoints use the existing pinned-public-DNS transport, TLS hostname checking,
zero redirects, finite retries and response-size bounds. Unknown domains and
unsupported providers are not requested or auto-registered.

The existing registry verifies nonempty boards whose returned posting URLs agree
with the board identity. Board slugs are used as provisional display labels;
a feed employer name is not treated as verified ownership. Empty/ambiguous
boards enter `needs_review`; transient/schema failures enter `pending_validation`
with a reason. Existing manual registrations and operator decisions survive.
Candidates record `method=global_discovery`, validation time, and audit evidence
containing up to ten provider posting IDs/listing links. Successful boards become
available to subsequent `ingest --registry-only` and `run --registry-only` runs.

The separate `registration` configuration defaults to 20 new-board validations,
60 total HTTP requests including retries, a 120-second total runtime, one-second
request pacing and 2 MB per response. A failing board does not discard feed jobs
or stop other boards while budget remains. Budget-deferred leads stay on the
stored feed jobs and are reconsidered if observed in another discovery run;
there is no background queue processor yet. Disable the phase with
`registration.enabled: false`, or set `max_new_boards_per_run: 0`.

`discover-jobs` JSON now includes `ats_registration` with outcomes, request count,
deferred count and a runtime error if present. Example: a validated Ashby lead
reports `state: active`, `validation: attempted`; the next run reports
`validation: skipped_existing` without another validation request. Dry-run makes
bounded validation requests but writes no candidates, sources, jobs, or delivery
state; a successful proposal reports `verified` instead of `active`.

M2 used existing audit fields without DDL changes. M3 now requires schema 4 for
persistent job identity links. Board verification does **not** verify a feed
vacancy's application URL: standalone feed jobs keep their listing links and
attribution. `discover-jobs` never sends messages or creates notification rows.

```sh
job-scout discover-jobs --profile config/profile.example.yaml
job-scout company list
job-scout company candidates
job-scout ingest --registry-only
```

## Unified daily run (M3)

```sh
job-scout db-migrate  # Existing database; db-init for a new one.
job-scout run --profile config/profile.example.yaml --dry-run
# Supply Telegram environment secrets before a real send:
job-scout run --profile config/profile.yaml
```

Feed discovery and registered ATS collection run independently. Healthy results
and valid early feed pages survive provider failures. Jobs are persisted before
bounded board registration; newly registered boards are monitored on the next
run. Registration errors are reported but do not suppress delivery. No employer
file or company YAML is required. `--discovery-config` supplies optional budgets;
`--no-discovery` disables feed acquisition, while `--registry-only` only disables
employer YAML seeding. A completed daily digest prevents redundant collection;
unconfirmed saved digests resume before any new fetch. Additional manual runs
continue to respect confirmed delivery history.

Schema 4 adds `job_links` (feed key, publisher key, verification time and method).
The additive migration keeps every original posting, registration and notification
record. A feed HTML anchor is retained as an unverified `ats_posting_leads` URL;
only recognized exact posting paths are eligible. A link is confirmed only when
its provider/region/board/posting ID agrees with an independently fetched ATS
record and its title agrees after case/whitespace normalization. Multiple posting
identities, title disagreement, unknown domains and board-only links do not merge.
The mapping is persisted so later text edits cannot cause duplicate notifications.

Deduplication happens before ranking. A freshly fetched publisher occurrence is
preferred and keeps feed attribution; a missing publisher description can use the
fresh feed description. A stale publisher is used only for identity evidence, not
to replace a fresh feed listing or assert current availability. Digest parts record
all equivalent occurrence keys. Existing notification keys also suppress linked
jobs if the feed was sent before ATS monitoring discovered it, or vice versa.
Jobs lacking exact corroborated links remain separate; universal cross-source
identity matching is not claimed.

Dry-run computes the same matches without writing jobs, identity links, board
registrations, health or delivery records, and sends nothing. Uncertain Telegram
outcomes retain the existing conservative retry behavior; no exactly-once external
delivery guarantee is claimed. The same orchestration runs in GitHub Actions with
optional `JOBSCOUT_DISCOVERY_YAML` settings and no required employer secret.

## Himalayas (M4)

Himalayas is disabled by default. Enable it in a private copy of
`config/discovery.example.yaml` and pass that file using `discover-jobs --config`
or `run --discovery-config`. Cloud runs accept the same YAML through the optional
`JOBSCOUT_DISCOVERY_YAML` secret. No employer list or API credential is involved.
`run --no-discovery` disables both feeds, and the existing matcher is shared.

### Contract and safe collection

The [official reference](https://himalayas.app/docs/remote-jobs-api),
[OpenAPI specification](https://himalayas.app/docs/openapi.json) and
[terms](https://himalayas.app/terms) were checked on 2026-10-11. A bounded browse
sample (one record) and search sample (one page) confirmed the envelopes; fixtures
contain only synthetic employers and descriptions. Production only contacts the
fixed HTTPS API host through the public-IP-pinned transport. Redirects are refused;
listing pages, logos and description URLs are not fetched.

- `mode: browse`: `/jobs/api`, configurable `page_size` from 1 to 20. Echo the
  response's opaque `nextCursor` without parsing or constructing it. Its absence
  ends the walk. Each run starts at the recent end of the feed; no persistent cursor.
- `mode: search`: `/jobs/api/search`, one-based `page`, with pagination bounded by
  the returned `limit` and `totalCount`. `page_size` applies only to browse.
  Queries are normalized, case-insensitively deduplicated primary/secondary roles
  from the selected profile or track, limited by `max_queries` (default 3).
  A deterministic UTC-day rotation covers role families without a Cartesian
  product or model-generated queries. No roles means one unfiltered search.
  Optional `country` accepts a country name/code, or `worldwide_only: true` requests
  worldwide-friendly postings; choose one. `sort` supports `recent` (default) and
  `relevant`. These provider filters require search mode and do not infer citizenship.
- `max_pages` applies per query (default 3); `max_requests` is shared across all
  queries and retries (default 9). Runtime is shared too (default 120 seconds),
  responses are bounded to 5 MB by default, and HTTP timeout is 20 seconds.
  A 429 waits at least 60 seconds before retrying, respecting a longer Retry-After
  by deferring to a later run. Runtime may stop a retry sooner without issuing
  another request. Partial successes remain usable if later pages fail.

The API is cached daily; schedule normal collection once daily. JobScout's
completed daily delivery guard skips recollection on catch-up runs. Standalone
previews and explicit additional runs can refetch, so avoid repeatedly invoking
those against unchanged data. No automatic paid endpoint is enabled.

### Facts, attribution and eligibility

Both the documented and observed response shapes are supported: country strings
or country-name objects, timezone offsets, UTC-aware ISO dates, Unix seconds and
Unix milliseconds. Empty country restrictions mean worldwide; absent restrictions
remain unknown. Country/timezone restrictions, explicit expiry and reported salary
are preserved. Missing salary currency or pay period stays unknown, and salary is
not converted or assigned a ranking bonus. The shared matcher's existing profile
and eligibility limitations still apply; timezone compatibility is displayed for
review, not evaluated against an inferred candidate timezone. Remote does not
establish work authorization or visa sponsorship.

`applicationLink` is a Himalayas listing URL, so it remains **View listing** with
no verified direct application URL. Every job display retains visible
**Source: Himalayas — https://himalayas.app/** credit. Reconciliation with an
independently fetched ATS posting preserves all participating feed credits.
Missing publisher pay/timezone facts may be supplemented from agreeing fresh
feed observations. No stale feed facts are substituted.

Explicitly expired incoming postings are counted separately and omitted from the
run's recommendations. A bounded feed cannot prove the status of absent postings;
stored historical rows are not closed just because a page omits them. The daily
run recommends only freshly observed jobs, while `match` can still inspect stored
history and should be read with `last_seen_at` in mind.

### Yield and limitations

`discover-jobs` reports `providers[]` in JSON and a per-provider terminal summary:
requests, pages, queries, unique provider IDs, matching jobs, expired/invalid counts,
errors, page caps and insertion/update counts. Repeated query hits count once per
provider. The synthetic two-employer Himalayas fixture yields two unique jobs on
first ingestion and zero inserts on repetition, demonstrating incremental yield
accounting rather than making a live coverage claim. Compare these counters with
your profile over time before raising budgets.

Himalayas adds remote-only coverage and will miss on-site opportunities. Country
and timezone restrictions mean its remote supply is not universally accessible.
Browse is only a bounded recent sample; targeted search can improve relevance but
miss alternative role wording. Cross-provider overlap is only merged when exact
posting identity is corroborated by a publisher; similar titles alone stay separate.
This deliberately leaves some duplicates when no trustworthy common identity exists.

Provider failures remain isolated from each other and ATS monitoring. The two feeds
share one per-run ATS-validation budget and one daily digest/delivery history.
M4 adds no DDL beyond M3's schema 4; new salary, timezone and attribution fields
live in canonical job JSON and default safely for older rows.

## Next milestone

M5 adds optional broader discovery with explicit source rights and budgets.
Profile-schema and career-stage limitations described in the README remain.
