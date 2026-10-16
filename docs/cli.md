# Command reference

Generated from the command line (`python scripts/gen_cli_docs.py`). Every command
also accepts `--json` (result as JSON on stdout) and `--plain` (line-by-line output),
anywhere on the line. Exit codes: 0 done, 1 failed, 2 wrong usage or setup,
3 not allowed by the site, 130 stopped with Ctrl+C.

## `ghost-api adopt`

Find hand-written calls to websites' hidden APIs in a codebase, and the typed client
method that replaces each one (for sites Ghost API has explored). Nothing is edited.

```
ghost-api adopt PATH [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

## `ghost-api audit`

Every request Ghost API sent to a site itself, and every request its rules blocked
(method, host, path, status; never query values, bodies or cookies).

```
ghost-api audit DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--limit` | Show the last N entries. (default: `50`) |
| `--db` | SQLite database path. |

## `ghost-api diff`

What changed in a site's API since the last client version, and what it means.

```
ghost-api diff DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--live` | Also call each endpoint once and compare the reply with what was captured (no browser). |
| `--history` | List the client's versions. |
| `--since` | Changes recorded after this client version (e.g. after a refresh). |
| `--markdown` | Print a pull-request description. |
| `--fail-on` | Exit 1 on: breaking, any or never (for CI). (default: `never`) |
| `--db` | SQLite database path. |

## `ghost-api doctor`

Check your setup: Python, the browser, the project, the database and each LLM key
(one tiny call per provider).

## `ghost-api explain`

A plain-language summary of a site's API for someone new to it (no LLM).

```
ghost-api explain DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

## `ghost-api fetch`

Download a site's data as CSV, Excel or JSON, following all pages. No code needed.

```
ghost-api fetch DOMAIN OPERATION INPUTS [OPTIONS]
```

| Option | What it does |
|---|---|
| `--format` | csv, xlsx or json. (default: `csv`) |
| `--pages` | Maximum pages to follow. (default: `5`) |
| `--max-rows` | Stop after this many rows. (default: `1000`) |
| `--save-to` | File to write (default: in output/). |
| `--include-sensitive` | Keep personal data (emails, phones, IDs). Only for auditing your own site. |
| `--out` | Folder for generated files. (default: `output`) |
| `--db` | SQLite database path. |

## `ghost-api forget`

Delete what Ghost API stored about a site: runs, captured traffic, endpoints,
watches, screenshots and its output folder.

```
ghost-api forget DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--llm-cache` | Also clear cached LLM answers (all sites). |
| `--keep-files` | Keep the output folder and screenshots. |
| `--yes`, `-y` | Don't ask for confirmation. |
| `--out` | Output directory. (default: `output`) |
| `--db` | SQLite database path. |

## `ghost-api generate`

(Re)generate the client for a site from its stored endpoint map.

```
ghost-api generate DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--out` | Folder for generated clients. (default: `output`) |
| `--no-llm` | Use names derived from URLs (no LLM call). |
| `--lang` | Client language: python or typescript. (default: `python`) |
| `--db` | SQLite database path. |
| `--allow` | An endpoint you checked only reads data, e.g. "POST /api/search". Ghost API skips POSTs without a read word in their URL; this includes one anyway. Repeatable. |

## `ghost-api import`

Map a site's API from a HAR file you recorded in your browser (no AI, no browser).

For sites behind a login or a bot check: browse them yourself with DevTools open
(Network tab), "Save all as HAR", then import. Cookies, tokens and personal data are
removed before anything is stored. Delete the .har file afterwards: it holds them.

```
ghost-api import HAR [OPTIONS]
```

| Option | What it does |
|---|---|
| `--goal`, `-g` | What you were doing (for the report). (default: `imported from a HAR file`) |
| `--url` | The site's start URL (default: the first page in the file). |
| `--offline` | Send nothing to the site: no endpoint tests, no client tests. |
| `--lang` | Client language: python or typescript. (default: `python`) |
| `--rate-limit` | Max requests per second the tool sends itself. (default: `1.0`) |
| `--max-probes` | Maximum direct API calls for testing endpoints. (default: `60`) |
| `--out` | Folder for generated clients. (default: `output`) |
| `--db` | SQLite database path. |

