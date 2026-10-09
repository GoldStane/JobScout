# Collection fixtures

These are synthetic, sanitized postings with reserved example.org URLs, shaped
according to the official public API documentation. They are not live vacancies
or verified employer identifiers.

- Greenhouse: https://docs.greenhouse.io/job-board.html (list jobs, content=true).
- Lever: https://github.com/lever/postings-api (JSON postings and pagination).

Fixtures cover encoded HTML, source timestamps, categories, list sections, and
application URLs. Edge cases are produced from these fixtures in tests. Tests
use httpx.MockTransport; no employer API or Telegram is contacted.
