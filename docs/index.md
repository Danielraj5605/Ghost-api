---
title: Ghost API
hide:
  - navigation
  - toc
---

<div class="gh-home" markdown>

<section class="gh-hero" markdown>

[Version 0.2.0 is on PyPI](https://pypi.org/project/ghost-api-cli/){ .gh-badge }

# Find a website's hidden API. Get a typed, tested client.

Most websites load their data from their own JSON API. Ghost API opens the site in a real
browser, records those requests, proves how they behave with direct calls, and writes a
Python or TypeScript client for them.

<div class="gh-hero__install" markdown>

```bash
pipx install ghost-api-cli
```

</div>

<div class="gh-actions" markdown>

[Get started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/Danielraj5605/Ghost-api){ .md-button }

</div>

<p class="gh-meta">Python 3.11+ · MIT licence · runs on your computer</p>

</section>

<figure class="gh-shot" markdown>

![The Ghost API dashboard showing the verified /api/quotes endpoint on quotes.toscrape.com](images/home-dashboard-light.webp#only-light){ width="1920" height="1200" }
![The Ghost API dashboard showing the verified /api/quotes endpoint on quotes.toscrape.com](images/home-dashboard-dark.webp#only-dark){ width="1920" height="1200" }

<figcaption>The local dashboard after a real run on quotes.toscrape.com: one endpoint, its
parameters and reply shape, verified by two live tests.</figcaption>

</figure>

<section class="gh-section" markdown>

<p class="gh-kicker">How it works</p>

## One real run, start to finish

From a run on quotes.toscrape.com, a public practice site. Nothing below is edited except
for length.

```bash
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors"
```

<div class="gh-steps" markdown>

<div class="gh-card" markdown>

<span class="gh-card__step">1</span>

### Recorded in the browser

The page loads more quotes as you scroll. Behind it is one request to the site's own
server:

```http
GET /api/quotes?page={page}
```

</div>

<div class="gh-card" markdown>

<span class="gh-card__step">2</span>

### Proved with direct calls

From the run's report, "Hypotheses tested":

- Called directly, outside the browser: HTTP 200, 10 records.
- Paginates with `page`: the next page returned 10 different records.
- Needs none of its custom headers or cookies.

</div>

<div class="gh-card" markdown>

<span class="gh-card__step">3</span>

### Written as a client

Models are inferred from real replies, then the client's own tests run against the live
API:

```python
class Quote(_Model):
    author: Author
    tags: list[str]
    text: str
```

</div>

</div>

```python
from quotes_toscrape_client import QuotesToscrapeClient

with QuotesToscrapeClient() as api:
    for page in api.iter_get_quotes(max_pages=3):
        print(page.quotes[0].author.name)
```

</section>

<section class="gh-section" markdown>

<p class="gh-kicker">Measured, not claimed</p>

## Why not just ask an AI assistant?

On a small site with readable JavaScript, Claude Code alone found all 5 endpoints in 7 turns;
with Ghost API it took 12 turns, at a similar cost. Finding endpoints on easy sites is not
where Ghost API helps. Keeping the result working is.

<div class="gh-grid" markdown>

<div class="gh-card" markdown>

### Re-check in 6.5 seconds

`ghost-api diff --live` compares the live API with the recorded one, with no LLM call.

</div>

<div class="gh-card" markdown>

### Tests that run offline

Recorded replies are replayed in CI: 7 tests passed in 9 seconds, offline.

</div>

<div class="gh-card" markdown>

### Know what changed

Changes are classified as breaking or added, and the client version is bumped to match.

</div>

<div class="gh-card" markdown>

### Rules for every request

`robots.txt`, rate limits and team rules are enforced, and every request it sends is
logged.

</div>

</div>

[Read the full comparison](benchmark.md){ .gh-link }

</section>

<section class="gh-section gh-split" markdown>

<div markdown>

<p class="gh-kicker">Assistants</p>

## Use it from your assistant

Claude Code, Cursor, VS Code and Claude Desktop can drive Ghost API as a set of tools. The
assistant decides where to click; Ghost API records, checks and remembers. No second LLM key
is needed.

```bash
ghost-api integrate claude-code
```

[Set up an assistant](guide/assistants.md){ .gh-link }

</div>

<div markdown>

<p class="gh-kicker">Safety</p>

## Polite by design

- Follows `robots.txt` and limits its own request rate.
- Stops at bot checks. When a site needs a login, you log in yourself.
- Never sends requests that change data on a site.
- Hides personal data (emails, phone, card and ID numbers) before storing anything.
- Refuses to let a website steer it into your own network.
- Sends no telemetry.

[What is collected and what leaves your machine](security.md){ .gh-link }

</div>

</section>

<section class="gh-cta" markdown>

## Try it on a practice site

<div class="gh-hero__install" markdown>

```bash
pipx install ghost-api-cli
ghost-api init
```

</div>

[Read the guide](getting-started.md){ .md-button .md-button--primary }
[See real runs](gallery.md){ .md-button }

</section>

</div>
