# Releasing Ghost API

Releases are built and published by GitHub Actions (`.github/workflows/release.yml`) when a
version tag is pushed. Nothing is published from a laptop.

## Once: set up PyPI trusted publishing

1. Create an account on [pypi.org](https://pypi.org) (and turn on two-factor login).
2. Go to **Your account → Publishing → Add a new pending publisher**:
   - PyPI project name: `ghost-api-cli`
   - Owner: `Danielraj5605`, repository: `Ghost-api`
   - Workflow: `release.yml`, environment: `pypi`
3. In the GitHub repository: **Settings → Environments → New environment** named `pypi`
   (optionally require your approval before each publish).

No token is stored anywhere: GitHub proves to PyPI which workflow is publishing.

## Each release

1. `ghost_api/__init__.py` has the new version, and `CHANGELOG.md` has a `## [X.Y.Z] - date`
   section describing it (move the "Unreleased" lines there).
2. CI is green on `main` (tests, quality gate, docs, wheel contents).
3. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. The workflow checks the tag matches the version, builds the wheel and sdist, writes an
   SBOM, publishes to PyPI with signed provenance, and creates the GitHub release with the
   changelog section, the files and the SBOM.
5. Check: `pipx install ghost-api-cli` in a fresh shell, then `ghost-api --version`.

## The documentation site (optional)

**Settings → Pages → Source: GitHub Actions**, then run the "Publish the documentation"
workflow (Actions tab). It builds `mkdocs.yml` in strict mode and deploys it.
