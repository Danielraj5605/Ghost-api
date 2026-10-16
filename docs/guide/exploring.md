# Exploring a site

## With the AI agent

```bash
ghost-api run https://hn.algolia.com/ --goal "search stories and comments"
```

The agent clicks, types and scrolls toward your goal, one decision per step, while every
request the page makes is recorded. Budgets (`--max-steps`, `--max-llm-calls`,
`--max-minutes`) and stopping rules keep runs short. Watch it with `--headed`, or in the
dashboard (`ghost-api ui`).

## Without the agent

`--no-agent` loads the page and scrolls: enough for sites that load their data as you
scroll, and no LLM call is made.

## Sites that need a login

```bash
ghost-api login https://example.com/account      # log in yourself, press Enter
ghost-api run https://example.com/account --goal "my orders"
```

The session is saved per site (encrypted for your Windows account on Windows) and reused by
runs, refreshes, fetch, watch and serve. `ghost-api logout example.com` deletes it.

## Sites Ghost API can't browse by itself

Browse them yourself with DevTools open, use **Network → Save all as HAR**, then:

```bash
ghost-api import session.har --goal "my orders"     # --offline: send nothing to the site
```

Cookies, tokens and personal data are removed before anything is stored. Delete the `.har`
file afterwards.

## GraphQL

Each named operation (`GetUser`, `ListOrders`) becomes its own endpoint and client method,
with the query built in and the variables as typed arguments. Mutations are never replayed
or generated.

## Re-checking later

```bash
ghost-api refresh quotes.toscrape.com/scroll
```

A refresh replays stored requests first, visits the site once only if needed, and uses the
agent only for what is still missing.
