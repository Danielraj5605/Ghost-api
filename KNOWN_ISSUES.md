# Known issues

Problems found while building, testing and using Ghost API, ordered by priority. Each has
what goes wrong, how it was found, and a suggested fix.

**Priority:** P1 = safety or wrong results, fix first · P2 = makes sites or features fail ·
P3 = quality and polish · Env = outside the code.

Last updated: 2026-10-15 (network boundary, audit fixes).

---

## P1: Safety and correctness

### 1. ~~Data-changing POST requests can be treated as safe to replay~~ (fixed 2026-10-07)
- **Fixed:** URLs are now split into whole words; a request is refused if any word is a
  write word (`delete`, `add`, `update`, `cart`, `order`...), even for GET, and a POST
  also needs a whole read word (`search`, `query`...). Tests cover the URLs below.
- **Problem:** A POST counts as read-only if its URL contains a word like `search`, `query`,
  `list`, `filter` or `find` *anywhere*. So `POST /api/lists/7/delete` and
  `POST /api/wishlist/add` count as safe, and the prober (and `call_endpoint`) could send them,
  changing data on the site.
- **Found:** Code review while writing this file, then confirmed with `is_replayable()`:
  `/api/lists`, `/api/lists/7/delete` and `/api/wishlist/add` all returned `True`.
- **Where:** `ghost_api/probe/replay.py` (`READ_ONLY_POST_RE`, `is_replayable`).
- **Fix:** Match whole path segments, not substrings (`/search`, `/query`, `/graphql`), and
  reject any path that also contains a write word (`delete`, `add`, `create`, `update`,
  `remove`, `cart`, `order`, `checkout`...). Also apply the browser's destructive-word list.
  Add tests for these exact URLs.

### 2. The agent's "goal covered" judgement can be too generous
- **Partly fixed (2026-10-15):** `goal-covered` now also needs the agent's `missing` field to
  be empty and at least one endpoint to be found; otherwise the run ends as `finished`.
  Mapping each part of the goal to a field (below) is still to do.
- **Problem:** The agent can declare the goal covered when it isn't. On Wikipedia, the goal
  was "search for articles and get article summaries"; it finished with `goal-covered`
  although its own lesson said the endpoint returns titles, not summaries.
