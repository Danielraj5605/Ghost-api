# With Claude and other AI assistants

Claude Code can already browse a website. What it can't do on its own is turn that into
something you can keep: a **tested** map of the site's hidden API, a **typed client**, a
report with evidence, a version that **notices when the site changes**, all under rules
that keep it polite and safe. Ghost API gives your assistant exactly that, as tools.

```bash
cd my-project
ghost-api integrate claude-code     # or: cursor, vscode, desktop
```

Then, in Claude Code:

```text
/ghost-explore https://quotes.toscrape.com/scroll quotes with their authors and tags
```

**No LLM key is needed:** the assistant is the explorer. It reads each page (a numbered
list of elements) and decides what to click or type; Ghost API performs the action under
its rules, records every request the page sends, and maps the endpoints. When the goal is
covered, Ghost API tests every endpoint with direct calls and writes the client, report and
OpenAPI spec.

## What gets installed

| File | What it does |
|---|---|
| `.mcp.json` | Starts `ghost-api mcp --explore` (merged into your existing file) |
| `.claude/skills/ghost-api/SKILL.md` | Tells Claude when to use Ghost API and how to explore well |
| `.claude/commands/ghost-explore.md` | `/ghost-explore <url> <goal>` |
| `.claude/commands/ghost-data.md` | `/ghost-data <site> <what you want>`: rows from an explored site |
| `.claude/commands/ghost-diff.md` | `/ghost-diff <site>`: did the API change, and does it break the client |

## The tools

| Tool | Does |
|---|---|
| `ghost_explore_start(url, goal)` | Opens the site; returns the page, known endpoints and memory |
| `ghost_click`, `ghost_type`, `ghost_scroll`, `ghost_navigate`, `ghost_go_back` | One action each; reports new requests and new endpoints |
| `ghost_test_endpoint(endpoint, params)` | A direct, read-only call with other parameters (e.g. the next page) |
| `ghost_endpoints`, `ghost_page` | What has been mapped, and the current page |
| `ghost_explore_finish(goal_covered)` | Tests everything; writes the client, report, OpenAPI and a version |
| `ghost_sites`, `ghost_operations`, `ghost_fetch` | Data from sites explored before |
| `ghost_diff(site, live)`, `ghost_generate(site, lang)` | Change checks and client regeneration |

Answers are compact (a page is a short element list, endpoints are one line each), so an
exploration costs little context.

## The rules hold, whoever decides

The assistant can't make Ghost API leave the site, click logins, purchases or delete
buttons, ignore `robots.txt`, exceed the rate limit, reach your local network, or keep
personal data. Page text reaches the assistant inside a marked block it is told to treat as
data, never instructions. A bot-protection page ends the step with an explanation instead of
an attempt to get past it.

## Why not let the assistant just browse?

| | Assistant alone | Assistant + Ghost API |
|---|---|---|
| Result you can keep | What it writes when asked (e.g. a client file) | Typed client, package, offline tests, report, OpenAPI, version |
| Proof | Its own tests, if it writes them | Every endpoint called directly and checked before you see it |
| Next week | Starts from zero | Memory, versions, `ghost_diff` |
| Site changes | Nobody notices | `diff --live`, GitHub Action, new version |
| Safety | Depends on the prompt | Enforced rules (robots, rate limit, no writes, private network) |
| Personal data | Seen and repeated | Hidden before anything is stored |

Measured on the demo shop, including where Claude alone does just as well:
[Does it help? Measured](../benchmark.md).
