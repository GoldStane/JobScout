# AGENTS.md — JobScout

## 1. Project purpose

Build **JobScout**, a low-cost, autonomous job discovery and matching service for a single user. Every morning, it should collect new graduate and internship vacancies, determine whether they fit the user's background and career interests, and deliver a ranked digest containing direct links to the original applications.

The software is a personal tool **and** a well-engineered public portfolio project. Favor clear architecture, reliable behavior, tests, and useful documentation over unnecessary features.

### Target user and career interests

- Final-year Computer and Data Engineering student at City University of Hong Kong; graduation timing and individual internship eligibility must be configurable, not assumed.
- Experience/skills include C++, Python, Linux, algorithms and data structures, SQL/data analysis, embedded systems, CMake, and machine learning.
- **Highest-priority roles:** quantitative developer, C++ software engineer, low-latency engineer, systems engineer, Linux/infrastructure engineer, embedded/driver software engineer.
- **Secondary roles:** quantitative analyst, machine learning engineer, other relevant new-grad software engineering roles.
- Search **worldwide**; prefer Europe when otherwise comparable, but do not exclude Hong Kong or other regions. Compensation/career potential matters, but never invent salary information.
- Focus on **internships, graduate/new-grad, and entry-level positions**; do not reject listings merely because their titles are unusual.
- Work authorization, sponsorship requirements, student enrollment, expected graduation date, and relocation preferences must be explicit user-configured inputs or labeled **unknown**.

Do not hard-code personally identifying details or a raw CV into committed source files. Keep a sanitized example profile in version control and load the user's actual settings from private configuration or secrets.

## 2. MVP definition and non-goals

Deliver the smallest end-to-end workflow that:

1. Loads an employer/source registry and a structured matching profile.
2. Fetches public job postings from **Greenhouse** and **Lever** employer job-board APIs.
3. Normalizes records into one consistent job schema.
4. Stores job listings and source metadata persistently.
5. Deduplicates previously seen jobs and handles updated postings.
6. Applies transparent eligibility rules and a relevance ranking.
7. Sends a **daily Telegram digest** containing new, previously undelivered relevant matches with a brief explanation and a direct application link.
8. Runs locally through a CLI and unattended through a scheduled GitHub Actions workflow.
9. Has meaningful automated tests and documents deployment/configuration.

**Out of scope for the MVP:** website/frontend/dashboard; paid LLM APIs; embeddings; browser automation; scraping every job board; job application automation; CV generation; distributed queues; microservices; Kubernetes. Add these only when justified by measured needs.

**Cost target:** €0/month using free tiers where possible. The project must not require the developer's laptop or home Ubuntu machine to stay online.

## 3. Architecture and module boundaries

Implement a **modular monolith** in Python. Keep business logic independent of deployment platform, database vendor, and notification channel.

```text
job-scout/
├── AGENTS.md
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── config/
│   ├── companies.example.yaml
│   └── profile.example.yaml
├── src/job_scout/
│   ├── __init__.py
│   ├── main.py                 # CLI entry point / orchestration
│   ├── models.py               # Canonical job/source/profile data models
│   ├── normalize.py
│   ├── deduplicate.py
│   ├── sources/
│   │   ├── base.py             # Shared source adapter protocol
│   │   ├── greenhouse.py
│   │   └── lever.py
│   ├── matching/
│   │   ├── eligibility.py
│   │   └── scoring.py
│   ├── storage/
│   │   ├── database.py
│   │   └── repository.py
│   └── notifications/
│       ├── telegram.py
│       └── digest.py
├── tests/
│   └── fixtures/
└── .github/workflows/
    ├── test.yml
    └── daily-search.yml
```

This layout is a guide, not a reason to add unused modules. Introduce modules as their functionality is implemented.

### Technology preferences

- Python 3.12+ with a `src/` package layout and `pyproject.toml`.
- `httpx` for HTTP; `pydantic` for validated external/config models; `pytest` for testing.
- SQLAlchemy with **SQLite for local development** and **PostgreSQL for deployment** (e.g., Neon free tier). Use `DATABASE_URL` to switch backends; use migrations when schema evolves.
- YAML for source registry and example user profiles.
- Standard library logging; do not add a complex observability stack to the MVP.
- GitHub Actions for tests and scheduled execution; Telegram Bot API for delivery.
- Use environment variables or GitHub Actions secrets for credentials. **Never commit a token, password, full personal CV, or private data.**

