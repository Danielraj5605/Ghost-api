# Using the client

Each run writes, in `output/<site>/`:

| File | What it is |
|---|---|
| `<name>_client.py`, `models.py` | The client (sync and async classes) and pydantic models |
| `test_<name>_client.py` | Live tests, already run once against the real API |
| `package/python/`, `package/typescript/` | The same client as an installable package, with a README, curl examples and a CHANGELOG |
| `openapi.yaml` | OpenAPI 3.1 (imports into Postman, Bruno, Insomnia) |
| `report.md`, `report.html` | Endpoints, evidence, the agent's reasoning, the network summary |

## Python

```python
from quotes_toscrape_client import QuotesToscrapeClient, QuotesToscrapeNotFoundError

with QuotesToscrapeClient() as api:
    page = api.get_quotes()
```

```python
from quotes_toscrape_client import AsyncQuotesToscrapeClient

async with AsyncQuotesToscrapeClient() as api:
    async for page in api.iter_get_quotes(max_pages=3):
        ...
```

Errors are typed: catch the base error for any failure, or `...AuthError` (401/403),
`...NotFoundError` (404), `...RateLimitError` (429) or `...ServerError` (5xx). Pass
`http_client=httpx.Client(event_hooks=...)` to log or change requests. Generated code passes
`mypy --strict`.

## TypeScript

```bash
ghost-api run https://quotes.toscrape.com/scroll -g "list quotes" --lang typescript
npm install ./output/quotes.toscrape.com/scroll/package/typescript
```

The client is made for Node 18+. In a web page, call it from your own server: browsers
block reading other sites' replies without CORS and don't let code set the User-Agent.

## Tests that run anywhere

Each client comes with `cassettes.json`: replies recorded while exploring (secrets redacted,
personal data hidden). The generated tests replay them by default, so they pass offline, in
CI and when the site is down, and always give the same answer:

```bash
pytest test_quotes_toscrape_client.py                    # offline, from the recordings
GHOST_API_LIVE=1 pytest test_quotes_toscrape_client.py   # against the real API
node --test quotes_toscrape_client.test.ts               # TypeScript, same rule
```

Ghost API's own verification always tests live. To check the real API on a schedule
without the browser, use `ghost-api diff SITE --live` (or the GitHub Action).

## In code you already have

```bash
ghost-api adopt .                      # hand-written calls to hidden APIs, by host
ghost-api adopt . --json               # the same, for an assistant to apply the edits
```

`adopt` scans Python and JavaScript/TypeScript files for HTTP calls with a hard-coded URL
(`fetch`, `axios`, `requests`, `httpx`...). For hosts Ghost API has explored it shows the
client method that replaces each call; for the others, how to map them. Nothing is edited.

## When there are docs, check them

```bash
ghost-api verify-spec openapi.yaml example.com
```

Compares a published OpenAPI spec with what the site really does: endpoints the site uses
but the spec doesn't mention, documented endpoints never seen, and reply fields that differ.

## Other languages

Python and TypeScript clients are generated and tuned by Ghost API. For Go, Java, C#,
Rust and others, feed the verified `openapi.yaml` to a standard generator, e.g.
`npx @openapitools/openapi-generator-cli generate -i openapi.yaml -g go -o client-go`.

## Understanding a site quickly

`ghost-api explain example.com` prints a plain-language summary: each endpoint, what it
returns, how it pages, whether it needs a login, and the client method for it.

## Credentials

Endpoints that need a key or token get a constructor argument, read from an environment
variable by default (the client's README lists them). Ghost API never writes a secret into
generated code.