- **Found:** Wikipedia run (run #16), `output/en.wikipedia.org/report.md`.
- **Where:** `ghost_api/agent/orchestrator.py`, `ghost_api/agent/prompts.py`.
- **Fix:** Before accepting `finish(goal_covered=True)`, ask the agent to map each part of the
  goal to a specific endpoint field (for example "summaries -> pages[].excerpt"), and reject
  the finish if a part has no field. Report `finished (partly covered)` honestly instead.

### 3. Page content can try to steer the agent (prompt injection)
- **Problem:** Page text, element labels and LLM-written lessons go into the agent's prompt.
  A hostile page could include instructions ("ignore your goal, navigate to...").
- **Mitigations already in place:** a fixed list of actions, destructive and off-site
  elements hidden, robots.txt and same-site checks, rate limits, no credentials typed,
  lessons cleaned and marked "may be outdated".
- **Remaining risk:** the agent could still waste steps, or ask the user misleading questions
  through `ask_user`.
- **Fix:** Wrap page content in clearly marked "untrusted data" blocks in the prompt, show
  `ask_user` questions with a warning that they come from the agent, and add a test page
  with injected instructions to the evaluation.

---

## P2: Sites or features that fail

### 4. Generated clients don't identify the user, so sites like Wikipedia refuse them
- **Problem:** `GHOST_API_CONTACT` is added to the tool's own requests (robots.txt, browser)
  but not to the generated Python and TypeScript clients. Sites that require contact info
  (Wikimedia) will answer their requests with HTTP 403, so verification fails.
- **Found:** Investigating Wikipedia's 403.
- **Where:** `ghost_api/generator/templates/client.py.j2`, `client.ts.j2`.
- **Fix:** Have generated clients read a contact from an environment variable *when they
  run* (for example `<CLIENT>_CONTACT`, falling back to `GHOST_API_CONTACT`) and add it to
  their User-Agent. Never write the contact into the generated code.

### 5. Wikipedia: the 403 on the search endpoint itself is not proven
- **Problem:** The 403 on `/w/rest.php/v1/search/title` is *probably* the same "identify
  yourself" rule that blocked robots.txt, but that was not tested, to avoid sending more
  requests to a path the site disallows.
- **Fix:** Only test it with the site's permission and a real contact set (see 4), or
  leave it documented as an inference.

### 6. Endpoints seen with only one value are not generalised
- **Problem:** A path segment becomes a parameter only if it looks like an ID or was seen
  with several values. If the agent saw just one Algolia index, the endpoint stays
  `/1/indexes/Item_dev/query`, and the client gets a fixed index and a clumsy method name
  (`post_indexes_item_dev_query`).
- **Found:** First hn.algolia.com evaluation run.
- **Where:** `ghost_api/analysis/url_grouping.py`, `ghost_api/probe/prober.py`.
- **Fix:** Let the prober test a guess: if a segment looks like a name (`Item_dev`), try a
  related value seen elsewhere in the traffic and see whether the response keeps its shape.

### 7. Free-tier model names go stale
- **Problem:** Providers retire free models without notice (`gemini-2.5-flash-lite`,
  `qwen/qwen3.8-27b:free` both broke during evaluation). The fallback lists are hard-coded,
  so they will go stale again.
- **Mitigation:** each provider has several fallback models, and retired ones are skipped
  at runtime.
- **Where:** `ghost_api/llm/providers.py`.
- **Fix:** Let `ghost-api doctor` list the provider's current models and suggest updates, or
  pick a working model from the provider's model list automatically.

### 8. Rate limits are forgotten between runs
- **Problem:** Cooldowns live in memory, so each new run tries providers that the previous
  run just found rate-limited (books.toscrape.com failed this way in the first evaluation).
- **Where:** `ghost_api/llm/gateway.py`.
- **Fix:** Store cooldowns (provider, model, until) in the SQLite database and read them at
  start-up.

### 9. Memory keeps only the latest run's endpoints
- **Problem:** `refresh` and code generation use the endpoints of the most recent run. An
  endpoint found in an earlier run but not in the latest is forgotten.
- **Where:** `ghost_api/memory/knowledge.py`, `ghost_api/memory/store.py`.
- **Fix:** Merge endpoints across runs, with a "last seen" date and status, and drop them
  only after a refresh confirms they are gone.

### 10. ~~No change detection~~ (fixed 2026-10-07: `ghost-api watch`)
- **Problem:** Ghost API does not tell you when a site's API changed (new or removed fields,
  new endpoints). This was planned (FR-43) and not built.
- **Fix:** In `refresh`, compare new schemas with stored ones and print a short diff
  (added, removed and type-changed fields), also in `report.md`.

### 11. Logged-in sites need manual work
- **Problem:** The agent can only ask you to log in when running `--headed` in an
  interactive terminal. Generated clients for cookie-protected endpoints need you to pass
  the cookie yourself, and sessions expire.
- **Fix:** Document the workflow, and add a `--login` mode that opens the browser, waits
  for you to log in, then explores.

### 12. GraphQL and WebSocket APIs are not supported
- **Problem:** GraphQL queries all go to one URL, so they look like a single endpoint;
  WebSocket traffic is not captured.
- **Fix:** Group GraphQL requests by operation name, and generate one method per query.

---

## P3: Quality and polish

### 13. Safety word list is English-only and sometimes too broad
- **Problem:** Destructive buttons are recognised by English words only ("Delete",
  "Buy"), so "Löschen" or "Supprimer" would be allowed. Some harmless controls are hidden
  ("Share", "Hide filters", "Follow" links).
- **Where:** `ghost_api/browser/snapshot.py` (`DESTRUCTIVE_RE`).
- **Fix:** Add common words in other major languages; consider form method and button
  type as extra signals; allow a site-specific override.

