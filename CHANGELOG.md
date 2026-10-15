# Changelog

All notable changes to Ghost API. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/): until 1.0, a minor version
(0.**x**.0) may change commands or output; a patch version (0.x.**y**) only fixes things.

## [Unreleased]

### Added
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

### Fixed
- Cursor paging was missed when the newest captured page was the last one (found by the
  quality gate).
- Bot-protection pages are detected without the agent too (`--no-agent`).
- The 28 issues in `ghost_api_issues_and_fixes.md`.

## [0.1.0] - 2026-10-15

First public version: AI exploration in a real browser, traffic capture and analysis,
endpoint testing, Python and TypeScript clients with tests, verification and repair,
reports, OpenAPI, the web dashboard, memory and refresh, fetch, watches, REST and MCP
servers, and the personal-data leak check.

[Unreleased]: https://github.com/Danielraj5605/Ghost-api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Danielraj5605/Ghost-api/releases/tag/v0.1.0
