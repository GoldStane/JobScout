# Company-first discovery

`discover-companies` searches for employers by engineering interests and registers
verified ATS boards in PostgreSQL (SQLite locally). It is independent of vacancy
matching and never sends notifications. `monitor` subsequently collects those boards,
ranks jobs and sends the existing Telegram digest. `run` combines monitoring with
employer-independent job feeds. No employers file is required by either discovery path.

## Configure and preview

Use the existing private profile and copy the relevant settings from
`config/discovery.example.yaml` into your private discovery configuration:

```yaml
companies:
  enabled: true
  engineering_domains: [robotics, distributed systems]
  max_sites: 5
  max_careers_pages: 2
  max_site_requests: 30
  refresh_hours: 168
providers:
  brave:
    enabled: false
    max_queries: 3
    max_pages: 1
    max_requests: 3
    max_boards: 5
    daily_request_limit: 6
    monthly_request_limit: 180
```

`companies.enabled` opts in to metered company search; `providers.brave.enabled`
independently controls job-first web search. Both share the Brave settings, quota
headers, and atomic database day/month request reservations. Use the same budget
configuration in every runner. Search is never enabled just by supplying a key.
Empty engineering domains fall back to the profile's roles and skills. Employer
queries rotate weekly across interests and preferred regions (or Brave's country
override). A matching interest must also occur on the independently fetched site;
search snippets alone never establish relevance.

```sh
job-scout discover-companies --config config/discovery.yaml --profile config/profile.yaml --dry-run
```

Dry-run prints generated queries and makes **no HTTP requests or database writes**.
For a real run, initialize a new database (`db-init`) or migrate an older one
(`db-migrate`), set `BRAVE_SEARCH_API_KEY` in your environment, then omit `--dry-run`.
No paid requests, accounts, or production changes are made by installing this code.
Review your account's costs and publisher access terms before enabling searches.

## Verification and retention

Search URLs are ephemeral leads. A bounded fetch of the candidate site's HTTPS
homepage must expose an unambiguous JSON-LD Organization name and same-origin URL.
The engine follows only a bounded number of same-origin Careers/Jobs links. A
supported Greenhouse, Lever or Ashby link must be observed, never guessed. The
ATS-hosted board page must link back to the organization website, and the public
ATS API must pass the existing schema/link validation. This reciprocal evidence
permits registering a valid empty board, without inventing current vacancies.

Only independently corroborated organization/board identities and compact publisher
verification evidence are persisted. Search URLs, snippets, titles and raw responses
are never saved. Unresolved identities and unsupported portals are counted in the
report; they are not retained as search-derived company records. Corroborated boards
with temporary API failures become retryable candidates. No new job records or
notifications are created by company discovery.

The official website identifies newly discovered companies, so multiple boards
with the same verified organization identity share one company. Conflicting names
on the same website are deferred for manual review. Existing manually registered
identities are preserved, not automatically merged or renamed. Existing disabled
boards and rejected candidates are not reactivated. Use `companies candidates`,
`companies revalidate ID`, `companies approve ID`, `companies disable ID` and
`companies enable ID` for explicit administration; source IDs and candidate IDs
are distinct. Approval is an operator identity decision, not an automatic retry.

Requests use public-IP DNS pinning, separate credential-free publisher clients,
HTTPS only, zero redirects, bounded response sizes/timeouts/retries, robots checks,
and pacing. Robots denial or uncertainty fails closed. Scripts are parsed only for
organization metadata and never executed. This first implementation deliberately
misses sites with redirects, client-rendered careers pages, missing organization
metadata, missing backlinks, or unsupported ATS portals. A valid API alone does not
establish ownership. Synthetic tests verify behavior; live discovery yield has not
been measured.

The Brave API reference and storage requirements were checked during implementation:
[Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get),
[Brave API rights and FAQ](https://brave.com/search/api/). Brave grants no blanket
rights to publisher content; this engine keeps identity evidence, not full pages.

## Results and operations

Output is JSON with `registered`, `pending_review`, `unsupported`, `failed`, request
counts, and verified publisher outcomes. For example, a verified company with an
empty board reports `registered: 1`; a later run skips that registered board.
`not_due` means another run already reserved the persisted refresh interval.
Failures retain the cooldown to prevent repeated metered retries. Partial runs
keep completed registrations; requests and board/site validations have fixed caps.

```sh
job-scout companies list
job-scout discovery-health
job-scout monitor --registry-only --profile config/profile.yaml --dry-run
job-scout monitor --registry-only --profile config/profile.yaml
```

Company health includes verified boards per 100 total requests. Normal monitor
runs retain the daily digest gate; `--additional` explicitly requests another
same-day digest while suppressing previously delivered jobs. `--dry-run` previews
without persistence or Telegram delivery. Existing `collect`, `ingest`, `company`,
`discover-sources`, `discover-jobs`, and `run` commands remain compatible.

Schema 6 already contains every needed table; this increment needs no new schema.
Back up PostgreSQL with your provider's snapshot/export or `pg_dump` before any
migration. `companies list` provides a read-only JSON registry inspection/export;
full database backup preserves provenance and delivery history as well. Neither
company discovery nor scheduled workflows write `companies.yaml`. Local YAML seeds
remain optional and repeated imports preserve database decisions.

The opt-in weekly workflow and legacy employer Secret migration are described in
[deployment](deployment.md#company-first-discovery-and-employer-list-migration).
Next work is to measure verified-board yield and extend independently verifiable
identity patterns based on actual publisher evidence, without broad crawling.
