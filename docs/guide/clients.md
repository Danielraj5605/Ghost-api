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

## Credentials

Endpoints that need a key or token get a constructor argument, read from an environment
variable by default (the client's README lists them). Ghost API never writes a secret into
generated code.
