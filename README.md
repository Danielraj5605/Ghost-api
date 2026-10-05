# Ghost API

**An AI agent that turns any website into a typed, tested API client.**

Give Ghost API a website and a plain-English goal. It opens the site in a real browser, watches the hidden requests the site makes to its own server, works out how that hidden API behaves, and writes a clean Python client library for it, with tests.

> **Status:** In development. Stages 0–2 are done: `ghost-api run` opens a site in a real browser, captures its filtered, redacted API traffic, and turns it into an endpoint map (URL templates, parameters and JSON schemas) stored in SQLite. The AI agent, code generation and memory come in later stages, so parts of this README still describe the intended design. See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the stage-by-stage plan.

---

## Table of contents

- [The simple explanation](#the-simple-explanation)
- [The problem](#the-problem)
- [What Ghost API does](#what-ghost-api-does)
- [Example](#example)
- [How it works](#how-it-works)
- [Why it's an agent, not a script](#why-its-an-agent-not-a-script)
- [Memory](#memory)
- [Features](#features)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Usage](#usage)
- [Output](#output)
- [Project structure](#project-structure)
- [Running on free LLM tiers](#running-on-free-llm-tiers)
- [Responsible use](#responsible-use)
- [Limitations](#limitations)
- [Roadmap](#roadmap)

---

## The simple explanation

Picture a restaurant with **no menu**. You can eat there, but only by pointing at other people's plates. Behind the scenes, every time someone orders, the waiter hands the kitchen a neat order slip:

> "Table 4: 1x Pasta, no cheese, large."

The kitchen understands those slips perfectly. You just never see them.

**Ghost API sits in the restaurant, watches every order slip going to the kitchen, and writes the menu for you.** After that you can order directly, without pointing at plates.

| In the analogy | In real life |
|---|---|
| The restaurant | Any website (a bookstore, a weather site, a job board) |
| Pointing at plates | Copying data by hand, or fragile "scraper" programs that break when the design changes |
| The order slips | The hidden requests a website makes to its own server |
| The menu | A small code library that gets the data directly, cleanly and reliably |

**What makes it "AI":** Ghost API doesn't just record. Like a curious person, it:

1. **Explores** the site by clicking, searching and scrolling
2. **Notices patterns**, such as "every time I open a book, the site asks for `/books/<number>`"
3. **Tests its guesses**, such as "if I ask for page 2, do I get different results?"
4. **Fixes its mistakes** when a guess turns out wrong
5. **Remembers** what it learned, so next time is faster

---

## The problem

Almost every modern website loads its data through a **hidden internal API**. When you open a product page, your browser quietly calls something like:

```
GET https://shop.example.com/api/v2/products/123?include=reviews
-> { "id": 123, "name": "Blue Shoes", "price": 59.99, "reviews": [...] }
```

Developers often want that data in their own code. Their options today are poor:

| Approach | Why it's poor |
|---|---|
| Scrape the HTML | Breaks whenever the page layout changes, and is slow |
| Reverse-engineer by hand in browser DevTools | Takes hours: clicking around, copying requests, guessing what each field means |
| Tools like mitmproxy or Postman | Record raw traffic, but don't understand it or write any code |

No existing tool both **discovers** these hidden APIs and **produces working code** for them.

---

## What Ghost API does

| Input | Output |
|---|---|
| A website URL | A typed Python client library (`httpx` + `pydantic`) |
| A plain-English goal ("search books and get details") | A pytest test suite that runs against the live site |
| | An OpenAPI 3 spec |
| | A report showing what was found, how confident it is, and the agent's reasoning |

---

## Example

```
$ ghost-api run https://books.example.com --goal "search books and get details"

Exploring site...
   [found] GET  /api/search?q={query}&page={n}
   [found] GET  /api/books/{id}
   [found] GET  /api/books/{id}/reviews
   [test]  Pagination: trying page=2... confirmed
   [test]  Auth: endpoints work without login - OK
Inferring schemas from 14 responses...
Generating Python client...
Running tests... 3/3 passed

Done -> output/books.example.com/
Usage: 31 LLM calls - 6m 12s - $0.00
```

The generated client:

```python
from books_client import BooksClient

client = BooksClient()
results = client.search(query="dune", page=1)  # -> list[BookSummary]
book = client.get_book(results[0].id)  # -> Book (typed)
print(book.title, book.rating)
```

---

## How it works

```
                         +---------------------+
                         |       START         |
                         |  URL + goal + lang  |
                         +----------+----------+
                                    v
                         +---------------------+
                         | 0. PRE-CHECKS       |
                         | robots.txt, memory  |---- known site? ---> load past
                         | lookup, budgets     |                      knowledge
                         +----------+----------+
                                    v
          +----------------------------------------------------+
          |                  AGENT LOOP                        |
          |   1. OBSERVE  page snapshot + captured traffic     |
          |   2. DECIDE   LLM picks: explore / test / ask /    |
          |               finish                               |
          |   3. ACT      run a tool (click, type, call API)   |
          |   4. LEARN    update endpoint map + schemas        |
          |   goal covered? budget left? --no--> back to 1     |
          +----------+-----------------------------------------+
                     | yes
                     v
          +---------------------+
          | 5. GENERATE CODE    | client + models + tests
          +----------+----------+
                     v
          +---------------------+      fail      +--------------+
          | 6. VERIFY (tests)   |--------------->| 7. DIAGNOSE  |--> back to
          +----------+----------+                | & REPLAN     |    agent loop
                     | pass                      +--------------+    (max 3x)
                     v
          +---------------------+
          | 8. SAVE MEMORY +    |
          |    WRITE REPORT     |
          +---------------------+
```

### Step by step

**0. Pre-checks.** It reads `robots.txt`, looks the domain up in memory, and sets budgets (maximum LLM calls, steps and minutes).

**1. Observe.** The agent gets a compact view of the page: the clickable elements and inputs with short IDs (such as `[12] button "Search"`), a summary of the new network requests, the endpoints found so far, and which parts of the goal are still uncovered.

**2. Decide.** The LLM picks **one** action and explains why:

```json
{
  "thought": "I found the book list endpoint but not book details. Clicking a book should trigger it.",
  "action": "click",
  "args": { "element_id": 23 }
}
```

**3. Act.** The chosen tool runs: click, type, scroll, navigate, call an endpoint directly, ask the user, or finish.

**4. Learn.** Ordinary code, not the LLM, processes the traffic:
- Filters out noise (images, CSS, fonts, analytics, ads)
- Groups URLs into templates: `/api/books/123` and `/api/books/456` become `/api/books/{id}`
- Infers JSON schemas from real responses and merges them across samples
- Classifies query parameters as required, optional or constant

**5. Generate.** The LLM gives endpoints and fields readable names. Jinja2 templates produce the client, pydantic models, tests and an OpenAPI spec.

**6. Verify.** The generated tests run against the live site and check that each response matches its schema.

**7. Diagnose and replan.** If a test fails, the agent works out why and recovers:

| Failure | Likely cause | Agent's response |
|---|---|---|
| 401 or 403 | Missing cookie, token or header | Retry with the browser's cookies, then find which header is required |
| Schema mismatch | Too few samples | Explore more, then widen the schema |
| 404 | Wrong URL template | Re-check how the URL was grouped |
| 429 | Rate limited | Back off and slow down |
| Timeout | Slow endpoint | Retry, then mark as unstable |

**8. Save and report.** It saves what it learned to memory and writes `report.md`.

---

## Why it's an agent, not a script

A fixed script can record traffic. Ghost API has to **make decisions**:

| Decision | Example |
|---|---|
| Where to explore next | "I've found search but not reviews. Click the reviews tab." |
| When it has seen enough | "Three similar responses. The schema is stable, so stop." |
| Which guesses to test | "This looks like cursor pagination, not page numbers. Test it." |
| How to recover from failure | "Direct call returned 403. Retry with the browser's cookies." |
| When to ask you | "This needs a login. Please log in in the browser window, then press Enter." |

**Design principle:** the LLM decides and names things. Deterministic code does the heavy lifting: capture, grouping, schema inference and code generation. This keeps it cheap, fast, and reliable even with small free models.

---

## Memory

Ghost API improves with use:

- **Per-site memory:** endpoints, schemas, auth style and pagination style for each domain. A second run re-verifies instead of re-exploring.
- **Lessons learned:** notes such as "this site needs the X-CSRF header" are fed back into the prompt on later runs.
- **LLM cache:** identical prompts return cached answers, which saves free-tier quota.

---

## Features

**MVP**
- One-command run: URL plus goal in, client out
- Real-browser exploration with Playwright
- Network capture with noise filtering and secret redaction
- URL templating and JSON schema inference
- Hypothesis testing (pagination, required headers)
- Python client with pydantic models
- Auto-generated and auto-run tests
- Diagnose-and-replan loop
- Per-site memory and an LLM response cache
- Live terminal progress and a final `report.md`

**Later**
- OpenAPI export (planned for the MVP if time allows)
- TypeScript client generation
- Change detection: update a client when the site changes
- GraphQL support

---

## Tech stack

| Part | Tool |
|---|---|
| Language | Python 3.11+ |
| Browser automation | Playwright (Chromium) |
| LLM access | LiteLLM, with Gemini (free tier) as the main model and Groq / OpenRouter as fallbacks |
| HTTP | httpx |
| Data models | pydantic |
| Schema inference | genson |
| Code generation | Jinja2 |
| CLI and terminal UI | Typer + Rich |
| Tests | pytest |
| Storage | SQLite |

Runs on an ordinary laptop: 8 GB RAM, no GPU.

---

## Getting started

> These steps describe the intended setup once the project is built.

**Requirements:** Python 3.11+, Git, and a free API key from [Google AI Studio](https://aistudio.google.com/) (Groq is optional, as a fallback).

```bash
git clone <your-repo-url> ghost-api
cd ghost-api

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
playwright install chromium

cp .env.example .env
# then add your keys to .env
```

`.env`:

```
GEMINI_API_KEY=your_key_here
GROQ_API_KEY=optional_fallback_key
OPENROUTER_API_KEY=optional_second_fallback
```

---

## Usage

```bash
# Basic run
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors and tags"

# Watch the browser while it works
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes" --headed

# Tighter budget
ghost-api run https://example.com --goal "search products" --max-llm-calls 30 --max-steps 25

# Re-run against a known site (uses memory)
ghost-api refresh quotes.toscrape.com

# Show what's stored for a site
ghost-api inspect quotes.toscrape.com
```

| Option | Default | Meaning |
|---|---|---|
| `--goal` | required | What you want the client to do |
| `--lang` | `python` | Output language |
| `--headed` | off | Show the browser window |
| `--max-steps` | 40 | Maximum agent actions |
| `--max-llm-calls` | 50 | Maximum LLM calls |
| `--max-minutes` | 15 | Time limit |
| `--rate-limit` | 1.0 | Maximum requests per second to the target site |
| `--ignore-robots` | off | Skip the robots.txt check (use responsibly) |

---

## Output

```
output/books.example.com/
|-- books_client.py     # The client library
|-- models.py           # Pydantic data models
|-- test_client.py      # Generated tests
|-- openapi.yaml        # OpenAPI spec
+-- report.md           # What was found, confidence, reasoning log
```

Sample `report.md`:

```
## Endpoints (3 verified, 1 unverified)
| Endpoint                    | Status     | Confidence | Samples |
|-----------------------------|------------|------------|---------|
| GET /api/books              | verified   | High       | 6       |
| GET /api/books/{id}         | verified   | High       | 4       |
| GET /api/search?q=          | verified   | Medium     | 3       |
| GET /api/books/{id}/related | unverified | Low        | 1       |

## Usage: 31 LLM calls - 42k tokens - 6m 12s - $0.00
```

---

## Project structure

```
ghost-api/
|-- ghost_api/
|   |-- cli.py              # Command-line entry point
|   |-- config.py           # Settings and budgets
|   |-- agent/
|   |   |-- orchestrator.py # Main loop, budget, replanning
|   |   |-- prompts.py      # System and step prompts
|   |   +-- tools.py        # Tool definitions
|   |-- browser/
|   |   |-- driver.py       # Playwright control
|   |   +-- snapshot.py     # Compact page view
|   |-- capture/
|   |   |-- recorder.py     # Network capture
|   |   +-- filters.py      # Noise filtering + redaction
|   |-- analysis/
|   |   |-- url_grouping.py # /books/123 -> /books/{id}
|   |   |-- schema.py       # JSON schema inference/merge
|   |   +-- detectors.py    # Pagination + auth detection
|   |-- generator/
|   |   |-- python_gen.py
|   |   |-- openapi_gen.py
|   |   +-- templates/      # Jinja2 templates
|   |-- llm/
|   |   +-- gateway.py      # LiteLLM + fallback + cache
|   +-- memory/
|       +-- store.py        # SQLite access
|-- tests/                  # Unit tests for non-LLM logic
|-- evals/                  # Test sites + scoring script
|-- .env.example
|-- pyproject.toml
|-- README.md
+-- IMPLEMENTATION.md
```

---

## Running on free LLM tiers

Ghost API is designed to cost **$0**:

- **Small prompts.** Pages and traffic are compressed into short summaries (under 8k tokens per call).
- **Few calls.** About 20–50 LLM calls per site, because code does the heavy work.
- **Fallback chain.** When one provider hits its rate limit, LiteLLM switches to the next (Gemini, then Groq, then OpenRouter).
- **Caching.** Repeated prompts are served from a local SQLite cache.

> Free-tier limits change often, so check each provider's current pricing page. Free tiers may use prompts for training. Ghost API redacts cookies and tokens before anything reaches an LLM, but don't point it at private data.

---

## Responsible use

Ghost API is built to behave like a polite, careful developer:

- Respects `robots.txt` by default
- Rate-limits its own requests (1 request per second by default)
- Never tries to bypass CAPTCHAs or bot protection; it detects them and stops
- Never clicks destructive or purchase actions (delete, buy, pay)
- Never stores passwords; for logins, you log in by hand in the browser window
- Redacts cookies and tokens from logs, reports and LLM prompts
- Uses an honest user-agent string

Check a site's terms of service before generating a client for it. For demos and testing, use practice sites such as `quotes.toscrape.com/scroll`, which loads its data from a JSON API. (`books.toscrape.com` renders on the server, so it has no API to discover.)

---

## Limitations

- Sites with strong bot protection (Cloudflare challenges, CAPTCHAs) will be skipped
- Server-rendered sites with no JSON API have nothing to discover
- GraphQL, WebSocket and gRPC APIs are out of scope for version 1
- Small free models sometimes make poor exploration choices; budgets and stopping rules limit the damage
- Generated clients can break when the site changes its internal API (`refresh` helps)

---

## Roadmap

| Stage | Focus | Status |
|---|---|---|
| 0 | Project setup | Done |
| 1 | Traffic capture | Done |
| 2 | API analysis (URL grouping, schemas) | Done |
| 3 | LLM gateway (fallback + cache) | Planned |
| 4 | Agent exploration loop | Planned |
| 5 | Hypothesis testing | Planned |
| 6 | Code generation | Planned |
| 7 | Verification and replanning | Planned |
| 8 | Memory | Planned |
| 9 | Reporting and OpenAPI export | Planned |
| 10 | Evaluation and polish | Planned |

Full details are in [IMPLEMENTATION.md](IMPLEMENTATION.md).
