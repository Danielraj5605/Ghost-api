# Changelog

All notable changes to Ghost API. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/): until 1.0, a minor version
(0.**x**.0) may change commands or output; a patch version (0.x.**y**) only fixes things.

## [Unreleased]

## [0.2.0] - 2026-10-29

### Added
- docs/gallery.md: real runs on public practice sites, including one with no JSON API.
- `--third-party-robots`: the page's API calls to other sites are checked against those
  sites' robots.txt too (closes KNOWN_ISSUES #38 as an option).
- docs/benchmark.md: Claude Code alone vs with Ghost API, measured (including where Claude
  alone does as well). `diff` labels changes from the live API `[live]`, apart from changes
  in the generated client.
- Team rules per site in `ghost-api.toml` (`require_terms`, `terms_ok`, `deny_paths`,
  `max_direct_calls`, `agent`), enforced for runs, refreshes, imports, assistants, fetch,
  watch and serve; an audit log of every request sent or blocked (`ghost-api audit SITE`).
- Local LLMs (Ollama, LM Studio...: `GHOST_API_LOCAL_URL`): nothing goes to a cloud provider.
- Direct requests connect to the exact address that was checked (no DNS-rebinding window).
- `ghost-api adopt PATH`: finds hand-written calls to hidden APIs in a codebase and the
  client method that replaces each one (JSON for an assistant to apply).
- `ghost-api verify-spec SPEC SITE`: a published OpenAPI spec compared with reality
  (undocumented endpoints, documented ones never seen, reply fields that differ).
- `ghost-api explain SITE`: a plain-language summary of a mapped API. Shell completion
  (`--install-completion`).
- Generated tests run offline by default from recorded replies (`cassettes.json`, secrets
  redacted, personal data hidden), in Python and TypeScript; `GHOST_API_LIVE=1` tests the
  real API. Verification always runs live.
- **Ghost API for AI assistants:** `ghost-api mcp --explore` lets Claude (or any MCP
  client) be the explorer, with no LLM key: it reads each page and decides, Ghost API acts
  under its rules, maps and tests the API, and writes the client, report and version.
  Library tools: sites, operations, fetch, diff, generate.
- `ghost-api integrate claude-code|cursor|vscode|desktop`: the MCP server, a Claude Code
  skill and `/ghost-explore`, `/ghost-data`, `/ghost-diff` commands, merged into the project.
- `ghost-api init`: sets up a folder in one step (project file, Chromium, an optional LLM
  key, `.gitignore`). Safe to run again.
- Project settings in `ghost-api.toml`: the project's own database and output folder, and
  default options for every command, found from any subfolder.
- `--json` on every command that has a result (run, refresh, inspect, generate, verify,
  report, fetch, watch list, doctor, forget, init): JSON on stdout, progress on stderr.
- Documented exit codes: 0 OK, 1 failed, 2 usage or setup error, 3 not allowed by the site,
  130 stopped with Ctrl+C.
- `ghost-api doctor` checks the whole setup: Python, Chromium, project, database, LLM keys.
- `.env` is also read from `~/.ghost-api/.env`, for installed copies.
- Network boundary: a local egress proxy refuses private addresses on every connection the
  browser makes, redirects included; robots.txt and the rate limit apply to the browser's
  page loads and API calls; replies over 5 MB are not read. See SECURITY.md.
- `ghost-api forget SITE` deletes everything stored about a site.
- SECURITY.md, CI (Linux and Windows, Python 3.11 and 3.13, frontend, wheel contents),
  `requirements.lock`, stable error codes.

- Generated clients: an async Python client next to the sync one, typed errors (auth, not
  found, rate limit, server) in Python and TypeScript, and installable packages in
  `package/python` and `package/typescript` with a README and curl examples. Python output
  passes `mypy --strict`.
- `ghost-api mock SITE`: a local copy of a site's API from captured replies.
- API drift: client versions (semantic versioning: breaking -> major, added -> minor),
  recorded by runs, refreshes and `generate`, with a CHANGELOG.md in each package.
  `ghost-api diff` (since the last version, `--since`, `--live`, `--history`, `--markdown`,
  `--fail-on`), and a GitHub Action (`action.yml`) that refreshes a site and reports what
  changed for a pull request.

- `ghost-api import FILE.har`: map an API from a HAR file recorded in your own browser
  (sites behind a login or a bot check). Same filters as live capture: secrets redacted and
  personal data hidden before storing; `--offline` sends nothing to the site.
- GraphQL: each named operation is its own endpoint and client method, with the variables
  as typed arguments. Mutations (also inside batches, and persisted queries whose name
  doesn't say they read) are never replayed or generated.
- `ghost-api login URL`: log in yourself once; the session is saved (DPAPI-encrypted on
  Windows) and used by runs, refreshes, fetch, watch and serve. `logout`, `login --list`;
  `forget` deletes it too.
- A Python API (`ghost_api.api`): discover, refresh, generate, operations, fetch, diff,
  sites; the CLI and the API share the same workflows (`ghost_api.workflows`).
- Every run records a "Network and permissions" summary (report, terminal, `--json`).
- A quality gate: ten local test sites with known answers (paging, auth headers, robots
  rules, huge replies, XSSI, no API, bot walls, personal data, prompt injection), scored in CI
  against `evals/thresholds.json`; a weekly live evaluation.

### Changed
- The package is published as `ghost-api-cli` (the name `ghost-api` was taken on PyPI).
  The command is still `ghost-api`.
- A `.env` found outside the current folder or project no longer sets `GHOST_API_DB`.
- TypeScript clients are documented for Node 18+; they no longer try to set User-Agent in
  browsers.
- Endpoints found by earlier runs are kept until a refresh finds them gone.
- The interactive session's welcome panel shows the ghost from the favicon as the CLI's avatar
  (half-block characters in the brand colour).
- One flat brand colour (royal blue) across the docs site, dashboard, report, favicon and
  terminal; the purple gradient logo and header glow are gone. The dashboard heading no
  longer says "any website", and the sparkles icons are replaced by plain ones.
- Docs site: favicon (SVG, ICO, Apple touch icon), system fonts (no request to Google Fonts),
  no "Made with" footer credit, and footer links to the new privacy policy and terms pages.
- Docs site: a new home page built from one real run (the recorded request, the checks that
  proved it, the generated models), the measured comparison with an AI assistant alone, and
  stronger headings on every page.
- Dashboard: the home page puts the run form and your sites side by side, sites as a compact
  list; run statuses use honest colours (limits and "no progress" in amber, not green).
- Dashboard: a sidebar for Sites, Watches and Settings on wide screens, and summary tiles on
  the home page (sites, endpoints found, verified endpoints, runs).

### Fixed
- Cursor paging parameters are typed as text (cursors are opaque, even when numeric).
- Tests no longer read the developer's `.env` or call real LLM providers.
- Cursor paging was missed when the newest captured page was the last one (found by the
  quality gate).
- Bot-protection pages are detected without the agent too (`--no-agent`).
- The 28 issues in `ghost_api_issues_and_fixes.md`.

## [0.1.0] - 2026-10-15

First public version: AI exploration in a real browser, traffic capture and analysis,
endpoint testing, Python and TypeScript clients with tests, verification and repair,
reports, OpenAPI, the web dashboard, memory and refresh, fetch, watches, REST and MCP
servers, and the personal-data leak check.

[Unreleased]: https://github.com/Danielraj5605/Ghost-api/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Danielraj5605/Ghost-api/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Danielraj5605/Ghost-api/releases/tag/v0.1.0
