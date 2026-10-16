# From Python

Everything the command does is also a function, with the same rules:

```python
from ghost_api.api import discover, fetch, diff, operations

result = discover("https://quotes.toscrape.com/scroll", "list quotes with their authors")
print(result.ok, result.status, [e["path"] for e in result.endpoints])
print(result.client_files, result.report_html, result.network)

for op in operations("quotes.toscrape.com/scroll"):
    print(op["name"], op["inputs"])

rows = fetch("quotes.toscrape.com/scroll", "get_quotes", pages=3)
changes = diff("quotes.toscrape.com/scroll", live=True)
```

| Function | Does |
|---|---|
| `discover(url, goal, use_agent=True, lang="python", on_event=None, ...)` | Explore, test, write the client and report; returns a `RunResult` |
| `refresh(site, use_llm=True, ...)` | Re-check a known site from memory |
| `generate(site, lang="python")` | Rewrite the client from stored data (no browser, no LLM) |
| `operations(site)` | What can be fetched, with example inputs |
| `fetch(site, operation, inputs=None, pages=5, max_rows=1000)` | Rows, following pages |
| `diff(site, live=False)` | Changes since the last client version |
| `sites()` | Sites explored so far |

Errors are exceptions: `NotAllowed` (robots.txt), `NoLLMKey`, `NothingRemembered`, and
`GhostApiError` for anything the caller can fix (an unknown site or operation). `on_event`
receives every live event (steps, requests, endpoints) as the run goes.
