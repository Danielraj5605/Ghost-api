# Does Ghost API help? Measured

The obvious question: Claude Code can already read a website and write a client. What does
Ghost API add? We measured it on the local demo shop (`evals/demo_shop.py`): list products,
search, open a product with its reviews. Same goal, same model, October 2026.

## Finding the API

| | Claude Code alone | Claude Code + Ghost API (`/ghost-explore`) |
|---|---|---|
| Endpoints found | 5 of 5 | 5 of 5 |
| Each endpoint called and checked | its own tests (6 passing) | all 5 verified by direct calls, generated tests passing |
| Turns / cost | 7 turns, about $0.25 | 12 turns, about $0.27 |
| LLM key needed besides Claude | no | no |

**Honest result:** on a small site whose JavaScript is short and readable, Claude alone
finds the API just as well, a little faster. Ghost API's advantage is not finding endpoints
on easy sites. It is everything after.

## Keeping it working

| | Claude Code alone | With Ghost API |
|---|---|---|
| Re-check the live API later | a new session (and its cost) | `ghost-api diff --live`: 6.5 s, no LLM call |
| Tests in CI when the site is down | fail | replay recorded replies: 7 passed in 9 s, offline |
| What changed, and does it break my code? | read the diff yourself | classified: breaking / added, client version bumped, CHANGELOG |
| Pull request when the site changes | no | GitHub Action |
| A second developer, next month | starts over | memory, report, OpenAPI, versioned package |

## Rules and data

| | Claude Code alone | With Ghost API |
|---|---|---|
| robots.txt, rate limit | if the prompt says so | enforced for every request, browser included |
| Never reach your local network | not enforced | egress proxy, every redirect hop |
| Personal data in replies | seen, and may be repeated | hidden before anything is stored |
| Team rules (denied paths, terms checked, no AI) | no | `ghost-api.toml`, enforced everywhere |
| What was sent to the site | not recorded | audit log per site |

## Where it matters most

Sites whose API calls only show up at run time (large bundles, GraphQL with persisted
queries, calls built from configuration), sites behind a login, and any client you will
depend on for more than a day. On a site like the demo shop, use whichever you like; to
keep the result working, add Ghost API.

The quality gate (`python -m evals.run_eval --suite local --no-agent --check`) runs eleven
local test sites with known answers on every change; see the README's Evaluation section.