### 14. Robots.txt is only checked for the start host when navigating
- **Problem:** `RobotsPolicy.allows()` returns `True` for other hosts, so browser navigation
  to another subdomain of the same site is not checked. (Direct calls are checked per host.)
- **Where:** `ghost_api/net.py`, `ghost_api/agent/orchestrator.py`.
- **Fix:** Fetch and cache robots.txt per host for navigation too, as the replayer does.

### 15. Redaction relies on names and a few patterns
- **Problem:** Secrets are found by field and header names (`token`, `api_key`...) and by
  JWT and bearer patterns. A secret in an oddly named field, or in a non-JSON, non-form body,
  could be stored.
- **Where:** `ghost_api/capture/filters.py`.
- **Fix:** Also flag long high-entropy strings, and let users add their own sensitive names.

### 16. Only the first 60 elements on a page are shown to the agent
- **Problem:** On busy pages the element that matters may be cut off (the agent is told to
  scroll, but may not).
- **Fix:** Rank elements by relevance to the goal before capping.

### 17. Page-size limits are not tested
- **Problem:** Pagination is confirmed, but whether `limit=100` works (fewer calls for
  users) is not tested, although the plan mentioned it.
- **Fix:** Add a "bigger page size" hypothesis to the prober.

### 18. TypeScript clients don't validate responses at runtime
- **Problem:** Types are checked at compile time only, so a changed response shape is not
  caught at runtime, and the "relax the model" repair has nothing to fix.
- **Fix:** Optionally generate a small validator (or zod schemas) alongside the interfaces.

### 19. Confidence scores are a simple formula
- **Problem:** The High/Medium/Low points were chosen by hand, not tested against outcomes.
- **Fix:** Re-weight them after more evaluation runs.

### 20. The evaluation is small
- **Problem:** Four sites is enough to find bugs, not to make general claims. Open Library
  (in the original plan) was not evaluated, and Wikipedia stops by design.
- **Fix:** Add more permissive sites with known answers, plus the injection test page
  from issue 3.

---

## Documentation and project

### 21. ~~README setup assumes PowerShell~~ (fixed 2026-10-07)
- **Fixed:** Getting started now has separate blocks for Command Prompt (`activate.bat`),
  PowerShell (`Activate.ps1`, with the execution-policy hint) and macOS/Linux, plus the
  `python -m ghost_api` fallback.
- **Problem:** The setup steps use `.venv\Scripts\Activate.ps1`, which does nothing in
  Command Prompt (cmd). This confused a first run.
- **Fix:** Show both: `activate.bat` for cmd and `Activate.ps1` for PowerShell, plus the
  `python -m ghost_api` alternative.

### 22. ~~No demo GIF~~ (fixed 2026-10-07)
- **Fixed:** The README opens with a GIF of a real dashboard run on quotes.toscrape.com and
  has screenshots of the terminal UI, the dashboard (desktop and phone) and `report.html`
  (`docs/images/`).
- **Problem:** The last unticked item in the acceptance checklist.
- **Fix:** Record a `--headed` run on the demo shop.

### 23. Not published yet, and the user-agent has a TODO
- **Problem:** No GitHub repository yet; `ghost_api/config.py` has a TODO to add the
  project URL to the user-agent.
- **Fix:** Create the repository, push, and add the URL.

### 24. A `build/` folder is in the git history
- **Problem:** It was committed by mistake in one commit (`ecb4da9`) and removed in the next.
  It contains only a copy of the package, no secrets.
- **Fix:** Nothing needed, unless a clean history matters before publishing.

### 27. README images are recorded by hand
- **Problem:** The screenshots, the terminal SVG and the GIF in `docs/images/` were made with
  one-off Playwright and Rich scripts. When the UI changes they go stale, and nothing checks.
- **Fix:** Commit a `scripts/record_readme_media.py` that runs the demo shop or a fixed site,
  and re-run it before releases.

### 28. Found and fixed while recording the README (for the record)
Real runs found three bugs that the demo-shop tests had missed; each now has a test:
- With the AI agent on, the dashboard's live browser view stayed empty when the agent
  finished at step 1 (quotes.toscrape.com): the first screenshot was saved but never sent.
