# Ghost API: Implementation Specification

This document covers the workflow, requirements, system design and the stage-by-stage implementation plan for Ghost API. For what the project is and how it works, see [README.md](README.md).

---

## Table of contents

1. [Scope](#1-scope)
2. [Workflow](#2-workflow)
3. [Requirements](#3-requirements)
4. [Functional requirements](#4-functional-requirements)
5. [Non-functional requirements](#5-non-functional-requirements)
6. [System design](#6-system-design)
7. [Implementation plan](#7-implementation-plan)
8. [Evaluation](#8-evaluation)
9. [Risks and mitigations](#9-risks-and-mitigations)
10. [Requirement traceability](#10-requirement-traceability)

---

## 1. Scope

### 1.1 Goals

| # | Goal |
|---|---|
| G1 | Turn a URL and a plain-English goal into a working, typed client library |
| G2 | Run entirely on free tools and free LLM tiers ($0) |
| G3 | Run on a modest laptop (8 GB RAM, no GPU) |
| G4 | Show transparent reasoning, so the user can see why it did each thing |
| G5 | Improve with memory: re-running on a known site is faster and cheaper |

### 1.2 Non-goals (version 1)

- Bypassing CAPTCHAs, bot protection or paywalls
- GraphQL, WebSocket or gRPC APIs
- Automatic login with stored passwords
- Large-scale data scraping (it generates the client; it doesn't harvest data)
- Mobile apps

### 1.3 Users and use cases

| Persona | Need | Example |
|---|---|---|
| Student developer | Data for a side project | "I want library book availability in my app" |
| Indie hacker | Integrate a site with no API | "Pull job listings into my dashboard" |
| Data analyst | Repeatable data collection | "Track prices on a public catalog weekly" |
| QA or test engineer | Document an undocumented internal API | "What does our own frontend actually call?" |

| ID | Use case | Description |
|---|---|---|
| UC-1 | Generate client | The user gives a URL and a goal, and receives a client library |
| UC-2 | Authenticated site | The user logs in by hand in the opened browser, and the agent continues |
| UC-3 | Refresh client | The site changed; re-run to update the existing client |
| UC-4 | Inspect report | The user reads the discovered endpoints, schemas and the agent's reasoning |
| UC-5 | Export spec | Export an OpenAPI spec for use in Postman, Swagger and similar tools |

---

## 2. Workflow

### 2.1 User workflow

```
+--------------+   +--------------+   +--------------+   +--------------+
| 1. Run the   |-->| 2. Watch the |-->| 3. Answer    |-->| 4. Get the   |
| command with |   | agent explore|   | questions if |   | client, the  |
| URL + goal   |   | (live log)   |   | asked (login)|   | tests and a  |
+--------------+   +--------------+   +--------------+   | report       |
                                                         +--------------+
```

```bash
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors and tags" --lang python
```

### 2.2 System workflow

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
          |                                                    |
          |   +-------------+                                  |
          |   | 1. OBSERVE  | page snapshot + captured traffic |
          |   +------+------+ + what's known so far            |
          |          v                                         |
          |   +-------------+                                  |
          |   | 2. DECIDE   | LLM picks next action:           |
          |   +------+------+ explore / test / ask / finish    |
          |          v                                         |
          |   +-------------+                                  |
          |   | 3. ACT      | run tool (click, type, call API) |
          |   +------+------+                                  |
          |          v                                         |
          |   +-------------+                                  |
          |   | 4. LEARN    | update endpoint map + schemas    |
          |   +------+------+                                  |
          |          v                                         |
          |   goal covered? budget left? --no--> back to 1     |
          +----------+-----------------------------------------+
                     | yes
                     v
          +---------------------+
          | 5. GENERATE CODE    | client + models + tests
          +----------+----------+
                     v
          +---------------------+      fail      +--------------+
          | 6. VERIFY (tests)   |--------------->| 7. DIAGNOSE  |
          +----------+----------+                | & REPLAN     |--> back to
                     | pass                      +--------------+    agent loop
                     v                                               (max 3x)
          +---------------------+
          | 8. SAVE MEMORY +    |
          |    WRITE REPORT     |
          +----------+----------+
                     v
                   DONE
```

### 2.3 Workflow stages

| # | Stage | Done by | Input | Output |
|---|---|---|---|---|
| 0 | Pre-checks | Code | URL, goal, options | Budgets, robots.txt verdict, loaded memory |
| 1 | Observe | Code | Browser state, captured traffic, endpoint map | Compact observation (under 8k tokens) |
| 2 | Decide | LLM | Observation, goal, history | One tool call with a "thought" |
| 3 | Act | Code | Tool call | Tool result (page change, API response, user answer) |
| 4 | Learn | Code | New traffic | Updated endpoint map, schemas, hypotheses |
| 5 | Generate | Code + LLM | Verified endpoint map | Client, models, tests, OpenAPI |
| 6 | Verify | Code | Generated tests | Pass/fail per endpoint |
| 7 | Diagnose and replan | LLM + code | Failing tests and errors | Recovery plan, back to the loop |
| 8 | Save and report | Code | Run state | Memory rows, `report.md`, usage stats |

### 2.4 Failure recovery (stage 7)

| Failure | Likely cause | Agent's response |
|---|---|---|
| 401 or 403 | Missing cookie, token or header | Retry with the browser's cookies, then find which header is required |
| Schema mismatch | Too few samples | Explore more to collect samples, then widen the schema |
| 404 | Wrong URL template | Re-check how the URL was grouped |
| 429 | Rate limited | Back off and slow down |
| Timeout | Slow endpoint | Retry, then mark as unstable |
| Bot challenge or CAPTCHA | Bot protection | Stop politely and report it (never bypass) |

There are at most 3 replan rounds, then it reports partial success honestly.

### 2.5 Stopping rules

The agent loop stops when **any** of these is true:

- Every part of the goal maps to at least one verified endpoint
- The step budget is used up (default 40)
- The LLM call budget is used up (default 50)
- No new endpoint has been found in the last 8 steps
- The time limit is reached (default 15 minutes)
- The user stops it (Ctrl+C), which still writes partial results

---

## 3. Requirements

### 3.1 Hardware

| Item | Minimum |
|---|---|
| RAM | 4 GB free (8 GB total recommended) |
| CPU | Any modern dual-core |
| GPU | Not required |
| Disk | 2 GB (Chromium plus cache) |
| Internet | Required |

### 3.2 Software

| Software | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Main language |
| Playwright | Latest | Browser automation |
| Chromium | Installed via `playwright install chromium` | Browser |
| Git | Any | Version control |

### 3.3 Python dependencies

| Library | Purpose |
|---|---|
| `playwright` | Drive the browser and capture traffic |
| `litellm` | One interface to Gemini, Groq and OpenRouter, with fallback |
| `httpx` | Direct API calls during testing, and in the generated client |
| `pydantic` | Data models, both inside the tool and in the generated code |
| `genson` | Build JSON schemas from sample responses |
| `jinja2` | Code generation templates |
| `typer`, `rich` | CLI and live terminal output |
| `pyyaml` | OpenAPI export |
| `python-dotenv` | Load API keys from `.env` |
| `pytest` | Run unit tests and the generated tests |
| `ruff`, `mypy` | Linting and type checks (dev only) |
| `sqlite3` | Storage (built into Python) |

### 3.4 Accounts (all free)

| Account | Why |
|---|---|
| Google AI Studio | Gemini API key, the main LLM |
| Groq | Fallback LLM key |
| OpenRouter | Second fallback (optional) |
| GitHub | Host the project |

### 3.5 Skills

- Python (intermediate)
- HTTP basics: methods, headers, status codes, JSON
- Browser DevTools Network tab (to check the agent's work)
- Prompting LLMs for structured (JSON) output
- Basic agent loop design

---

## 4. Functional requirements

Priority: **M** = must have (MVP), **S** = should have, **C** = could have (later).

### 4.1 Input and configuration

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Accept a target URL and a plain-English goal through the CLI (`ghost-api run`) | M |
| FR-2 | Accept an output language option (`python`; `typescript` later) | M / C |
| FR-3 | Accept budget limits: max LLM calls, max steps, max minutes | M |
| FR-4 | Load API keys from a `.env` file | M |
| FR-5 | Support headed mode (visible browser) and headless mode | S |
| FR-6 | Accept a request rate limit for the target site (`--rate-limit`) | M |

### 4.2 Exploration

| ID | Requirement | Priority |
|---|---|---|
| FR-7 | Open the URL in a real Chromium browser through Playwright | M |
| FR-8 | Extract a compact list of interactive elements (buttons, links, inputs) with short IDs | M |
| FR-9 | Let the agent click, type, scroll, go back and navigate | M |
| FR-10 | Stay on the target domain and its API subdomains | M |
| FR-11 | Block destructive actions (elements matching delete, buy, pay, checkout, unsubscribe) | M |
| FR-12 | Check robots.txt before exploring; stop unless `--ignore-robots` is given | M |

### 4.3 Traffic capture

| ID | Requirement | Priority |
|---|---|---|
| FR-13 | Record every XHR or fetch request: method, URL, headers, body, status and response | M |
| FR-14 | Filter out non-API traffic (images, CSS, fonts, known analytics domains) | M |
| FR-15 | Store captured traffic in SQLite | M |
| FR-16 | Redact sensitive values (cookies, auth headers, tokens) in logs, reports and prompts | M |

### 4.4 API understanding

| ID | Requirement | Priority |
|---|---|---|
| FR-17 | Group similar URLs into templates with path parameters (`/books/{id}`) | M |
| FR-18 | Identify query parameters and classify them as required, optional or constant | M |
| FR-19 | Infer a JSON schema for each endpoint, merged from multiple samples | M |
| FR-20 | Detect pagination style (page number, offset, cursor) | S |
| FR-21 | Detect authentication style (cookie, bearer token, API key header, none) | S |
| FR-22 | Use the LLM to give endpoints, models and fields readable names and descriptions | M |

### 4.5 Hypothesis testing

| ID | Requirement | Priority |
|---|---|---|
| FR-23 | Let the agent call endpoints directly (outside the browser) to test guesses | M |
| FR-24 | Test whether headers or cookies are required by removing them one at a time | S |
| FR-25 | Test pagination by requesting the next page and comparing results | S |
| FR-26 | Record each hypothesis with its result (confirmed or rejected) and evidence | M |

### 4.6 Code generation

| ID | Requirement | Priority |
|---|---|---|
| FR-27 | Generate a Python client class with one method per endpoint | M |
| FR-28 | Generate pydantic models for request and response data | M |
| FR-29 | Include docstrings with the endpoint description and an example | M |
| FR-30 | Include built-in rate limiting and retry in the generated client | S |
| FR-31 | Generate a pytest test file covering each endpoint | M |
| FR-32 | Export an OpenAPI 3 spec | S |
| FR-33 | Generate a TypeScript client | C |

### 4.7 Verification and replanning

| ID | Requirement | Priority |
|---|---|---|
| FR-34 | Run the generated tests automatically after generation | M |
| FR-35 | On failure, classify the error and choose a recovery strategy (section 2.4) | M |
| FR-36 | Limit replan rounds (default 3) | M |
| FR-37 | Mark each endpoint as verified, unverified or failed in the output | M |

### 4.8 Human in the loop

| ID | Requirement | Priority |
|---|---|---|
| FR-38 | Pause and ask the user to log in by hand when a login wall is detected | S |
| FR-39 | Ask the user when the goal is ambiguous ("I found two search endpoints. Which one?") | S |
| FR-40 | Let the user stop at any time (Ctrl+C) and still get partial results | M |

### 4.9 Memory

| ID | Requirement | Priority |
|---|---|---|
| FR-41 | Save the endpoint map, schemas, auth notes and lessons learned per domain | M |
| FR-42 | On re-run (`ghost-api refresh`), load memory and only re-verify known endpoints | S |
| FR-43 | Detect changes (new or removed fields, new endpoints) and update the client | C |
| FR-44 | Cache LLM responses keyed by prompt hash to save quota | M |
| FR-45 | Show stored knowledge for a site (`ghost-api inspect`) | S |

### 4.10 Reporting

| ID | Requirement | Priority |
|---|---|---|
| FR-46 | Show live progress in the terminal (actions, discoveries, tests) | M |
| FR-47 | Write `report.md` with endpoints, confidence, reasoning log and known issues | M |
| FR-48 | Show usage stats: LLM calls, tokens, time taken | M |

---

## 5. Non-functional requirements

### 5.1 Cost

| ID | Requirement |
|---|---|
| NFR-1 | Total running cost must be **$0** using free LLM tiers |
| NFR-2 | A typical site should need **50 or fewer LLM calls** |
| NFR-3 | Automatically fall back to the next provider when one hits its rate limit |

### 5.2 Performance

| ID | Requirement |
|---|---|
| NFR-4 | A typical small site finishes in **10 minutes or less** |
| NFR-5 | Peak memory use **1.5 GB or less**, browser included |
| NFR-6 | Each LLM prompt **8k tokens or less**, kept small by compressing page and traffic summaries |

### 5.3 Reliability

| ID | Requirement |
|---|---|
| NFR-7 | No crash on LLM errors: retry, then fall back, then fail gracefully with partial output |
| NFR-8 | Invalid JSON from the LLM is detected and re-requested (up to 2 times) |
| NFR-9 | A run can be interrupted and resumed from saved state |

### 5.4 Accuracy

| ID | Requirement |
|---|---|
| NFR-10 | Endpoint recall **80% or higher** on the evaluation sites |
| NFR-11 | Generated test pass rate **85% or higher** |
| NFR-12 | **Zero invented endpoints**: every endpoint must come from real captured traffic |

### 5.5 Security and privacy

| ID | Requirement |
|---|---|
| NFR-13 | Never send cookies, tokens or passwords to the LLM; redact them first |
| NFR-14 | Never store user passwords |
| NFR-15 | API keys only in `.env`, which is listed in `.gitignore` |
| NFR-16 | Generated code must not contain hard-coded secrets |

### 5.6 Ethics and compliance

| ID | Requirement |
|---|---|
| NFR-17 | Respect robots.txt by default |
| NFR-18 | Rate-limit its own requests (default: at most 1 request per second) |
| NFR-19 | Never attempt to bypass CAPTCHAs or bot protection; detect them and stop |
| NFR-20 | Send an honest user-agent string identifying the tool |
| NFR-21 | Never click destructive or purchase actions |

### 5.7 Usability

| ID | Requirement |
|---|---|
| NFR-22 | One command to run; sensible defaults for everything |
| NFR-23 | Clear, human-readable error messages |
| NFR-24 | Generated code is readable and well named, as if a human wrote it |
| NFR-25 | The README gets a new user running in **5 minutes or less** |

### 5.8 Maintainability

| ID | Requirement |
|---|---|
| NFR-26 | Modular design: browser, capture, analysis, agent, generator, LLM and memory are separate packages |
| NFR-27 | Adding a new LLM provider means changing only config |
| NFR-28 | Unit tests for all non-LLM logic (URL grouping, schema merging, redaction, filters) |
| NFR-29 | Type hints throughout; passes `ruff` and `mypy` |

### 5.9 Portability

| ID | Requirement |
|---|---|
| NFR-30 | Runs on Windows, macOS and Linux |
| NFR-31 | No GPU or paid service required |

### 5.10 Observability

| ID | Requirement |
|---|---|
| NFR-32 | Every agent decision is logged as thought, action and result |
| NFR-33 | Runs are saved so they can be replayed for debugging |

---

## 6. System design

### 6.1 Components

```
+----------------------------------------------------------------+
|                          CLI (Typer)                           |
+-------------------------------+--------------------------------+
                                v
+----------------------------------------------------------------+
|                     AGENT ORCHESTRATOR                         |
|      loop - budget tracking - replanning - human prompts       |
+--+----------+-----------+------------+------------+------------+
   v          v           v            v            v
+-------+ +--------+ +----------+ +----------+ +----------+
|BROWSER| |CAPTURE | | ANALYZER | |GENERATOR | |   LLM    |
|Playw- | |network | |URL group | |templates | | GATEWAY  |
|right  | |filter  | |schema    | |client +  | |LiteLLM + |
|actions| |redact  | |inference | |tests +   | |fallback +|
|       | |        | |          | |OpenAPI   | |cache     |
+---+---+ +---+----+ +----+-----+ +----+-----+ +----+-----+
    +---------+-----------+------------+------------+
                          v
              +------------------------+
              |   SQLite (memory +     |
              |   traffic + cache)     |
              +------------------------+
```

| Component | Package | Responsibility |
|---|---|---|
| CLI | `ghost_api/cli.py` | Parse arguments, show live progress, write output files |
| Agent orchestrator | `ghost_api/agent/` | Observe-decide-act-learn loop, budgets, replanning, human prompts |
| Browser | `ghost_api/browser/` | Control Chromium, build the compact page snapshot |
| Capture | `ghost_api/capture/` | Record traffic, filter noise, redact secrets |
| Analyzer | `ghost_api/analysis/` | URL templates, schema inference, pagination and auth detection (no LLM) |
| Generator | `ghost_api/generator/` | Client code, tests and OpenAPI from templates |
| LLM gateway | `ghost_api/llm/` | One interface to all providers, with fallback, retries and cache |
| Memory store | `ghost_api/memory/` | SQLite access for traffic, endpoints, schemas, memory and cache |

### 6.2 Agent toolset

| Tool | What it does |
|---|---|
| `click(element_id)` | Click a page element |
| `type(element_id, text)` | Type into an input |
| `scroll(direction)` | Scroll to trigger lazy loading |
| `navigate(url)` | Go to a URL on the same domain |
| `go_back()` | Browser back |
| `call_endpoint(method, url, params, headers)` | Call the API directly to test a guess |
| `inspect_endpoint(endpoint_id)` | View the samples and schema for an endpoint |
| `record_hypothesis(text, result)` | Log a guess and its outcome |
| `ask_user(question)` | Pause for human input |
| `finish(reason)` | Stop exploring and move to code generation |

### 6.3 Database schema (SQLite)

| Table | Key fields |
|---|---|
| `runs` | id, domain, goal, started_at, finished_at, status, llm_calls, tokens |
| `requests` | id, run_id, method, url, status, req_headers, req_body, resp_body, timestamp |
| `endpoints` | id, domain, method, url_template, name, description, auth_type, pagination, status |
| `schemas` | endpoint_id, kind (request/response), json_schema, sample_count |
| `hypotheses` | id, run_id, endpoint_id, text, result, evidence |
| `agent_steps` | id, run_id, step_no, thought, action, args, result |
| `lessons` | id, domain, text, created_at |
| `llm_cache` | prompt_hash, provider, model, response, created_at |

### 6.4 Key design decisions

| Decision | Reason |
|---|---|
| LLM decides and names; code analyzes and generates | Cheap, fast, and reliable with small free models |
| One action per LLM call | Easier to debug, and each step is logged clearly |
| Endpoints only from captured traffic | Prevents hallucinated endpoints (NFR-12) |
| Redaction before storage and prompts | Secrets never leave the machine (NFR-13) |
| Templates (Jinja2) for code, not LLM-written code | Generated code is consistent and always valid Python |
| SQLite for everything | Zero setup, one file, easy to inspect |

---

## 7. Implementation plan

The build is split into 11 stages over about two weeks. Each stage ends with something that runs and can be checked. Stages 0–9 complete every **M** requirement; **S** items are included where they fit, and **C** items are left for later.

### Overview

| Stage | Name | Days | Main output |
|---|---|---|---|
| 0 | Project setup | 0.5 | Installable package, CLI skeleton |
| 1 | Traffic capture | 1.5 | Browser opens a site and stores filtered, redacted traffic |
| 2 | API analysis | 1 | Endpoint map with URL templates and schemas |
| 3 | LLM gateway | 1 | `llm.ask()` with fallback, retries and cache |
| 4 | Agent exploration loop | 2 | Agent explores toward a goal and stops sensibly |
| 5 | Hypothesis testing | 1 | Pagination and auth detection by direct calls |
| 6 | Code generation | 1.5 | Python client, models and tests |
| 7 | Verification and replanning | 1.5 | Tests auto-run; failures trigger recovery |
| 8 | Memory | 1 | Refresh and inspect commands use stored knowledge |
| 9 | Reporting and export | 1 | `report.md`, OpenAPI, usage stats |
| 10 | Evaluation and polish | 2 | Metrics, README demo, release |

---

### Stage 0: Project setup (Day 1, morning)

**Goal:** a clean, installable project skeleton.

**Tasks**
- [x] Create the folder structure from section 6.1
- [x] `pyproject.toml` with dependencies and a `ghost-api` console script
- [x] `.env.example`, `.gitignore` (includes `.env`, `output/`, `*.db`)
- [x] `config.py`: settings and default budgets loaded from env and CLI
- [x] Typer CLI with `run`, `refresh` and `inspect` commands (stubs)
- [x] Set up `pytest`, `ruff` and `mypy`
- [x] `git init` and first commit

**Deliverable:** `ghost-api --help` lists the commands.

**Done when:** `pip install -e .` works and `pytest` runs (with zero tests).

**Requirements covered:** FR-1, FR-2 (python), FR-3, FR-4, NFR-15, NFR-26

---

### Stage 1: Traffic capture (Day 1 afternoon to Day 2)

**Goal:** open a site in a real browser and record clean API traffic.

**Tasks**
- [x] `browser/driver.py`: launch Chromium (headed or headless), open a URL, honest user-agent
- [x] `capture/recorder.py`: hook Playwright `request` and `response` events for XHR and fetch
- [x] `capture/filters.py`: drop images, CSS, fonts, media and known analytics domains; keep JSON responses
- [x] Redact `Cookie`, `Authorization`, `Set-Cookie` and token-like values
- [x] `memory/store.py`: create the `runs` and `requests` tables and save traffic
- [x] robots.txt check before opening the site
- [x] Rate limiter for requests the tool makes itself
- [x] Unit tests for filters and redaction

**Deliverable:** `ghost-api run <url> --goal x` opens the site, waits, and prints the captured API requests.

**Done when:** on `quotes.toscrape.com/scroll`, the JSON API calls are captured, noise is filtered, and no secrets appear in the database.

**Requirements covered:** FR-5, FR-6, FR-7, FR-12, FR-13, FR-14, FR-15, FR-16, NFR-17, NFR-18, NFR-20

---

### Stage 2: API analysis (Day 3)

**Goal:** turn raw traffic into an endpoint map, without any LLM.

**Tasks**
- [ ] `analysis/url_grouping.py`: detect variable path segments (numbers, UUIDs, slugs that vary across samples) and build templates
- [ ] Classify query parameters as constant (same in every sample), variable or optional
- [ ] `analysis/schema.py`: use genson to build and merge JSON schemas; track sample counts
- [ ] Mark fields that are missing in some samples as optional
- [ ] Create the `endpoints` and `schemas` tables
- [ ] Unit tests with hand-written traffic fixtures

**Deliverable:** a function that turns stored traffic into a printed endpoint table.

**Done when:** `/api/books/1`, `/api/books/2` and `/api/books/3` collapse into one `/api/books/{id}` endpoint with a correct merged schema.

**Requirements covered:** FR-17, FR-18, FR-19, NFR-12, NFR-28

---

### Stage 3: LLM gateway (Day 4)

**Goal:** one reliable, free way to call an LLM.

**Tasks**
- [ ] `llm/gateway.py`: `ask(messages, schema=None)` built on LiteLLM
- [ ] Provider chain from config: Gemini, then Groq, then OpenRouter
- [ ] On 429 or 5xx: retry with backoff, then move to the next provider
- [ ] Structured output: parse JSON, validate with pydantic, re-ask up to 2 times on invalid output
- [ ] `llm_cache` table keyed by a hash of model and prompt
- [ ] Count calls and tokens for the budget and the final report
- [ ] Redaction check before sending (a final safety net)

**Deliverable:** `llm.ask()` returns validated JSON and survives one provider being down.

**Done when:** a test that forces Gemini to fail succeeds through Groq, and a repeated call is served from the cache.

**Requirements covered:** FR-44, NFR-1, NFR-3, NFR-7, NFR-8, NFR-13, NFR-27

---

### Stage 4: Agent exploration loop (Days 5–6)

**Goal:** the agent explores the site toward the goal on its own.

**Tasks**
- [ ] `browser/snapshot.py`: list visible interactive elements with short IDs, labels and roles; cap the size
- [ ] Block destructive elements (delete, buy, pay, checkout, unsubscribe) and links off the domain
- [ ] `agent/tools.py`: click, type, scroll, navigate, go_back, finish
- [ ] `agent/prompts.py`: system prompt (role, rules, tools, JSON format) and step prompt (observation, goal coverage, endpoint map, recent steps)
- [ ] `agent/orchestrator.py`: observe, decide, act, learn loop; run the analyzer after each step
- [ ] Goal coverage: ask the LLM which goal parts the current endpoints satisfy (cheap, done every few steps)
- [ ] Stopping rules from section 2.5
- [ ] Log every step to `agent_steps`
- [ ] Ctrl+C handling that saves partial state
- [ ] Rich live view: current action, endpoints found, budget left

**Deliverable:** the agent explores `quotes.toscrape.com/scroll` toward a goal and stops by itself.

**Done when:** on two practice sites the agent finds the endpoints needed for the goal within budget, without any destructive or off-site actions.

**Requirements covered:** FR-8, FR-9, FR-10, FR-11, FR-40, FR-46, NFR-2, NFR-6, NFR-21, NFR-32

---

### Stage 5: Hypothesis testing (Day 7)

**Goal:** the agent checks its guesses by calling the API directly.

**Tasks**
- [ ] `call_endpoint` tool using httpx with the rate limiter; reuses browser cookies only in memory
- [ ] `inspect_endpoint` and `record_hypothesis` tools; `hypotheses` table
- [ ] `analysis/detectors.py`: guess pagination (page, offset, cursor parameters, `next` links) and auth style
- [ ] Required-header test: repeat the call with each header removed and compare the result
- [ ] Pagination test: request the next page and compare the IDs that come back
- [ ] `ask_user` tool, plus login-wall detection (redirect to a login page, or 401 on first load)
- [ ] Bot-challenge detection: stop and report

**Deliverable:** endpoints carry confirmed pagination and auth info, with evidence.

**Done when:** pagination is correctly identified on the practice sites, and each hypothesis has a stored result.

**Requirements covered:** FR-20, FR-21, FR-23, FR-24, FR-25, FR-26, FR-38, FR-39, NFR-14, NFR-19

---

### Stage 6: Code generation (Days 8–9 morning)

**Goal:** turn the endpoint map into a clean, typed Python client.

**Tasks**
- [ ] Naming pass: one LLM call returns method names, model names, field descriptions and docstrings for all endpoints
- [ ] JSON schema to pydantic model converter (nested objects, lists, optional fields, enums kept as `str`)
- [ ] Jinja2 templates: `client.py.j2`, `models.py.j2`, `test_client.py.j2`
- [ ] Generated client: httpx session, base URL, rate limit, retry with backoff, typed methods, error class
- [ ] Generated tests: call each endpoint with captured example parameters and validate against the model
- [ ] Format the output with `ruff format` and check it compiles
- [ ] Secret scan on the generated files

**Deliverable:** `output/<domain>/` contains the client, models and tests.

**Done when:** the generated code imports cleanly, passes `mypy`, and contains no secrets.

**Requirements covered:** FR-22, FR-27, FR-28, FR-29, FR-30, FR-31, NFR-16, NFR-24

---

### Stage 7: Verification and replanning (Days 9 afternoon to 10)

**Goal:** prove the client works, and recover when it doesn't.

**Tasks**
- [ ] Run the generated pytest file in a subprocess and parse the results per endpoint
- [ ] Error classifier for the failure types in section 2.4
- [ ] Recovery strategies: add cookies or headers, collect more samples, re-group the URL, back off, mark unstable
- [ ] Re-enter the agent loop with a focused sub-goal ("collect more samples of `/api/books/{id}`")
- [ ] Cap at 3 replan rounds, then finish with partial results
- [ ] Set each endpoint's status to verified, unverified or failed

**Deliverable:** a full run ends with verified endpoints, or an honest partial result.

**Done when:** deliberately breaking a schema or header in a test fixture triggers the right recovery and the test then passes.

**Requirements covered:** FR-34, FR-35, FR-36, FR-37, NFR-7, NFR-9

---

### Stage 8: Memory (Day 11)

**Goal:** a second run on a known site is faster and smarter.

**Tasks**
- [ ] Save the endpoint map, schemas, auth and pagination notes per domain at the end of a run
- [ ] `lessons` table: the LLM writes 1–3 short lessons at the end of each run, which are added to later prompts for that domain
- [ ] `ghost-api refresh <domain>`: load memory, re-verify known endpoints, explore only when something fails
- [ ] `ghost-api inspect <domain>`: print stored endpoints, statuses and lessons
- [ ] Resume an interrupted run from saved state

**Deliverable:** working `refresh` and `inspect` commands.

**Done when:** a refresh on a known site uses at least 50% fewer LLM calls than the first run.

**Requirements covered:** FR-41, FR-42, FR-45, NFR-9, G5

---

### Stage 9: Reporting and export (Day 12)

**Goal:** clear outputs a human can trust.

**Tasks**
- [ ] `report.md`: summary, endpoint table with status, confidence and sample count, hypotheses, reasoning log, known issues
- [ ] Confidence score per endpoint: based on samples, verification and hypotheses
- [ ] Usage block: LLM calls, tokens, time, cost ($0)
- [ ] `generator/openapi_gen.py`: OpenAPI 3 YAML from the endpoint map
- [ ] Save full runs so they can be replayed for debugging

**Deliverable:** the complete `output/<domain>/` folder.

**Done when:** `openapi.yaml` validates and loads in Swagger Editor, and the report matches the database.

**Requirements covered:** FR-32, FR-47, FR-48, NFR-33

---

### Stage 10: Evaluation and polish (Days 13–14)

**Goal:** measure the results, fix the worst problems, and publish.

**Tasks**
- [ ] Build a small local demo app (FastAPI plus a simple frontend) where the true endpoint list is known exactly
- [ ] `evals/`: a ground-truth endpoint list per evaluation site, and a scoring script
- [ ] Run all evaluation sites and record the metrics from section 8.2
- [ ] Fix the top 3 failure causes and re-run
- [ ] Clear error messages for common problems (no API key, robots.txt block, bot challenge)
- [ ] Update README.md: real results, demo GIF, status table
- [ ] Final `ruff`, `mypy` and unit test pass

**Deliverable:** a public GitHub repo with real numbers.

**Done when:** every item in section 8.3 is checked.

**Requirements covered:** NFR-4, NFR-5, NFR-10, NFR-11, NFR-22, NFR-23, NFR-25, NFR-29, NFR-30, NFR-31

---

### After the MVP (could-have items)

| Item | Requirement |
|---|---|
| TypeScript client generation | FR-33 |
| Change detection and automatic client updates | FR-43 |
| GraphQL support | Non-goal for v1 |
| Web UI that shows the agent's exploration live | New |
| Vision mode: send screenshots to Gemini when the element list isn't enough | New |

---

## 8. Evaluation

### 8.1 Evaluation sites (legal and safe)

| Site | Why |
|---|---|
| `books.toscrape.com` | Negative test: server-rendered, so Ghost API must report "no API found" rather than invent one |
| `quotes.toscrape.com` (scroll and other variants) | Built for practice; has a JSON API variant |
| Hacker News | Public API to compare against |
| Open Library | Public data with a known API |
| Local demo app | Exact ground truth |

### 8.2 Metrics

| Metric | How it's measured | Target |
|---|---|---|
| Endpoint recall | Found / actual (checked by hand in DevTools or from ground truth) | 80% or higher |
| Endpoint precision | Real / reported | 100% |
| Test pass rate | Passing / generated tests | 85% or higher |
| LLM calls per site | Counted by the gateway | 50 or fewer |
| Refresh savings | Refresh calls / first-run calls | 50% or lower |
| Time per site | Measured | 10 minutes or less |
| Peak memory | Measured | 1.5 GB or less |
| Cost | Measured | $0 |

### 8.3 MVP acceptance checklist

- [ ] One command produces a working client for at least 3 evaluation sites
- [ ] Generated tests pass at or above target
- [ ] No invented endpoints on any site
- [ ] No secrets in the database, reports, prompts or generated code
- [ ] The report shows the reasoning log
- [ ] Runs on an 8 GB laptop with only free API keys
- [ ] README has setup steps, real results and a demo GIF

---

## 9. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Free LLM rate limits | Runs stall | Fallback chain, caching, small prompts |
| Weak free models make bad decisions | Poor exploration | Code does the heavy analysis; the LLM only picks actions and names things; budgets limit waste |
| Bot protection | Some sites fail | Detect it, stop politely, document as a limitation |
| LLM invents endpoints | Wrong output | Endpoints only from captured traffic (NFR-12) |
| Leaking secrets to the LLM | Privacy issue | Redaction at capture time and again before every prompt (NFR-13) |
| Legal or terms-of-service issues | Reputation | Practice and public sites only, robots.txt, rate limiting |
| Generated code is invalid | Broken output | Templates instead of free-form LLM code; compile and type checks |
| Scope creep | Never finishes | Only M-priority items until Stage 10 is done |

---

## 10. Requirement traceability

| Stage | Functional requirements | Non-functional requirements |
|---|---|---|
| 0 Setup | FR-1, FR-2 (python), FR-3, FR-4 | NFR-15, NFR-26 |
| 1 Capture | FR-5, FR-6, FR-7, FR-12 to FR-16 | NFR-17, NFR-18, NFR-20 |
| 2 Analysis | FR-17 to FR-19 | NFR-12, NFR-28 |
| 3 LLM gateway | FR-44 | NFR-1, NFR-3, NFR-7, NFR-8, NFR-13, NFR-27 |
| 4 Agent loop | FR-8 to FR-11, FR-40, FR-46 | NFR-2, NFR-6, NFR-21, NFR-32 |
| 5 Hypotheses | FR-20, FR-21, FR-23 to FR-26, FR-38, FR-39 | NFR-14, NFR-19 |
| 6 Generation | FR-22, FR-27 to FR-31 | NFR-16, NFR-24 |
| 7 Verification | FR-34 to FR-37 | NFR-7, NFR-9 |
| 8 Memory | FR-41, FR-42, FR-45 | NFR-9 |
| 9 Reporting | FR-32, FR-47, FR-48 | NFR-33 |
| 10 Evaluation | none | NFR-4, NFR-5, NFR-10, NFR-11, NFR-22, NFR-23, NFR-25, NFR-29, NFR-30, NFR-31 |
| Later | FR-2 (TypeScript part), FR-33, FR-43 | none |