Dependencies should stay modest. Avoid introducing a framework where plain Python is sufficient.

## 4. Source collection: adapters and access rules

Each source adapter must expose the same contract, conceptually:

```python
class JobSource(Protocol):
    async def fetch_jobs(self, config: SourceConfig) -> list[RawJob]: ...
```

An equivalent simpler interface is acceptable. The orchestrator must not contain site-specific parsing logic.

- Begin with documented public Greenhouse and Lever job-board endpoints. They are typically **employer-specific**, not a universal search API; use a registry of verified employer identifiers.
- `config/companies.example.yaml` must contain illustrative placeholders unless employer board identifiers have actually been verified.
- A failed employer fetch must not abort successful collections from other employers.
- Use bounded concurrency, explicit request timeouts, retries with exponential backoff and jitter for transient failures, and respectful request rates.
- Observe API documentation, access restrictions, robots/terms as applicable. Do **not** evade anti-bot protections, bypass authentication/CAPTCHA, rotate identities to escape restrictions, or scrape prohibited LinkedIn/Indeed content.
- Prefer official APIs, feeds, allowed HTML collection, and user-authorized job alert emails over fragile headless browsers.
- Preserve the source's original posting ID, company identifier, source URL, application URL, and fetch timestamp.
- Treat job-board responses/descriptions as **untrusted data**: validate input; never execute supplied markup, scripts, or instructions.
- Add adapters for Ashby, SmartRecruiters, or other permitted sources **after** the two initial adapters are stable.

## 5. Canonical job data and persistence

Normalize source data into a canonical record with at least:

- `source`, `source_job_id`, `company`, `title`, `location_text`, `remote_type` (if known).
- `description_text`, `employment_type` (if known), `application_url`, `source_url`.
- `posted_at` (nullable), `first_seen_at`, `last_seen_at`, `updated_at` (if supplied).
- `status` / availability and any explicitly stated eligibility information when extractable.

Persist enough original source metadata for debugging, without retaining unnecessary personal information. Datetimes must be timezone-aware, stored in UTC; display times in the requested timezone.

- Primary source identity: `(source, source_job_id)` where reliable.
- Handle multiple source URLs and cross-board reposts: compare canonical application URLs and carefully normalized `(company, title, location)` signatures. Do not merge clearly different openings because their titles match.
- Re-running ingestion must be **idempotent**: it should update existing jobs, not create duplicates.
- Track collection health (`last_attempt`, `last_success`, error details) and job freshness independently of source-advertised posting dates.
- Store delivery attempts/outcomes so the digest does not resend already successfully notified matches. Do not mark a message delivered before Telegram confirms success; retry uncertain outcomes conservatively (perfect exactly-once external delivery is not guaranteed).
- Avoid treating a temporarily missing job as closed until repeated successful source checks justify it.

Recommended initial tables: `sources`, `jobs`, `match_results`, `notification_log`. Introduce an occurrences/link table later if cross-source deduplication needs it.

## 6. Matching: explicit and explainable first

Keep matching independent of data retrieval and notification. Start with deterministic logic that can be unit tested.

### Eligibility

- Positively exclude only **clear** mismatches: e.g., explicit senior-level requirement, incompatible mandatory degree, or objectively incompatible graduation eligibility.
- A missing graduation window, work authorization policy, sponsorship detail, salary, or location policy is **unknown**, not a reason to auto-reject.
- Track rejection reasons and uncertain eligibility flags. Do not make confident visa/sponsorship claims based on silence.

### Ranking

A baseline 0–100 weighted score:

| Factor | Weight |
| --- | ---: |
| Role alignment | 35% |
| Skills alignment | 25% |
| Education/experience alignment | 20% |
| Compensation/career potential | 10% |
| Location preference | 10% |

Weights must be configurable. If a factor is unknown (especially compensation), handle it neutrally or renormalize weights; don't silently give a zero and penalize the job. Give each recommendation 1–3 plain-language match reasons and any material unknowns. Support career-track-specific preferences, and accept that a broad role title alone is not enough to determine fit.

