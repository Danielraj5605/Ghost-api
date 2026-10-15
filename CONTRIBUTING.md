# Contributing to Ghost API

Thanks for helping. Bug reports, site results (good or bad) and pull requests are welcome.

## Set up

```bash
git clone https://github.com/Danielraj5605/Ghost-api
cd Ghost-api
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,mcp]" -c requirements.lock
playwright install chromium
```

No LLM key is needed for the tests: the agent is driven by scripted fakes.

## Before a pull request

```bash
ruff check . && ruff format ghost_api tests
mypy ghost_api            # strict
pytest -q                 # about 10 minutes; tests/test_netguard.py and test_console.py drive a real browser/console
cd web && npm ci && npm run typecheck && npm run lint && npm test && npm run build   # if you changed web/
```

If you change the frontend, commit the rebuilt `ghost_api/web/static` and
`ghost_api/export/viewer` files too; CI checks that they match the source.

## Ground rules

- Ghost API respects `robots.txt`, rate limits and bot protection. Changes that work around
  them won't be merged.
- Never commit real tokens, keys or personal data. Tests build fake secrets at run time
  (`tests/fake_secrets.py`).
- Every fix gets a test that fails without it. Local test servers only: no live sites in
  the normal test suite.
- Messages people see are short, plain and say what to do next.
- Add a line to `CHANGELOG.md` under "Unreleased".

## Security problems

Please don't open a public issue; see [SECURITY.md](SECURITY.md).
