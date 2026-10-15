# Improvement Audit and Company-Scale Roadmap

This file records code-review findings and product improvements that are not already
tracked in [KNOWN_ISSUES.md](KNOWN_ISSUES.md) or
[ghost_api_issues_and_fixes.md](ghost_api_issues_and_fixes.md). It complements the feature
plan in [IMPLEMENTATION.md](IMPLEMENTATION.md); it does not replace the existing issue log.

The findings below come from static review of the Python backend, browser/capture path,
generated clients, dashboard, and project configuration. Tests and live-site behavior were
not run as part of this review, so each item should be confirmed with a focused regression
case before implementation.

## Findings to address first

### P1 — Browser traffic can bypass robots and request-rate policy

- **Evidence:** `ghost_api/net.py` applies `RateLimiter` in `fetch_robots()` and
  `ghost_api/probe/replay.py` calls it before direct replays. `BrowserSession` in
  `ghost_api/browser/driver.py` loads pages and lets page scripts issue requests without
  either check. `ghost_api/capture/recorder.py` filters and records responses after they
  have happened; it does not control them.
- **Impact:** A page can issue many API calls in parallel or request a path disallowed by
  robots.txt. The README describes runs, watches, and servers as following these limits, so
  behavior and user expectations can diverge.
- **Recommendation:** Add a browser-context request gate before requests are sent. Apply
  host/path robots policy and a shared per-host scheduler to tool-driven navigation and
  requests. Make policy decisions visible in the run log. If third-party traffic cannot be
  governed safely, document the narrower policy clearly and exclude it from claims of
  rate-limited crawling.
- **Acceptance:** A local test site that emits parallel requests and a disallowed API path
  must show that blocked requests never reach the test server and allowed requests obey the
  configured per-host interval.

### P1 — Redirects and page-initiated requests need an explicit network boundary

- **Evidence:** `BrowserSession.goto()` uses normal browser navigation, which follows HTTP
  redirects. `Explorer` checks the resulting URL after an action and can go back, but that
  check is after the redirect request. The CLI and dashboard accept arbitrary HTTP(S) URLs.
- **Impact:** A public URL can redirect the local browser to a different host, including
  loopback, link-local, or private-network addresses. A page can also trigger requests to
  destinations outside the intended site. This creates an SSRF and local-network exposure
  surface for users who scan untrusted URLs.
- **Recommendation:** Validate every navigation target and redirect before following it;
  validate resolved IP addresses and re-check after DNS resolution to reduce rebinding risk.
  Gate browser subrequests as well. Keep localhost available for the documented local demo
  and tests through an explicit local-target mode or narrow development setting.
- **Acceptance:** Cover redirects to another public host, `127.0.0.1`, IPv6 loopback,
  private ranges, link-local metadata addresses, and a hostname that resolves to a private
  address. Verify the blocked destination receives no request.

### P1 — Capture limits are applied after the full response is buffered

- **Evidence:** `TrafficRecorder._build()` calls `response.body()` and decodes the entire
  body before applying `MAX_BODY_CHARS`. `Replayer.send()` similarly reads `resp.text` before
  parsing or redacting it.
- **Impact:** A site can return a very large JSON response and consume substantial memory
  before the existing truncation limit takes effect. Repeated large responses can slow or
  crash a run and exceed the modest-machine resource target.
- **Recommendation:** Enforce byte limits while streaming, with separate limits for
  compressed and decompressed payloads. Stop reading once the budget is exceeded, record a
  clear `response-too-large` outcome, and put a total capture/storage budget on each run.
- **Acceptance:** Large and highly compressed responses remain within a measured memory
  budget, are not partially treated as valid schemas, and produce an understandable report.

### P2 — TypeScript browser clients cannot reliably set their declared User-Agent

- **Evidence:** `ghost_api/generator/templates/client.ts.j2` says the client supports
  browsers and always sends a `User-Agent` header. Browser Fetch treats this as a forbidden
  request header; browsers may silently ignore it. Browser-origin calls can also be blocked
  by the target site's CORS policy.
