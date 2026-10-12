# Source discovery and registration

This optional admin workflow registers supplied employer ATS URLs. It validates
public endpoints and activates boards or queues them for review. `discover-sources`
is a batch URL importer, not employer-independent job discovery. For the primary
`discover-jobs` workflow, see [global job discovery](job-discovery.md).
CSV/YAML dataset imports remain a future extension.

## Upgrade first

Activate your virtual environment and update the package. For an existing database,
back it up, stop concurrent writers, then explicitly migrate:

```sh
source .venv/bin/activate
python -m pip install -e '.[dev,postgres]'
job-scout db-migrate
```

Use `job-scout db-init` instead for a new empty database. Schema 3 adds companies,
discovery candidates, and source registration metadata. Versions 1 and 2 migrate
without changing source IDs or deleting jobs, digest parts, or delivery history.
Do not run an old checkout against the upgraded database.

For GitHub deployment: publish this version, then run **Daily search → migrate**
before sending. Import any existing manual seeds once locally with
`job-scout ingest --config private/companies.yaml` against the hosted DATABASE_URL.
The obsolete employer-list Secret is no longer read. Migration alone does not
assume that every historically fetched source should be re-enabled.

Commands require the shared PostgreSQL DATABASE_URL by default. Load the same
connection string used by GitHub into your local environment. For an explicitly
local registry, pass `--local-db data/jobscout.db` on each command. `.env` is not loaded automatically. Do not
paste the URL/password into committed files or public logs.

## Add a company board

Find the careers link on the employer's official site. Supported URL families:

- `https://boards.greenhouse.io/<board>` or `https://job-boards.greenhouse.io/<board>`
- `https://jobs.lever.co/<board>` or `https://jobs.eu.lever.co/<board>`
- `https://jobs.ashbyhq.com/<board>`

Job-specific paths on these hosts are normalized to their board. Tracking query
parameters and fragments are removed. Board identifier case is preserved.

First inspect the proposal (this contacts the public API but writes nothing):

```sh
job-scout company add 'https://jobs.ashbyhq.com/YOUR_VERIFIED_BOARD' --name 'Employer name' --dry-run
```

Replace YOUR_VERIFIED_BOARD with a real board copied from the employer's careers
site. The example is illustrative, not a verified employer. Then register:

```sh
job-scout company add 'https://jobs.ashbyhq.com/YOUR_VERIFIED_BOARD' --name 'Employer name'
job-scout company list
job-scout company candidates
```

`--name` is your display label, not a verified legal company name. Omit it to use
the board identifier as the label. No official company website is inferred from
an ATS domain. Different boards are distinct sources. By default they receive
separate provisional company records, even when their names match; use
`--company-id ID` when adding a verified second board to an existing company.

Known providers are automatically activated only after a valid, nonempty response
whose posting URLs all identify the supplied board. The engine reuses the existing
source adapters; it does not store fetched jobs during registration. Regular
ingestion collects and matches them afterwards.

## Import a text list

Create `employers.txt` with one actual careers URL per line. Blank lines and lines
beginning with `#` are ignored. Use UTF-8; a UTF-8 BOM is accepted. Company names
alone are not URLs, and no identifiers are guessed. There are no inline comments;
URL fragments are handled by URL normalization. Use
`config/employers.example.txt` as a template, replacing every placeholder.

```sh
job-scout discover-sources --input employers.txt --dry-run
job-scout discover-sources --input employers.txt
job-scout company candidates
job-scout company list
```

Preview validates public APIs but never opens the database, so it reports proposed
verification states without consulting existing operator overrides. A real import
uses DATABASE_URL and schema 3. No additional migration is needed for this command.
Use the cloud DATABASE_URL locally if these boards should feed GitHub's daily runs.

Each result includes its input line, normalized URL (when safe), state, reason, and
candidate/source IDs when persisted. The summary counts each outcome. Repeated
board/job URLs are deduplicated before HTTP validation. Unsafe inputs are reported
without echoing their raw URLs. Unsupported employer career pages remain in review;
this command does not crawl them for ATS links.

Successful registrations commit independently. Timeout/rate-limit/budget failures
remain retryable; rerun the same file or use `company revalidate ID`. Repeated
imports preserve disabled sources, existing rejections, and history. Rejected
candidates are skipped without requests. Import provenance records a SHA-256 of the
input and the line number, rather than retaining the entire file or its private path.

Defaults: at most 100 non-comment input lines, a 1 MB input file, sequential
processing (one candidate at a time), 60 API requests per provider for the entire
batch (including retries), and 2 MB per API response. Provider pacing is shared
across candidates. Adjust the supported bounds explicitly:

```sh
job-scout discover-sources --input employers.txt --max-urls 200 --max-requests-per-provider 120
```

