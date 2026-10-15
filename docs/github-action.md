# Keep a client up to date with GitHub Actions

When a site changes its API, Ghost API can notice, regenerate your client, bump its version
(major for breaking changes) and open a pull request that says exactly what changed.

## 1. Set up the repository once

```bash
ghost-api init
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors"
```

Commit `ghost-api.toml`, `output/` and the database in `.ghost-api/` (remove `.ghost-api/`
from `.gitignore` for this repository). The database holds captured traffic with cookies,
tokens and personal data already hidden; still, only commit it to a **private** repository
if the site needs a login.

## 2. Add the workflow

`.github/workflows/api-drift.yml`:

```yaml
name: API drift
on:
  schedule:
    - cron: "0 7 * * *"   # every day
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - id: ghost
        uses: Danielraj5605/Ghost-api@main
        with:
          site: quotes.toscrape.com/scroll
          mode: refresh         # or "check" to only report (no browser, nothing updated)
      - if: steps.ghost.outputs.changed == 'true'
        uses: peter-evans/create-pull-request@v6
        with:
          branch: ghost-api/quotes
          title: "API change on quotes.toscrape.com: client ${{ steps.ghost.outputs.version }}"
          body-path: ${{ steps.ghost.outputs.summary-file }}
          labels: ${{ steps.ghost.outputs.breaking != '0' && 'breaking-change' || 'api-update' }}
```

`mode: refresh` works like `ghost-api refresh`: it replays the stored requests first and only
opens the browser for endpoints that need fresh traffic, then regenerates the client and
records a new version when the API changed. Set `GEMINI_API_KEY` (or another key) as a
repository secret and pass it as `env:` to the step if the agent should be able to help.
`mode: check` only reports: one read-only request per endpoint, no browser, nothing written.
Commit the updated `output/` and `.ghost-api/` in the pull request (create-pull-request does).

## What the pull request says

```markdown
## API changes on quotes.toscrape.com/scroll

Client version **1.2.0 -> 2.0.0**: **1 breaking**

### Breaking
- `get_quotes total`: now string, was integer

### Added
- `get_quotes cursor`: new field in the reply
```

## The same checks without GitHub

```bash
ghost-api diff quotes.toscrape.com/scroll                # since the last client version
ghost-api diff quotes.toscrape.com/scroll --live         # call each endpoint once and compare
ghost-api diff quotes.toscrape.com/scroll --live --fail-on breaking   # exit 1 for CI
ghost-api diff quotes.toscrape.com/scroll --history      # every client version
```
