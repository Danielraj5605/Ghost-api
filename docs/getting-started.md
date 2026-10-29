# Getting started

You need Python 3.11+ and, for the AI agent, one free LLM key from
[Google AI Studio](https://aistudio.google.com/apikey), [Groq](https://console.groq.com/keys)
or [OpenRouter](https://openrouter.ai/keys). Without a key, runs still load and scroll pages
and map what they see (`--no-agent`).

## 1. Install

```bash
pipx install ghost-api-cli
# or:  uv tool install ghost-api-cli
```

## 2. Set up a folder

```bash
mkdir my-apis && cd my-apis
ghost-api init      # project file, Chromium (~150 MB, once), an optional key, .gitignore
ghost-api doctor    # checks Python, Chromium, the project and each key
```

`init` writes `ghost-api.toml`. Commands run in that folder or below use its database,
output folder and defaults:

```toml
[project]
database = ".ghost-api/ghost_api.db"
output = "output"

[defaults]          # any command option, by name
lang = "typescript"
rate_limit = 0.5
```

## 3. Explore a site

```bash
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors"
ghost-api           # or the interactive session: paste a site, say what you want
```

The result is in `output/<site>/`: the client, its tests, a package, `openapi.yaml` and
`report.html`.

## Scripts and CI

Add `--json` anywhere to get the result as JSON on stdout (progress goes to stderr). Exit
codes are stable: 0 done, 1 failed, 2 wrong usage or setup, 3 not allowed by the site,
130 stopped with Ctrl+C.