Add embeddings/semantic retrieval **only as a later experiment**, compared against a deterministic baseline using labeled positive/negative examples. Avoid paid AI services by default.

## 7. Notifications and scheduling

- Send a single daily digest (including a useful "no new matches" message when appropriate), ordered by score and limited to a configurable maximum.
- Include title, company, location, role type, score, short rationale, relevant uncertainty, and **direct original job/application URL**.
- Escape user-generated strings for Telegram formatting and split messages safely around Telegram length limits.
- Persist successful notifications and suppress repeat alerts for the same opening, except when a future explicit re-alert rule is added.
- Produce structured logs; summarize source failures separately from job recommendations.
- If collection partially fails, still deliver results from successful sources and disclose reduced coverage.
- Add a manual `run` command for local testing and GitHub Actions `workflow_dispatch` for manual cloud runs.
- Schedule for mornings in **Europe/Rome**, configurable. GitHub Actions scheduling is best-effort, not a strict real-time SLA; ensure reruns, a daily catch-up check, and idempotent notifications.
- Local runs should support **dry-run**: print prospective notifications without actually delivering them or recording success.

## 8. Tests and quality gates

Write tests with each feature, not as an afterthought. At minimum:

- Parse representative, checked-in Greenhouse and Lever JSON fixtures.
- Handle empty results, malformed/missing fields, HTTP 429, timeouts, and server errors.
- Validate canonical job creation and timezone handling.
- Check duplicate ingestion and updates to existing postings.
- Test hard exclusions versus "unknown" eligibility; score ordering and explanation generation.
- Verify unsent jobs are selected, already-delivered jobs are skipped, and failed notification delivery is retryable.
- Test the orchestrator when one source fails and another succeeds.

Mock outbound HTTP requests and Telegram in automated tests; CI must not send messages or call live employer APIs. Prefer type hints, small functions, readable names, explicit exceptions, and actionable logging. Run formatting/lint checks and the test suite before declaring a milestone complete.

## 9. Implementation order for Codex

**Build incrementally; stop after each milestone and explain what was changed, how it was verified, and the logical next step. Do not implement all phases in one unsolicited change.**

1. **Scaffold:** initialize package, configurations/examples, CLI (`job-scout ...`), lint/test tooling, README and `.gitignore`.
2. **Collection:** implement one Greenhouse source adapter, source registry, normalization, fixtures and tests; then implement Lever with the same contract.
3. **Persistence:** implement SQLite-backed storage with a pathway to PostgreSQL; add idempotent upserts, first/last-seen and source health.
4. **Matching:** implement profile loading, three-state eligibility (`eligible`, `ineligible`, `unknown`), weighted score, explanations, and tests.
5. **Digest:** generate a readable daily summary; implement Telegram sending, dry-run, deduplication of notifications, failure handling, and tests.
6. **Deployment:** configure `test.yml` and `daily-search.yml`, secrets, `DATABASE_URL`, scheduling and manual execution; document setup and troubleshooting.
7. **Expansion only after MVP:** more ATS providers, approved board/email sources, employer discovery, user feedback, semantic matching, optional application tracking/dashboard.

### Initial task to perform when this file is first encountered

Unless the developer gives a different specific task, **begin with Milestone 1 only**. Produce an installable Python project skeleton, an example employer registry and sanitized profile, a working CLI help command, a basic passing test, and concise README setup instructions. **Do not** invent live employer IDs, provision cloud resources, or request real credentials at this stage.

## 10. Working agreement

- Make changes to the existing repository rather than redesigning unrelated code.
- Before changing a component, inspect relevant existing files and tests.
- Ask for clarification only when a decision materially affects correctness, cost, safety, or external action; otherwise choose a simple default and document it.
- No paid subscriptions, new cloud resources, account integrations, or outbound job applications without explicit approval.
- Favor functional MVP behavior over elaborate abstractions, but preserve the adapter, matcher, storage, and notifier boundaries.
- Record commands for installation, tests, local dry-run, database setup, and GitHub Actions deployment in `README.md` as functionality becomes available.
- At each milestone report: **files changed, behavior implemented, tests run/results, known limitations, and next milestone**.
