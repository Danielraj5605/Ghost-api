# Gallery: real runs

Real runs on public sites that welcome this kind of practice (their `robots.txt` allows it),
with the AI agent on small budgets (at most 8 steps and 12 LLM calls), October 2026.
Each line is what `ghost-api run URL --goal "..." --json` reported.

| Site and goal | Result | Endpoints (all verified by direct calls) | LLM calls | Time |
|---|---|---|---|---|
| [quotes.toscrape.com/scroll](https://quotes.toscrape.com/scroll), "list quotes with their authors and tags" | goal covered | `GET /api/quotes`: quotes with author and tags, page by page | 2 | 17 s |
| [scrapethissite.com AJAX page](https://www.scrapethissite.com/pages/ajax-javascript/), "list Oscar-winning films for each year" | goal covered | `GET /pages/ajax-javascript/?year=`: title, year, awards, nominations, best picture | 6 | 31 s |
| [hn.algolia.com](https://hn.algolia.com/), "search Hacker News stories and comments" | goal covered | `POST uj5wyc0l7x-dsn.algolia.net/1/indexes/{index}/query`: a third-party search API, paging by page, key in the URL found to be required | 7 | 77 s |
| [webscraper.io AJAX shop](https://webscraper.io/test-sites/e-commerce/ajax), "list products by category with pages" | no JSON API for products | only a chat widget's settings (a third party on a CDN) | 10 | 46 s |

## What these show

- **Hidden APIs on other hosts are found.** Hacker News search runs on Algolia; the client
  calls Algolia directly, and the probe found which URL parameter is the required key.
- **Paging is detected and proven**, not guessed: page 2 is requested and compared.
- **"No API" is a real answer.** The webscraper.io shop loads its product lists as HTML
  fragments, so there is no JSON API to map for that goal. The one JSON reply on the page
  was a chat widget's settings; Ghost API mapped it, and the report marks it as a third
  party, but it is not what the goal asked for. Read the report's endpoint list before
  using a client.
- **Small budgets are enough** for typical sites: 2 to 10 LLM calls.

## Reproduce

```bash
ghost-api run https://www.scrapethissite.com/pages/ajax-javascript/ \
  --goal "list Oscar-winning films for each year" --max-steps 8 --max-llm-calls 12
```

Or in Claude Code with `ghost-api integrate claude-code`:
`/ghost-explore https://hn.algolia.com/ search Hacker News stories and comments`.
