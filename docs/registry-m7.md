# M7: database-authoritative registry

## Existing foundation

Schema 6 already contains persistent companies, distinct sources with unique
`(provider, region, board_id)` identities, validation metadata and collection
health. Hosted runs require PostgreSQL; SQLite is explicitly selectable locally.
Monitoring uses the database registry after optional seed import. Missing seed
files do not remove database registrations. No new migration is needed for this
increment, and existing registrations and job/delivery history are preserved.

## Validated seed import

Use the same DATABASE_URL as the scheduled deployment (never print or commit it):

```sh
job-scout companies import-seeds --config private/companies.yaml --dry-run
job-scout companies import-seeds --config private/companies.yaml
job-scout companies list
job-scout companies candidates
```

The command accepts the existing `source`, `company`, `board_id`, `region`,
`enabled` YAML schema. Missing, empty and comment-only files are no-ops.
An initialized, current-schema database is required, including for dry-run so
existing operator decisions can be checked. Dry-run performs public endpoint
validation but writes nothing and never sends notifications or stores jobs.

New enabled seeds reuse the ATS URL detector, safe public transport and board
validator. Only successful, unambiguous validation activates a new source;
empty boards and ambiguous responses remain candidates for review. This confirms
endpoint/posting-link agreement, not a company's legal identity. New disabled
seeds are stored disabled without HTTP. Existing registered sources are skipped,
preserving their labels, provenance, validation timestamps and disabled status;
rejected candidates remain rejected. YAML is never rewritten.

Defaults: 100 input sources, 60 total HTTP requests (including retries), 2 MB per
response, and 120 seconds for validation. Override with `--max-sources`,
`--max-requests`, `--max-bytes`, and `--max-runtime-seconds`. Exhausted budgets
defer remaining enabled sources; successfully imported sources remain durable.
Rerunning skips registered sources and retries unresolved candidates. Exit 1 means
some sources need review, failed validation, were rejected or were deferred.

Example successful output (abbreviated):

```json
{"dry_run": false, "requests": 1, "counts": {"active": 1}}
```

Use `companies revalidate CANDIDATE_ID` and `companies approve CANDIDATE_ID` for
review, and `companies disable SOURCE_ID` / `companies enable SOURCE_ID` for
explicit administrative changes. Disabling preserves historical jobs and delivery
records. For backups, retain a database-native backup; `companies list` is an
inspection report, not a full restore format.

## Remaining M7 work

This is the first M7 increment, not milestone completion. The older automatic
seed import in `run` and `ingest` still trusts manually supplied enabled seeds
without validating them first. Moving those paths onto validation, handling
legacy active-but-unvalidated seeds without discarding manual registrations, and
testing migration/recovery across ephemeral PostgreSQL runners remain outstanding.
For validated imports today, use this command followed by `monitor --registry-only`.
M8 company discovery already has implementation in this repository; audit its
remaining requirements after finishing M7 rather than rebuilding it.

All new validation tests use synthetic HTTP. No paid services or additional
dependencies are introduced. Live provider and production database access are
not tested by the offline suite.
