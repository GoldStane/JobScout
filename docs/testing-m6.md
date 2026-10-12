# Testing M6

## Offline acceptance checks

From the repository with Python 3.12+ and the development environment activated:

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
# Focus on the new milestone while developing:
python -m pytest tests/test_m6.py tests/test_cloud.py -q
```

Tests use synthetic HTTP responses and temporary SQLite databases. No real profile,
provider credential, Telegram bot or employer is required. Network transports are
blocked by the test harness. Coverage includes:

- Empty-registry Remotive ingestion across employers, shared matching and attribution.
- Malformed/empty/unsafe responses, 429/503/timeouts, bounds and provider isolation.
- Concurrent refresh reservation, cooldown expiry, no immediate retries and no dry-run writes.
- Cached observations retaining source health/freshness and avoiding inflated yield counts.
- Scoped feedback, verified aliases, clear/review behavior and unchanged hard eligibility.
- Feedback filtering in discovery, stored matching and actual mocked daily delivery.
- Telemetry replay, reporting windows, private-content exclusion and cloud health summaries.
- Additive schema-5-to-6 migration with existing jobs and identity links preserved.

The Tests Actions workflow also runs PostgreSQL integration with a disposable
PostgreSQL 17 service, including migration, feedback and concurrent refresh claims.
Those tests skip locally unless `TEST_POSTGRES_URL` points to a dedicated
`localhost/jobscout_test` database. Never supply a production database to tests.

## Optional live smoke test without Telegram

Use a separate test database and a fictional/example profile. These commands make
one public Remotive request on the first normal discovery; they do not use Brave
or send messages. Run from a fresh shell so the test database selection does not
replace your normal shell's configuration:

```sh
source .venv/bin/activate
job-scout --local-db data/m6-smoke.db db-init
# If this test database already exists at an older revision, use db-migrate instead.
cat > config/m6-smoke.yaml <<'YAML'
providers:
  arbeitnow: {enabled: false}
  himalayas: {enabled: false}
  brave: {enabled: false}
  remotive: {enabled: true, limit: 100, refresh_hours: 24}
registration: {enabled: false}
YAML
job-scout --local-db data/m6-smoke.db discover-jobs --config config/m6-smoke.yaml --profile config/profile-tracks.example.yaml
job-scout --local-db data/m6-smoke.db discovery-health --days 7
job-scout --local-db data/m6-smoke.db jobs
```

Expected: a saved report, one Remotive request, attributed listing links and zero
ATS validation requests. Job/match counts depend on current postings and the example
profile; zero relevant matches is possible. The health report shows counters such
as `requests`, `inserted`, `qualifying_observations` and `next_due`.

Repeat the discovery command: it should report `cached`, zero requests and no new
rows. Add `--dry-run`: it should explain that Remotive refresh is skipped and show
only recent cached matches, without any database changes. Do not delete cooldowns
or rotate test databases to force repeated fetches; respect provider request limits.

Copy a full `job_key` from `jobs` or a matching result and test feedback:

```sh
job-scout --local-db data/m6-smoke.db feedback irrelevant JOB_KEY
job-scout --local-db data/m6-smoke.db match --profile config/profile-tracks.example.yaml
job-scout --local-db data/m6-smoke.db match --profile config/profile-tracks.example.yaml --include-dismissed
job-scout --local-db data/m6-smoke.db feedback clear JOB_KEY
```

A previously matching dismissed job should disappear from normal matching and
reappear after clearing. A job below the score threshold or excluded by eligibility
will not become a match merely because its feedback is cleared. For scope testing,
use a separate profile with `feedback_scope: smoke` and commands with `--scope smoke`.
Exit the test shell when finished to restore your usual database environment.

## Deployment checks

Follow [the GitHub Actions guide](deployment.md). A green offline suite does not
prove live API availability, database reachability or real Telegram delivery.
Verify **initialize/migrate → preview → send → status → health**, then enable the
schedule. A repeat ordinary send should return `already_sent`; **health** should
show aggregate discovery activity after normal collection. Keep production profiles
and tokens in Actions secrets, and inspect full previews privately rather than in
public logs.
