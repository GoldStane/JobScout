# Collection fixtures

These are synthetic, sanitized postings with reserved example.org URLs, shaped
according to the official public API documentation. They are not live vacancies
or verified employer identifiers.

- Greenhouse: https://docs.greenhouse.io/job-board.html (list jobs, content=true).
- Lever: https://github.com/lever/postings-api (JSON postings and pagination).

Fixtures cover encoded HTML, source timestamps, categories, list sections, and
application URLs. Edge cases are produced from these fixtures in tests. Tests
use httpx.MockTransport; no employer API or Telegram is contacted.

`discovery.json` contains synthetic minimal postings for Greenhouse, Lever, and
Ashby with matching board URL identities. The `example` tokens are not live verified
employers. Discovery tests never fetch these hosts.

`arbeitnow.json` is synthetic, with fictional employers/descriptions. Its envelope,
posting fields and pagination structure follow a bounded official public API
sample checked on 2026-10-11. No live employer content is copied into this fixture.
Tests use MockTransport and never fetch live feed/ATS URLs or send Telegram.

`himalayas.json` is synthetic, shaped after the official remote-jobs API reference
and two bounded response-shape samples checked on 2026-10-11. It includes country
and timezone restrictions, stated pay and publication/expiry timestamps. Tests
also cover documented ISO/millisecond dates and country objects. No live posting
content is retained; all HTTP and delivery in the acceptance tests are mocked.

`brave.json` is a synthetic search envelope based on the official Web Search API
and pagination documentation checked on 2026-10-11. Its sentinel snippets, titles,
query echo and tracking parameters must never enter persistent storage or digests.
Publisher content comes from separate mocked `discovery.json` responses. No real
Brave account, key, billed API request or search-result payload was used.