## `ghost-api init`

Set up Ghost API here: project file, browser, LLM key, .gitignore. Safe to re-run.

| Option | What it does |
|---|---|
| `--lang` | Default client language: python or typescript. (default: `python`) |
| `--yes`, `-y` | Don't ask: install the browser, skip the key. |
| `--skip-browser` | Don't download Chromium. |

## `ghost-api inspect`

Show what's stored for a site, or replay one run with --run.

```
ghost-api inspect DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--run` | Replay one run's step log and test results. |
| `--db` | SQLite database path. |

## `ghost-api integrate`

Add Ghost API to an AI assistant for this project: the MCP server (explore sites,
fetch data, check changes), and for Claude Code a skill and /ghost-* commands.

```
ghost-api integrate TARGET [OPTIONS]
```

| Option | What it does |
|---|---|
| `--path` | The project folder. (default: `.`) |
| `--dry-run` | Show what would be written, write nothing. |

## `ghost-api login`

Log in to a site yourself, once; Ghost API reuses the session.

A browser window opens: log in as you normally would (Ghost API never sees your
password), then press Enter here. Runs and refreshes then start logged in, and fetch,
watch and serve send the session's cookies. Remove it with `ghost-api logout SITE`.

```
ghost-api login URL [OPTIONS]
```

| Option | What it does |
|---|---|
| `--list` | Show saved logins. |
| `--db` | SQLite database path. |

## `ghost-api logout`

Delete a site's saved login.

