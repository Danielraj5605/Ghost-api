# Ghost API evaluation

Max 20 agent steps and 30 LLM calls per site.

| Site | Outcome | Recall | Precision | Pass rate | LLM calls | Time | Peak memory | Invented | Forbidden generated |
|---|---|---|---|---|---|---|---|---|---|
| demo-shop | goal-covered | 100% | 100% | 100% | 6 | 36s | 415 MB | - | - |
| quotes | goal-covered | 100% | 100% | 100% | 2 | 18s | 423 MB | - | - |
| hn-algolia | goal-covered | 100% | 100% | 100% | 10 | 259s | 473 MB | - | - |
| books-no-api | finished | n/a | 100% | n/a | 1 | 9s | 452 MB | - | - |

Found endpoints:

- **demo-shop:** `GET /api/categories`, `GET /api/products`, `GET /api/products/{}`, `GET /api/products/{}/reviews`, `GET /api/search`
- **quotes:** `GET /api/quotes`
- **hn-algolia:** `POST uj5wyc0l7x-dsn.algolia.net/1/indexes/{}/query`
- **books-no-api:** none

## How these numbers were produced

Run on 2026-10-05 with free tiers only, on a Windows laptop with 8 GB of RAM. Gemini was
over its daily quota or overloaded for much of the time, so many answers came from Groq.

- **First run:** demo-shop and quotes scored as above. hn-algolia and books-no-api stopped
  with `llm-unavailable`: two of the four providers had retired free models (HTTP 404), and
  the gateway did not wait for a rate limit that would have cleared in seconds.
- **Fixes:** per-model routing with fallback models, waiting that ignores dead routes, and
  a scoring fix (an endpoint seen with one value was counted as invented).
- **Re-run** of hn-algolia and books-no-api after the fixes: the rows above.
- hn-algolia's 259 s was mostly the gateway retrying an overloaded Gemini model; it now
  moves to another model at once (fixed after this run, not re-measured).
