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
Empty engineering domains use profile roles, falling back to skills only when there
are no roles. Prefer employer-focused `engineering_domains`, such as
`[trading systems, low latency, embedded systems]`, over standalone language names. Employer
queries rotate weekly across interests and preferred regions (or Brave's country
override). A matching interest must also occur on the independently fetched site;
search snippets alone never establish relevance. The site allowance is shared fairly
across remaining queries: five sites and three queries allow 2/2/1 sites. Unused
allowance passes forward when a query has fewer unique sites. Denied sites still
consume their allowance, but do not prevent later queries from being attempted.
Request and runtime limits can still end the run earlier.

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
homepage must expose an unambiguous JSON-LD Organization or Corporation name and
same-origin URL. Without that metadata, a single `og:site_name` is accepted only
when the ATS page independently exposes the same unambiguous site name, in addition
to the reciprocal link and API checks below. Missing or conflicting names remain
unregistered. See [Schema.org Corporation](https://schema.org/Corporation) and
[Open Graph site-name metadata](https://ogp.me/#optional).
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
HTTPS only, bounded same-origin page redirects, bounded response sizes/timeouts/retries, robots checks,
and pacing. Robots denial or uncertainty fails closed. Scripts are parsed only for
organization metadata and never executed. This first implementation deliberately
misses sites with cross-origin redirects, client-rendered careers pages, missing corroborated identity
evidence, missing backlinks, or unsupported ATS portals. A valid API alone does not
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

## Diagnose failed discovery

Company discovery now prints progress/failure diagnostics to stderr while keeping
its JSON report on stdout. Each diagnostic includes a query/site ordinal, stage,
reason code, an HTTP status when applicable, and a suggested next action. Stages
separate search, robots, homepage, careers, ATS page, ownership and ATS API checks.
Unverified search-derived domains/URLs and snippets are omitted from both logs and
saved diagnostic counts. Site numbers are scoped to the current run, not database IDs.

A run with site failures or deferred work reports `partial` and exits with code 1.
A fatal search/runtime error with no registrations reports `failed`. A completed
scan with no new registrations reports `no_new_boards`; this does not imply that
companies were registered. Temporary ATS validation failures also count as failures.
The weekly Actions summary includes the same allowlisted reason counts and actions.

Read the most recent saved company diagnostic counts without a profile, API key,
network search or database writes:

```sh
job-scout discover-companies --last-report
```

This also shows `next_due`. Runs saved before diagnostics were implemented have
`diagnostics_available: false`; their original errors cannot be reconstructed.
A normal `not_due` response now shows the next eligible run time as well.

For an intentional new attempt before the cooldown expires:

```sh
job-scout discover-companies --config config/discovery.yaml --retry-now
```

This can spend metered search requests and register verified boards. It resets the
company cooldown from the new attempt but **does not bypass** shared day/month
request allowances, server quota handling, per-run limits, or URL/access checks.
It does not send Telegram messages. `--retry-now --dry-run` remains a query-only
preview with no requests or writes. Scheduled workflows never pass `--retry-now`.

To save the safe progress log locally while retaining JSON on the terminal:

```sh
mkdir -p private
job-scout discover-companies --config config/discovery.yaml --retry-now 2> private/company-discovery.log
```

Examples of actionable reason codes:

- `http_redirect`: cross-origin, unsafe, missing, looping or excessive page
  redirects require review. Robots-file redirects are not followed.
- `http_denied`, `robots_denied`, `page_opt_out`: respect the publisher's access
  policy and continue with other candidates.
- `search_denied`: check the search API key and subscription permissions.
- `timeout`, `http_server_error`, `dns_error`: transient transport/publisher issue;
  retry later before increasing budgets.
- `identity_missing`, `identity_ambiguous`, `reciprocal_link_missing`: ownership
  cannot yet be corroborated; manual review or additional verification patterns
  are needed. These counts do not guarantee a persisted candidate record.
- `site_limit`, `board_limit`, `request_budget`: identify the actual exhausted
  allowance before changing configuration.

Diagnostics are capped at 200 entries per run; `diagnostics_omitted` reports any
additional entries. Saved reports contain aggregate stage/reason counts rather
than raw errors or site URLs. No schema migration is needed for these counters.


### Redirects and unused site allowance

`companies.max_redirects` defaults to 3 (range 0–5; 0 disables following).
Page redirects are followed only within the same HTTPS origin. Each destination
undergoes URL/DNS safety and robots checks and consumes the existing request budget.
Cross-origin redirects, missing destinations, loops and excessive hops fail closed.
Robots-file redirects remain unsupported. A redirect never establishes ownership;
the existing identity, reciprocal-link and ATS API checks still apply.

After all queries, spare site slots inspect deferred candidates from earlier
results without extra search requests. This queue exists only in memory.
`site_limit` means candidates remain after the total allowance is exhausted,
not merely that one query has used its initial share.
