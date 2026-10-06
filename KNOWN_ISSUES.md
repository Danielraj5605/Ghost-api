# Known issues

Problems found while building, testing and using Ghost API, ordered by priority. Each has
what goes wrong, how it was found, and a suggested fix.

**Priority:** P1 = safety or wrong results, fix first · P2 = makes sites or features fail ·
P3 = quality and polish · Env = outside the code.

Last updated: 2026-10-07.

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

### 10. No change detection
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

### 21. README setup assumes PowerShell
- **Problem:** The setup steps use `.venv\Scripts\Activate.ps1`, which does nothing in
  Command Prompt (cmd). This confused a first run.
- **Fix:** Show both: `activate.bat` for cmd and `Activate.ps1` for PowerShell, plus the
  `python -m ghost_api` alternative.

### 22. No demo GIF
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
