# Security and data handling

Ghost API is a local tool: it runs on your computer, drives a browser on your computer, and
keeps what it learns in files on your computer. This page says exactly what it collects,
what leaves your machine, how long things are kept, and how to delete them.

## What Ghost API collects

| What | Where it is kept | Notes |
|---|---|---|
| Runs: the URL, your goal, status, timings | `ghost_api.db` (SQLite) | |
| Captured API traffic: method, URL, status, request and reply bodies (JSON only) | `ghost_api.db` | Cookies, tokens, API keys and passwords are replaced with `[REDACTED]` before storing. Personal data (emails, phones, PAN/GST/Aadhaar, UPI, bank and card numbers) is replaced with `[hidden ...]`. Bodies are cut at 500,000 characters; replies over 5 MB are not read at all |
| Endpoints, schemas, test results, lessons | `ghost_api.db` | Derived from the traffic above |
| The agent's steps and reasoning, run events | `ghost_api.db` | Shown again by `ghost-api inspect --run N` and the dashboard |
| Cached LLM answers | `ghost_api.db` (`llm_cache`) | They contain text from the pages that were explored |
| Browser screenshots | `ghost_api_files/runs/<run>/` next to the database | Show the pages as the browser saw them |
| Reports, OpenAPI, generated clients, downloads | `output/<site>/` | Built from the stored data |
| Watches and what changed | `ghost_api.db` | |
| Command history of the interactive session; provider cooldowns | `~/.ghost-api/` (or `GHOST_API_HOME`) | No page content |

**None of these files are encrypted by Ghost API.** Anyone who can read your user folder can
read them. Use disk encryption (BitLocker, FileVault, LUKS) if that matters to you, and do
not commit `ghost_api.db`, `ghost_api_files/` or `output/` to a public repository (the
project's `.gitignore` already excludes them).

Ghost API never stores passwords. When a site needs a login, you log in yourself in the
browser window; the session cookies stay in that browser profile, which is thrown away at the
end of the run.

## What leaves your machine

- **To the website you explore:** what any browser sends when you open it, plus the direct
  requests Ghost API makes to check endpoints (read-only, at most one per second by default).
  Every request carries an honest user-agent: `GhostAPI/<version> (API discovery tool;
  +https://github.com/Danielraj5605/Ghost-api)`, plus your `GHOST_API_CONTACT` if you set it.
- **To the LLM provider you configured** (Gemini, Groq or OpenRouter), only when the AI agent
  or naming step runs: your goal, a text summary of the current page (title, visible text,
  buttons and links), the endpoints found so far, lessons from earlier runs, and short
  samples of API replies for naming. Bearer tokens and JWTs are masked first. **Visible page
  text is sent as it is**, so do not run the agent on pages showing data you may not share
  with that provider. `--no-agent` and `generate --no-llm` send nothing to any provider.
- **Notifications you set up** for watches (ntfy.sh, a webhook): the change summary you asked
  for. Personal data stays hidden there too.
- Nothing else. Ghost API has no telemetry and sends no usage data anywhere.

## Network boundary

A website could try to use Ghost API to reach your own computer or local network, by
redirecting the browser or making its page call addresses such as `127.0.0.1`,
`192.168.x.x` or the cloud metadata address `169.254.169.254`.

- All browser traffic goes through a small local proxy (`ghost_api/browser/egress.py`) that
  checks every connection, **redirects included**, and refuses loopback, private,
  link-local, multicast and other non-public addresses. It connects to the exact address it
  checked, so a host name can't be switched to a private address in between.
- Direct requests (robots.txt, endpoint checks, fetch, watch, serve) are checked on every
  redirect hop. They resolve the name again when connecting, which leaves a very small
  window for DNS rebinding; endpoint checks never follow redirects at all.
- **Local target mode:** when the site you give Ghost API is itself on your computer or
  network (`localhost`, `127.0.0.1`, a LAN address), private addresses are allowed for that
  run, because you asked for them. The run log says so.
- The browser's page loads and the site's own API calls follow `robots.txt` and the rate
  limit before they are sent; blocked requests never reach the server and are listed in the
  run log. Images, scripts and styles load as in any browser, and third-party calls
  (analytics, maps, CDNs) are not checked against their own `robots.txt`.

## Keeping and deleting data

Ghost API keeps everything until you delete it.

```bash
ghost-api forget quotes.toscrape.com          # one site: database rows, screenshots, output folder
ghost-api forget quotes.toscrape.com --keep-files   # only the database rows
ghost-api forget --llm-cache                  # cached LLM answers for all sites
```

`forget` asks before deleting (`--yes` skips the question) and compacts the database so the
data is really gone from the file. Copies you made elsewhere (generated clients you copied,
downloaded spreadsheets) are not touched. To delete everything, remove `ghost_api.db`,
`ghost_api_files/` and `output/`.

## The dashboard, REST API and MCP server

`ghost-api ui`, `ghost-api serve` and `ghost-api mcp` are for **one person on one machine**.
`ui` and `serve` listen on `127.0.0.1` only and reject requests addressed to any other host
name; `mcp` talks only to the program that started it, over stdin/stdout. None of them has a
login, user accounts or per-user separation. Do not expose them to a
network or put them behind a public proxy. A shared or hosted version would need
authentication, per-user storage and secrets, isolated browser workers and audit logs first.

## Supported versions

Only the latest release on the `main` branch gets security fixes.

## Reporting a vulnerability

Please report security problems privately through GitHub: on the repository page, open
**Security → Report a vulnerability**. Do not open a public issue for them. Include the
version (`ghost-api --version`), what you did, and what happened. You should get an answer
within a week.

## Release checklist (security)

- [ ] `ruff`, `mypy --strict` and the full test suite pass, including `tests/test_netguard.py`
- [ ] No real tokens, keys or personal data in the repository (fake values in tests are built at
      run time, see `tests/fake_secrets.py`)
- [ ] Dependencies updated from `requirements.lock`, and `pip-audit` shows no known issue
- [ ] The packaged dashboard and report assets match the frontend source (CI checks this)
- [ ] README and this page still describe what is collected and sent
