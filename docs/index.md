# Ghost API

**Give Ghost API a website and say what data you want.** It opens the site in a real
browser, watches the hidden requests the site makes to its own server, works out how that
hidden API behaves, and writes a typed, tested Python or TypeScript client for it.

```bash
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors"
```

```python
from quotes_toscrape_client import QuotesToscrapeClient

with QuotesToscrapeClient() as api:
    for page in api.iter_get_quotes(max_pages=3):
        print(page.quotes[0].author.name)
```

## What you get

- **A client:** sync and async Python (pydantic models) or TypeScript, with typed errors,
  paging, rate limiting and retries, as loose files or an installable package.
- **Proof it works:** every endpoint is tested with direct calls, and the client's own tests
  run against the live API before you see it.
- **A report and an OpenAPI spec** (imports into Postman, Bruno, Insomnia).
- **Data without code:** CSV / Excel / JSON downloads, watches with alerts, a local REST API,
  an MCP server for AI assistants, and an offline mock of the API.
- **Change detection:** client versions, `ghost-api diff`, and a GitHub Action that opens a
  pull request when the site's API changes.

## Built to be polite and safe

Ghost API follows `robots.txt`, rate-limits itself, never gets past bot protection, never
calls endpoints that change data, hides personal data before storing anything, and never
lets a website steer it into your local network. See [Security and data](security.md).

[Get started](getting-started.md){ .md-button .md-button--primary }
[Command reference](cli.md){ .md-button }
