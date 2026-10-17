# To do

What is left, in order. Last updated: 2026-10-17 (after v0.2.0 was prepared, CI green).

**Who:** 🙋 = needs you (an account, a decision or a click) · 🤖 = Claude can do it.

---

## 1. Release v0.2.0 (blocking: nothing else ships without these)

- [ ] 🙋 **Decide: public or private repository.** Developers can't find or trust a private
      tool, and the docs website on a private repository needs a paid plan. All keys,
      including dummy ones, are already removed from the whole history.
      Make it public: `gh repo edit --visibility public --accept-visibility-change-consequences`
      (or Settings → General → Danger Zone → Change visibility).
- [ ] 🙋 **PyPI trusted publishing.** On pypi.org (with two-factor login on):
      Your account → Publishing → Add a new pending publisher:
      project `ghost-api-cli`, owner `Danielraj5605`, repository `Ghost-api`,
      workflow `release.yml`, environment `pypi`.
      (The GitHub `pypi` environment already exists.)
- [ ] 🤖 **Tag and release** (`git tag v0.2.0 && git push origin v0.2.0`), then watch the
      release workflow: PyPI upload, signed provenance, GitHub release with the changelog.
      This workflow has not run yet with the updated (Node 24) actions.
- [ ] 🤖 **Check the published package:** `pipx install ghost-api-cli` in a fresh shell,
      `ghost-api --version` shows 0.2.0, `ghost-api doctor` works.
- [ ] 🙋🤖 **Docs website** (after the repository is public): Settings → Pages →
      Source: GitHub Actions, then run "Publish the documentation". Address:
      `https://danielraj5605.github.io/Ghost-api/`. Then add it to the repository's
      "Website" field and the README.

## 2. Launch (after the release)

- [ ] 🤖 Draft a short launch post (what it is, the honest benchmark, the gallery) for
      r/Python, r/webscraping and Show HN. 🙋 post it.
- [ ] 🙋 Repository topics and description on GitHub (e.g. `api`, `reverse-engineering`,
      `mcp`, `claude`, `playwright`, `openapi`).
- [ ] 🤖 Submit the MCP server to MCP server lists once it's on PyPI.
- [ ] 🤖 After October 19, 2026 GitHub moves `ubuntu-latest` to Ubuntu 26: check the next
      CI run (`gh run view --log-failed` if it fails).

## 3. Finish the improvement plan (not built yet)

- [ ] 🤖 `ghost-api contract check SITE`: replay each verified endpoint's recorded
      contract live (one request each) and report what no longer holds (feeds `diff`).
- [ ] 🤖 Evidence view per endpoint in report.html: the request, reply shape, hypotheses
      tested and their results, last verified date.
- [ ] 🤖 `ghost-api export SITE --openapi-generator go` (and other languages) through
      openapi-generator or Kiota when installed, on top of the verified OpenAPI spec.
- [ ] 🤖 Re-run the benchmark (docs/benchmark.md) on more sites and more repeats, so the
      numbers mean more than one example each.

## 4. Known issues still open (details in KNOWN_ISSUES.md)

**Wrong or failed results first**

- [ ] 🤖 #2 "Goal covered" can be too generous: map each part of the goal to an endpoint
      field before accepting `covered`.
- [ ] 🤖 #4 Generated clients don't send the user's contact (`GHOST_API_CONTACT`), so sites
      like Wikipedia refuse them. Read it from the environment at run time, never write it
      into the code.
- [ ] 🤖 #3 Show the agent's `ask_user` questions with a warning that they come from the
      agent (page text could have put words in its mouth).
- [ ] 🤖 #6 Path segments seen with one value stay fixed (`/indexes/Item_dev/query`):
      test whether a related value gives the same reply shape.
- [ ] 🤖 #7 Free model names go stale: `doctor` lists the provider's current models and
      suggests a working one.
- [ ] 🤖 #32 Replies that are nested arrays with changing types (Google Maps): fall back to
      an untyped reply for that endpoint and say so in the report.
- [ ] 🤖 #34 Read-only POST searches need `--allow`: detect them automatically when safe.
- [ ] 🤖 #12 WebSocket traffic is not captured (GraphQL is supported now).

**Quality and polish**

- [ ] 🤖 #13 Destructive-button words are English-only (add German, French, Spanish,
      Hindi...; use form method and button type too).
- [ ] 🤖 #15 Secret redaction by name and pattern only: also flag long random-looking
      strings, and let users add their own sensitive names.
- [ ] 🤖 #35 Personal names and addresses are only hidden by field name: more hints and a
      strict mode.
- [ ] 🤖 #16 Only the first 60 page elements reach the agent: rank by relevance first.
- [ ] 🤖 #17 Test bigger page sizes (`limit=100`) so clients need fewer calls.
- [ ] 🤖 #18 TypeScript clients don't check replies at run time: optional zod schemas.
- [ ] 🤖 #19 Confidence scores use hand-picked weights: re-weight from evaluation results.
- [ ] 🤖 #20 The evaluation is small: add more permissive sites with known answers.
- [ ] 🤖 #36 Watches: learn which fields change on every visit from two quick fetches.
- [ ] 🤖 #37 ntfy access tokens for private notification topics.
- [ ] 🤖 #27 A script that re-records the README screenshots and GIF before each release.

**Housekeeping**

- [ ] 🤖 Update KNOWN_ISSUES.md: #8 (cooldowns are saved now), #9 (endpoints are merged
      across runs), #11 (`ghost-api login`), #12 (GraphQL part), #14 (robots.txt per host)
      and #23 (repository exists) are done or partly done but not marked.
- [ ] 🤖 Old planning notes (`improvement.md`, `ghost_api_issues_and_fixes.md`,
      `IMPLEMENTATION.md`): fold what is still useful into this file and KNOWN_ISSUES.md,
      then remove them so newcomers read one list.

## 5. Not code (keep in mind)

- Windows Application Control once blocked dev tools (KNOWN_ISSUES #25): use
  `python -m ruff`, `python -m mypy`, `python -m ghost_api` if it comes back.
- On this laptop, run the full test suite in 4 parts (one run uses too much memory):
  `ls tests/test_*.py > files.txt`, then `pytest -q $(split -n l/1/4 files.txt)` and the
  same with 2/4, 3/4 and 4/4 (Git Bash).
- Never commit `.env`, `ghost_api.db`, `ghost_api_files/` or `output/`. Fake keys in tests are
  built at run time (`tests/fake_secrets.py`).