- **Impact:** Generated clients may behave differently in Node and browsers, and users may
  believe the identifying header is present when it is not.
- **Recommendation:** Make the supported runtime explicit per generated client. Set the
  User-Agent only in runtimes that permit it, warn that browser calls require target CORS
  support, and provide a small server-side proxy example for browser applications.
- **Acceptance:** Document and typecheck the Node and browser examples separately; test the
  Node header and ensure browser output does not promise an un-settable header.

## Company-scale improvements

These are strategic investments rather than claims that the current local-first design is
broken. A large, trusted developer-tools company needs stronger operational guarantees,
security boundaries, and evidence than a local prototype.

### 1. Make policy, privacy, and permissions auditable

- Write a security model that names data collected, what is sent to each LLM provider, what
  stays local, and the retention period for SQLite records, reports, screenshots, and logs.
- Add retention and purge controls, plus a clear way to remove a site's stored data. Document
  that local SQLite files and generated artifacts are not encrypted by the application.
- Add a per-run permissions summary: target hosts, third-party hosts observed, whether
  robots restrictions were checked, request budgets, and any user-approved exceptions.
- Provide a responsible disclosure contact, supported-version policy, and a security review
  checklist for releases.

### 2. Move from local-only execution to an explicit deployment model

- The dashboard and REST/MCP integrations are local tools; they do not currently provide
  tenant isolation, user identity, or a job queue. Do not expose them as a shared service
  without authentication, authorization, quotas, and isolation around browser processes,
  files, databases, and provider keys.
- If teams need hosted or multi-user use, define a separate service architecture: authenticated
  API, per-tenant storage and secrets, durable queued jobs, worker sandboxes, cancellation,
  concurrency quotas, audit logs, and backup/restore procedures.
- Keep the CLI useful without a hosted account, and make any hosted mode's data flows
  opt-in and explicit.

### 3. Turn evaluations into release gates

- The README currently reports four evaluation sites; the existing issue log already calls
  out the small benchmark. Expand this into a versioned corpus with known answers, including
  adversarial redirect, robots, prompt-injection, huge-response, authentication, and
  pagination cases.
- Track endpoint precision/recall, schema validity, generated-client success, policy
  violations, latency, token usage, and peak memory by release. Add regression thresholds
  to CI, and label live-site results with the date and environment.
- Use synthetic/local fixtures for deterministic CI; keep external live-site evaluations as
  a separate scheduled job so site changes do not make normal builds flaky.

### 4. Make installs and releases reproducible

- `pyproject.toml` uses open-ended minimum dependency versions. Add a tested lock or
  constraints file for development and release builds, while keeping the library's supported
  dependency ranges intentional.
- Add CI for supported Python versions and operating systems, frontend typecheck/lint/build,
  package installation from the built artifact, and generated-client compatibility.
- Automate frontend asset synchronization into the packaged `ghost_api/web/static` directory
  and report viewer. Verify that a wheel contains the same dashboard/report assets as the
  source build.
- Publish versioned release notes and artifacts, produce an SBOM, and automate dependency
  and vulnerability review.

### 5. Improve operational observability without collecting user content

- Define stable run outcome and error codes rather than relying on display strings. Include
  bounded diagnostics for network policy denials, provider failures, parsing errors, and
  verification failures.
- Add opt-in, privacy-preserving diagnostics (counts and timings only) if maintainers need
  product health data. Never enable content or URL telemetry by default.
- Add database backup/export and restore guidance, storage growth visibility, and health
  checks for long-running watch/serve processes.

## Suggested delivery order

1. Add regression fixtures for request gating, redirects/private addresses, and oversized
   responses; fix the P1 items before recommending scans of untrusted targets.
2. Correct TypeScript runtime claims and add browser-versus-Node usage guidance.
3. Establish reproducible CI and package-asset verification.
4. Publish the security/data-retention model and deletion controls.
5. Design hosted/team mode only after its identity, isolation, and operational requirements
   are specified.
