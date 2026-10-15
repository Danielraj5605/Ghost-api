# Ghost API CLI — Complete Issues Audit (Functional + Non-Functional + UI/UX)

After a thorough review of every source file, here are **28 issues** found, now including **11 UI/UX issues** that make the tool feel rough compared to polished CLIs like Claude Code.

---

## 🔴 Functional Issues (7)

### 1. ✅ Rate limits forgotten between runs
**File:** [gateway.py](file:///e:/workspace/ai-project/ghost_api/llm/gateway.py)  
Cooldowns live in memory only. Retries a just-rate-limited provider on the next run.

### 2. ✅ Memory keeps only latest run's endpoints
**File:** [knowledge.py](file:///e:/workspace/ai-project/ghost_api/memory/knowledge.py)  
Endpoints from earlier runs are lost during refresh/generate.

### 3. ✅ Robots.txt only checked for start host
**File:** [net.py](file:///e:/workspace/ai-project/ghost_api/net.py#L81-L84)  
Navigation to subdomains is unchecked.

### 4. ✅ `_version` callback leaks value into signature
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L66-L69)  
Missing `expose_value=False` on the version option.

### 5. ✅ Agent `goal_covered` can be too generous
**File:** [orchestrator.py](file:///e:/workspace/ai-project/ghost_api/agent/orchestrator.py#L155-L158)  
Declares goal covered without validating the `missing` field is empty.

### 6. ✅ Config user-agent has a TODO placeholder
**File:** [config.py](file:///e:/workspace/ai-project/ghost_api/config.py#L14-L16)  
`USER_AGENT_SUFFIX` has a TODO to add the project URL.

### 7. ✅ REPL `_interactive` doesn't re-check providers after `.env` changes
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L118-L129)  
`has_llm` check runs once and never updates.

---

## 🟡 Non-Functional Issues (10)

### 8. ✅ No graceful error on corrupt/old database schema
**File:** [store.py](file:///e:/workspace/ai-project/ghost_api/memory/store.py)

### 9. ✅ `safe_output` doesn't handle `sys.stdout` being `None`
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L94-L100)

### 10. ✅ `_check_lang` validation duplicated in `Settings.__post_init__`
**Files:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L158-L163), [config.py](file:///e:/workspace/ai-project/ghost_api/config.py#L48-L52)

### 11. ✅ `errors.py` timeout regex matches too broadly
**File:** [errors.py](file:///e:/workspace/ai-project/ghost_api/errors.py#L30-L31)

### 12. ✅ `watch_start` enters infinite loop with zero watches
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L775-L785)

### 13. ✅ `schedule_command` doesn't quote paths with spaces
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L788-L796)

### 14. ✅ `EventBus.emit` swallows errors silently
**File:** [events.py](file:///e:/workspace/ai-project/ghost_api/events.py#L186-L189)

### 15. ✅ Missing `__all__` exports in key modules
**Files:** Multiple `__init__.py` files

### 16. ✅ `_find_url` regex can match email fragments
**File:** [repl.py](file:///e:/workspace/ai-project/ghost_api/ui/repl.py#L33-L35)

### 17. ✅ `friendly_error` fallback loses root cause context
**File:** [errors.py](file:///e:/workspace/ai-project/ghost_api/errors.py#L45-L52)

---

## 🟣 UI/UX Issues (11) — *Making it feel like Claude Code*

### 18. ✅ No feedback when a command starts running
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py) — `run`, `refresh`, `fetch`, `verify` commands  
**Problem:** After typing a command, there's a blank pause while providers load, the browser launches, etc. Claude Code shows an immediate "thinking" indicator the moment you press Enter. Ghost API should print a "Starting..." line immediately, before any expensive work.  
**Fix:** Print an immediate `⏳ Starting...` line in `run`, `refresh`, and `fetch` before doing setup.

### 19. ✅ No elapsed-time display in commands other than `run`
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py) — `refresh`, `verify`, `fetch`, `doctor`  
**Problem:** `run` shows time via the stepper, but `fetch`, `verify`, `doctor`, and `refresh` don't show how long they took. Users are left guessing if something hung.  
**Fix:** Print elapsed time at the end of these commands.

### 20. ✅ Welcome panel shows raw `cwd` path — feels technical
**File:** [repl.py](file:///e:/workspace/ai-project/ghost_api/ui/repl.py#L261-L276)  
**Problem:** The welcome panel prints `cwd: E:\workspace\ai-project` which is noisy. Claude Code's welcome is clean, showing only what the user needs to get started.  
**Fix:** Drop the `cwd` line (or make it part of `/help` debug info). Add a short "What's new" or provider status instead.

### 21. ✅ Summary card shows `$0.00` always — misleading/confusing
**File:** [terminal.py](file:///e:/workspace/ai-project/ghost_api/ui/terminal.py#L353-L354)  
**Problem:** The status line and summary card hardcode `$0.00` for cost. This looks like a bug or a broken feature, not a deliberate free-tier indicator. If cost tracking isn't implemented, don't show a price.  
**Fix:** Remove the `$0.00` display. Optionally replace with "(free tier)" label.

### 22. ✅ File links show full `file:///` URIs — wall of noise
**File:** [terminal.py](file:///e:/workspace/ai-project/ghost_api/ui/terminal.py#L95-L101)  
**Problem:** Report links show `file:///E:/workspace/ai-project/output/quotes.toscrape.com/report.html` which is very long, hard to read, and breaks the visual flow. Claude Code uses short, clean paths.  
**Fix:** Show a relative path (e.g., `output/quotes.toscrape.com/report.html`) as the visible text, but keep the `file:///` URI as the clickable link.

### 23. ✅ `doctor` output doesn't suggest what to do next
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L906-L923)  
**Problem:** When `doctor` shows all providers as missing, it says "No provider is working" but doesn't show *how* to add a key. A first-run user is stuck.  
**Fix:** Add a line like `Add GEMINI_API_KEY=your-key to a .env file. Get one free at ...`.

### 24. ✅ `inspect` with no domain shows too much — no summary
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L926-L974)  
**Problem:** `ghost-api inspect` dumps a raw table of every run, with no summary counts. Compare to `claude /status` which gives a clean, digestible overview.  
**Fix:** Add a summary line above the table: "3 sites explored, 14 endpoints across 5 runs."

### 25. ✅ Error messages on wrong commands don't offer closest match reliably
**File:** [repl.py](file:///e:/workspace/ai-project/ghost_api/ui/repl.py#L149-L156)  
**Problem:** `/hep` shows "Unknown command /hep." — the "Did you mean" heuristic only checks the first 3 chars, so `/he` would match but `/hep` wouldn't match `/help` (it checks `[:3]` = `/he`, which does match `/help`). But many typos fail, e.g. `/opn` → `[:3]` = `/op` which matches `/open` but `/fetc` → `[:3]` = `/fe` which matches `/fetch`. The heuristic isn't consistent.  
**Fix:** Use proper Levenshtein distance (or at least `startswith(name[:len(name)-1])` for the full typed prefix).

### 26. ✅ No empty-state guidance in `fetch` and `watch list`
**File:** [cli.py](file:///e:/workspace/ai-project/ghost_api/cli.py#L552-L621)  
**Problem:** When there are no operations for a site, the message is "Nothing here can be fetched without the browser." — but it doesn't say *why* or *what to do*. Same for empty watch lists.  
**Fix:** Explain: "This site's endpoints use authentication or change data, so Ghost API won't call them directly. Use `--allow 'POST /api/search'` if you know an endpoint only reads data."

### 27. ✅ Ctrl+C stop message is too terse — no context
**File:** [repl.py](file:///e:/workspace/ai-project/ghost_api/ui/repl.py#L407-L410)  
**Problem:** When a run is interrupted, the user sees only `Stopped.` with no context about partial results. Claude Code shows what was accomplished before the interruption.  
**Fix:** Print something like "Stopped after step 6 of 40. Found 3 endpoints so far — results saved."

### 28. ✅ After a run, the REPL prompt gives generic hints — not contextual
**File:** [repl.py](file:///e:/workspace/ai-project/ghost_api/ui/repl.py#L135-L147)  
**Problem:** `_after_run()` always shows the same hints (`/fetch to download · /watch add for alerts`). If no endpoints were found, suggesting `/fetch` is misleading. Should adapt to the result.  
**Fix:** Show contextual hints based on what happened: if no endpoints → suggest re-running with different goal. If endpoints found → suggest `/fetch`. If errors → suggest `/doctor`.

---

## 📋 Fix Order (by impact → effort)

| # | Issue | Impact | Effort |
|---|---|---|---|
| 18 | No "starting" feedback | High (feels broken) | Low |
| 21 | Hardcoded `$0.00` | High (looks buggy) | Low |
| 22 | Full file:/// URIs | High (noisy) | Low |
| 20 | Raw cwd in welcome | Medium (unprofessional) | Low |
| 23 | Doctor no next-step hint | Medium (stuck users) | Low |
| 19 | No elapsed time on commands | Medium (feels slow) | Low |
| 28 | Generic post-run hints | Medium (misleading) | Low |
| 27 | Terse Ctrl+C message | Medium (lost context) | Low |
| 24 | Inspect no summary | Medium (overwhelming) | Low |
| 25 | Command typo matching | Low (minor annoyance) | Low |
| 26 | No empty-state guidance | Medium (stuck users) | Medium |
| 4 | Version callback | Low | Low |
| 6 | User-agent TODO | Low | Low |
| 17 | Error root cause context | Medium | Low |
| 11 | Timeout regex too broad | Low | Low |
| 9 | safe_output None guard | Low | Low |
| 14 | EventBus error surfacing | Medium | Medium |
| 5 | goal_covered validation | Medium | Medium |
| 12 | watch_start empty loop | Low | Low |
| 16 | URL regex email match | Low | Low |
| 7 | REPL env re-check | Low | Low |
| 10 | Lang validation dup | Low | Low |
| 13 | schedule_command quoting | Low | Low |
| 15 | Missing `__all__` | Low | Low |
| 8 | DB corruption handling | Medium | Medium |
| 1 | Rate limit persistence | High | High |
| 2 | Memory endpoint merging | High | High |
| 3 | Robots.txt per host | Medium | High |