`--max-urls` and `--max-requests-per-provider` accept 1–1000; `--max-bytes` accepts
1024–10000000. Over-limit files fail before any database changes. When a provider's
request budget runs out, remaining boards for that provider stay pending while
other providers can proceed. Per-candidate validation still has a 120-second deadline.

Exit 0 means all entries were processed without invalid inputs or temporary
validation failures; some may still require review. Exit 1 means partial failure
(consult the results); exit 2 means invalid command/input-file configuration.
Database setup/failure uses the existing exit 3 behavior.

The root `employers.txt` is ignored by Git. Put other private lists under `private/`.
The command does not send Telegram messages, modify YAML secrets, or install a schedule.

## Review and recovery

Candidate output includes an ID, normalized URL, validation result/reason,
registration source ID, timestamps, and audit events. States include
`pending_validation`, `verified`, `active`, `needs_review`, and `rejected`.

- Valid empty boards and boards returning custom/mismatched careers links require
  review. HTTP success alone does not prove the intended employer's identity.
- Unsupported URLs, including SmartRecruiters, are stored for review without an
  HTTP request. They cannot be approved until a supported provider is supplied;
  submit the employer's actual supported ATS link instead.
- Timeouts, rate limits, malformed responses, and budget exhaustion stay retryable.
  Retry with `company revalidate ID`. A crash leaves a pending candidate to resume.
- `company approve ID` is your explicit confirmation that a validated board belongs
  to the intended employer and collection is permitted. Validation must be less
  than 24 hours old; otherwise revalidate first. Invalid endpoints cannot be approved.
- `company reject ID` keeps a tombstone and disables an associated source. Repeated
  adds cannot silently undo rejection. Rejected-candidate restoration is deferred.
- `company disable SOURCE_ID` stops future monitoring without erasing history.
  `company enable SOURCE_ID` explicitly resumes an already registered supported
  source (except one rejected through the review queue).

```sh
job-scout company revalidate 12 --dry-run
job-scout company revalidate 12
job-scout company approve 12
job-scout company disable 4
job-scout company enable 4
```

Candidate IDs and source IDs are separate; use the IDs shown in the corresponding
list. Revalidation failures do not automatically disable already registered sources;
monitoring health remains independent from discovery health. An in-flight collection
may finish after a source is disabled; subsequent runs omit it.

## Database registry and existing YAML

The database is now canonical for `ingest` and `run`. Existing companies YAML remains
an operator-trusted seed. A normal non-dry run imports missing seed entries once,
then monitors all active database sources, including newly discovered ones. Enabled
legacy seeds are treated as manually approved configuration, not automatically
verified discovery. Unsupported seeds remain disabled.

Repeated imports never overwrite registered names, validation history, or enabled/
disabled decisions. **Changing `enabled` in YAML after its first import no longer
toggles that source**; use `company disable` / `company enable`. Removing a YAML entry
does not delete an existing registration. Historical unregistered sources stay
inactive unless seeded or added explicitly.

For database-only monitoring, without requiring a local companies file:

```sh
job-scout ingest --registry-only
job-scout run --registry-only --dry-run
job-scout run --registry-only
```

A dry-run merges unsaved seeds in memory and makes no registry writes. The standalone
`collect --config ...` command remains a stateless YAML-only debugging command.
Hosted runs use only database registrations, plus independent job-first discovery.
Keep the private profile and Telegram secrets as before. The separate opt-in
[company-discovery workflow](company-discovery.md) expands the registry weekly.

Already-sent digests still follow the daily gate. Use `run --additional` (or the
GitHub additional checkbox) when you explicitly want another same-day digest after
adding sources, keeping confirmed jobs suppressed.

## Limits and safety

Each command processes one URL with concurrency 1. Public API requests are paced
at least 0.5 seconds apart, with 20-second request timeouts, a 120-second total
validation deadline, and at most three transient retries per request. Defaults are
12 total requests (including retries) and 2 MB per response; override with
`--max-requests` (1–100) and `--max-bytes` (1024–10000000). These bounds can leave
large/paginated boards pending until retried with a larger budget. Only one provider
is contacted per command, so the command budget is also its provider budget.

Arbitrary input URLs are never fetched. Only HTTPS GET requests to the fixed ATS
API hosts are allowed. DNS must resolve entirely to public addresses; the connection
is pinned to a checked address while preserving TLS certificate hostname checking.
IP-literal, credential-bearing, internal-host and nonstandard-port input URLs are
rejected. Redirect budget is deliberately zero; returned posting links are inspected
as text, never followed. Compressed API bodies are refused and uncompressed responses
are size-limited. HTML, JavaScript, authentication, robots bypasses and browser
automation are not used. This first milestone does not crawl employer pages.

All tests use synthetic fixtures and mock transports; no live jobs, email, or
Telegram are needed. PostgreSQL-specific tests run in CI. Registration and manual
review outputs contain employer/board information; keep them private if that list
is sensitive. No accounts, paid services, or cloud resources are provisioned.