- On phones, long schema types (`array of array of integer | string`) pushed the endpoint
  panels wider than the screen.
- `ghost-api ui > log.txt` crashed on Windows: the log file's encoding (cp1252) can't hold
  the `◆` in the banner. The CLI now prints `?` for characters the output can't show.

### 29. Found and fixed from a run on Google Maps (for the record)
A real run on `https://www.google.com/maps` failed at once. Fixed, each with a test in
`tests/test_google_maps_findings.py`:
- Exploration stopped with `'utf-8' codec can't decode byte 0x8b`: a binary (compressed or
  protobuf) response was read as text. Binary bodies are now skipped and counted, and one
  unreadable response can no longer stop a run.
- Google puts `)]}'` before its JSON so other sites can't read it. Capture, replay and the
  generated Python and TypeScript clients now remove it. Responses that claim to be JSON
  but still don't parse (Google's `batchexecute` envelope) are dropped.
- The generated client did not compile when responses had deeply nested arrays of mixed
  types: merging union types split inside brackets and lost a `]`.
- A map-tile URL (`/maps/vt/pb=!1m8!3m7...`) became a 300-character method name. Segments
  that hold encoded data are now parameters (`/maps/vt/{pb}`), and names keep at most the
  last 3 path words.
- A "response is not JSON" failure was treated as a network error and retried after 20 s,
  because the word "Timeout" appears in every traceback (`httpx.Timeout`). It is now its own
  kind of failure, and network errors are matched by their exception names.
- Endpoint tables printed 9 levels of nested array types; labels now stop at 3 levels.

### 30. ~~One folder and memory for a whole domain~~ (fixed 2026-10-07)
- **Problem:** A run on `https://www.google.com/maps` was filed under `www.google.com`, so a
  later run on `/search` would have shared (and overwritten) its memory and files.
- **Fixed:** Results are kept per site: the host, plus the start path's first segment when it
  names a section (`www.google.com/maps`). See `ghost_api/site.py`. Data saved before this
  change stays under its old name and still works. In the dashboard a site's "/" is written
  as "~" in URLs (`/sites/www.google.com~maps`).

### 31. ~~"Required" header evidence could read like the opposite~~ (fixed 2026-10-07)
- **Problem:** The report said "header 'content-type' is required (without it: HTTP 200)".
  The reply without the header was HTTP 200 but a different kind of JSON (an error inside a
  200), and the evidence didn't say so.
- **Fixed:** The evidence now names the difference, e.g. "HTTP 200, but a different
  response: fields error, message instead of fields items, page".

### 32. Positional-array responses can't be repaired (Google Maps)
- **Problem:** Some Google endpoints (e.g. `/maps/preview/lp`) answer with nested arrays
  where each position's type changes from call to call. The model inferred from one sample
  fails on the next reply, and the verifier's repairs (fresh samples, relaxed fields) work on
  named fields, not array positions, so the endpoint ends up `failed`.
- **Found:** A no-LLM run on https://www.google.com/maps, 2026-10-07.
- **Fix:** When a list-of-union model still fails after the repairs, fall back to `Any` for
  that response (keep the endpoint, drop the typing), and say so in the report.

### 33. ~~Models too strict when samples come from one search~~ (fixed 2026-10-07)
- **Problem:** The Turf Town venue list was captured 4 times, all for one area. Every field
  seen in all 24 venues became required, and a venue in another area without `base_type`
  failed validation. The venue-details model (2 samples) failed for every other venue.
- **Fixed:** Fields are only generated as always present with enough evidence: replies to at
  least 2 different inputs (paging aside) across 3 replies, or 6 replies. Otherwise they may
  be missing. See `trust_required()` in `ghost_api/generator/spec.py`.