```
ghost-api logout DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

## `ghost-api mcp`

Offer Ghost API to AI assistants (MCP over stdio), e.g. for Claude Code:
claude mcp add ghost-api -- ghost-api mcp --explore      (explore any site)
claude mcp add ghost -- ghost-api mcp turftown.in/chennai  (one tool per operation)

```
ghost-api mcp SITES [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |
| `--explore` | Ghost API as a toolset: the assistant explores new sites (no LLM key needed) and uses explored ones (fetch, diff, generate). |
| `--out` | Folder for generated clients. (default: `output`) |

## `ghost-api mock`

Serve a local copy of a site's API from captured replies: work offline, test
front ends, never touch the real site.

```
ghost-api mock DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--port` | Port on this computer. (default: `8901`) |
| `--db` | SQLite database path. |

## `ghost-api refresh`

Re-check a known site using memory: replay first, browse only if needed.

Also continues an interrupted run, since its traffic and endpoints were saved.

```
ghost-api refresh DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--headed` | Show the browser window. |
| `--max-steps` | Agent steps, if the agent is needed. (default: `10`) |
| `--max-llm-calls` | Maximum LLM calls. (default: `20`) |
| `--rate-limit` | Max requests per second the tool sends itself. (default: `1.0`) |
| `--ignore-robots` | Skip the robots.txt check. |
| `--max-probes` | Maximum direct API calls for testing endpoints. (default: `60`) |
| `--no-verify` | Don't run the generated tests against the API. |
| `--out` | Folder for generated clients. (default: `output`) |
| `--no-llm` | Never call the LLM (no agent, URL-based names). |
| `--third-party-robots` | Also check the page's API calls to other sites against their robots.txt. |
| `--lang` | Client language: python or typescript. (default: `python`) |
| `--db` | SQLite database path. |

## `ghost-api report`

Write report.md, report.html and openapi.yaml for a site from what is stored.

```
ghost-api report DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--run` | Run to report on (default: the latest). |
| `--out` | Folder for generated files. (default: `output`) |
| `--db` | SQLite database path. |

## `ghost-api run`

Explore a website with the AI agent and map its hidden API.

```
ghost-api run URL [OPTIONS]
```

| Option | What it does |
|---|---|
| `--goal`, `-g` | What the client should do. |
| `--lang` | Client language: python or typescript. (default: `python`) |
| `--headed` | Show the browser window. |
| `--max-steps` | Maximum agent actions. (default: `40`) |
| `--max-llm-calls` | Maximum LLM calls. (default: `50`) |
| `--max-minutes` | Time limit in minutes. (default: `15.0`) |
| `--rate-limit` | Max requests per second the tool sends itself. (default: `1.0`) |
| `--ignore-robots` | Skip the robots.txt check. |
| `--db` | SQLite database path. |
| `--no-agent` | Don't use the LLM; just load and scroll the page. |
| `--no-probe` | Don't test endpoints by calling them directly. |
| `--max-probes` | Maximum direct API calls for testing endpoints. (default: `60`) |
| `--no-generate` | Don't generate a client. |
| `--no-verify` | Don't run the generated tests against the API. |
| `--third-party-robots` | Also check the page's API calls to other sites against their robots.txt. |
| `--out` | Folder for generated clients. (default: `output`) |

## `ghost-api serve`

Serve sites as a clean local REST API (for Excel, scripts, any language).

```
ghost-api serve SITES [OPTIONS]
```

| Option | What it does |
|---|---|
| `--port` | Port on this computer. (default: `8900`) |
| `--db` | SQLite database path. |

## `ghost-api ui`

Open the web dashboard: start runs, watch the agent live, browse every result.

| Option | What it does |
|---|---|
| `--port` | Port for the local dashboard. (default: `8800`) |
| `--no-open` | Don't open the browser automatically. |
| `--out` | Folder for generated clients. (default: `output`) |
| `--db` | SQLite database path. |
| `--no-watch` | Don't check due watches while open. |

## `ghost-api verify`

Regenerate a site's client, run its tests against the live API, and repair.

Uses stored data only. Endpoints that need a key are tested when the key is set in
the environment (the generated client says which variable).

```
ghost-api verify DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--out` | Folder for generated clients. (default: `output`) |
| `--lang` | Client language: python or typescript. (default: `python`) |
| `--db` | SQLite database path. |

## `ghost-api verify-spec`

Compare a published OpenAPI spec with what the site really does: undocumented
endpoints, documented ones never seen, and reply fields that differ.

```
ghost-api verify-spec SPEC_FILE DOMAIN [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

## `ghost-api watch`

Watch a site for new, changed or removed items, and get alerts.

### `ghost-api watch add`

Start watching an endpoint. The first check records what's there now.

```
ghost-api watch add DOMAIN OPERATION INPUTS [OPTIONS]
```

| Option | What it does |
|---|---|
| `--every` | How often to check: 30m, 6h, 1d... (default: `6h`) |
| `--notify` | ntfy:TOPIC, webhook:URL or desktop. Repeatable. |
| `--track` | Only these fields count as changes, e.g. price,name (default: all). |
| `--pages` | Pages to read on each check. (default: `5`) |
| `--no-check` | Don't take the first snapshot now. |
| `--db` | SQLite database path. |

### `ghost-api watch changes`

Show recent changes found by your watches.

```
ghost-api watch changes WATCH_ID [OPTIONS]
```

| Option | What it does |
|---|---|
| `--limit` | How many to show. (default: `30`) |
| `--db` | SQLite database path. |

### `ghost-api watch list`

Show your watches.

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

### `ghost-api watch remove`

Stop watching (removes its history too).

```
ghost-api watch remove WATCH_ID [OPTIONS]
```

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |

### `ghost-api watch run`

Check watches once now.

```
ghost-api watch run IDS [OPTIONS]
```

| Option | What it does |
|---|---|
| `--due` | Only watches that are due. |
| `--db` | SQLite database path. |

### `ghost-api watch schedule`

Run due watches in the background (Windows Task Scheduler), even with no window open.

| Option | What it does |
|---|---|
| `--every` | How often Windows wakes Ghost API up. (default: `30m`) |
| `--remove` | Remove the scheduled task. |
| `--dry-run` | Show the command without running it. |
| `--db` | SQLite database path. |

### `ghost-api watch start`

Keep checking due watches until you press Ctrl+C.

| Option | What it does |
|---|---|
| `--db` | SQLite database path. |