### 34. POST searches were never in the client (now: `--allow` and a dashboard switch)
- **Problem:** Many sites use POST for read-only searches (Turf Town's venue list). Ghost API
  rightly never decides by itself that a POST without a read word is safe, so no method was
  generated.
- **Now:** `ghost-api generate SITE --allow "POST /path"` includes an endpoint the user has
  checked only reads. Automatic detection is still open.

### 35. Leak check: names and addresses are only caught by field name
- **Problem:** Emails, phones, PAN/GST/Aadhaar, UPI, bank and card numbers are recognised by
  their shape. People's names and home addresses can't be: only fields named like
  `account_holder` or `beneficiary` are hidden. A name in a field called `contact_person`
  would be kept.
- **Fix:** More field-name hints, and an optional "strict" mode that hides every string in
  fields whose name suggests a person.

### 36. Watches compare everything except volatile-looking fields
- **Problem:** Fields that change on every visit but don't look volatile (a rotating
  "featured" flag, a random image order) make items look "changed" each time.
- **Workaround:** `--track price,name` limits what counts. **Fix:** learn volatile fields
  from two quick back-to-back fetches when a watch is created.

### 37. ntfy topics are public by name
- **Problem:** Anyone who guesses an ntfy.sh topic can read its messages. Ghost API's
  messages contain only item titles and changed values (personal data stays hidden), but
  the README and dashboard say to pick a long, unguessable topic.
- **Fix:** Support ntfy access tokens (`ntfy:topic?token=...`) or a self-hosted server
  (`GHOST_API_NTFY_URL`, already supported).

### 38. Third-party requests in the browser are not checked against their robots.txt
- **Problem:** The request gate checks robots.txt for page loads and the site's own API calls.
  Calls a page makes to other companies' servers (analytics, maps, CDNs) load as in any
  browser; only the private-address rule applies to them.
- **Why:** Ghost API didn't choose them, and fetching every third party's robots.txt would
  add traffic. Documented in SECURITY.md. **Fix if needed:** an option to block third-party
  API calls that robots.txt disallows (useful when the real API is on another domain, like
  Algolia for hn.algolia.com).

### 39. Direct requests resolve host names twice
- **Problem:** The browser's proxy connects to the exact address it checked. Direct requests
  (httpx: robots.txt, endpoint checks, fetch, watch) check the name, then httpx resolves it
  again, so a DNS-rebinding server could switch to a private address in between.
- **Mitigation:** endpoint checks never follow redirects; robots.txt redirects are checked per
  hop. **Fix:** an httpx transport that connects to the checked address (keeping the host
  name for TLS).

### 40. ~~No per-run permissions summary yet~~ (fixed 2026-10-16)
- **Fixed:** every run records a "Network and permissions" summary (hosts, robots.txt,
  rate limit, direct calls, blocked requests, saved login, LLM calls), shown in report.md,
  the terminal and `--json`. The HTML report shows it after the next frontend update.
- **Problem:** improvement.md asks for a summary of each run's hosts, third-party hosts,
  robots checks, request budgets and exceptions. Blocked requests and local target mode are
  in the run log, but there is no single summary in the report.
- **Fix:** collect the gate's and proxy's decisions into the run record and add a
  "Network and permissions" section to the report.

---

## Environment (not code)

### 25. Windows Application Control blocked dev tools
- **Problem:** For about a day, Windows blocked `ruff.exe`, part of mypy, and could block the
  `ghost-api.exe` launcher. It cleared on its own.
- **Workaround:** Use `python -m ghost_api`, `python -m ruff`, `python -m mypy`. If it comes
  back: Windows Security, App & browser control, Smart App Control.

### 26. Intermittent test stalls
- **Problem:** Twice, two tests stalled for about 19 minutes during full test runs. They are
  fast alone and in later full runs; not reproduced. Most likely the Windows policy above.
- **Mitigation:** `faulthandler_timeout = 300` in `pyproject.toml` prints a stack trace if
  a test runs over 5 minutes, so the next stall will show where it is stuck.
- **Seen again 2026-10-07:** a full run took 68 minutes instead of about 3 (all 409 tests
  passed). The stack trace was lost because only the last lines of output were kept; next
  time, run with `pytest -q 2>&1 | tee pytest.log` and read the trace.
