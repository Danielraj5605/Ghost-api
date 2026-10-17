---
title: Ghost API
hide:
  - navigation
  - toc
---

<div class="gh-home" markdown>

<section class="gh-hero" markdown>

<p class="gh-eyebrow">Open source · MIT · runs on your computer</p>

# Find a website's hidden API. Get a typed, tested client.

Most websites load their data from their own JSON API. Ghost API opens the site in a real
browser, records those requests, proves how they behave with direct calls, and writes a Python
or TypeScript client for them.

```bash
pipx install git+https://github.com/Danielraj5605/Ghost-api
ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors"
```

[Get started](getting-started.md){ .md-button .md-button--primary }
[See real runs](gallery.md){ .md-button }

</section>

<section class="gh-section" markdown>

## One real run, start to finish

From a run on quotes.toscrape.com, a public practice site. Nothing below is edited except
for length.

<div class="gh-steps" markdown>

<div class="gh-step" markdown>

<span class="gh-step__label">1. Recorded in the browser</span>

The page loads more quotes as you scroll. Behind it is one request to the site's own
server:

```http
GET /api/quotes?page={page}
```

</div>

<div class="gh-step" markdown>

<span class="gh-step__label">2. Proved with direct calls</span>

From the run's report, "Hypotheses tested":

- confirmed: can be called directly, outside the browser (HTTP 200, 10 records)
- confirmed: paginates with page parameter `page` (the next page returned 10 different
  records)
- confirmed: needs none of its custom headers or cookies

</div>

<div class="gh-step" markdown>

<span class="gh-step__label">3. Written as a client</span>

Models are inferred from real replies, then the client's own tests run against the live API:

```python
class Author(_Model):
    goodreads_link: str
    name: str
    slug: str

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

## Why not just ask an AI assistant?

We measured it. On a small site with readable JavaScript, Claude Code alone found all 5
endpoints in 7 turns; with Ghost API it took 12 turns, at a similar cost. Finding endpoints
on easy sites is not where Ghost API helps. Keeping the result working is:

<div class="gh-facts" markdown>

<div markdown>
**Re-check the live API later**

`ghost-api diff --live` in 6.5 s, with no LLM call.
</div>

<div markdown>
**Tests in CI when the site is down**

Recorded replies are replayed: 7 tests passed in 9 s, offline.
</div>

<div markdown>
**Know what changed**

Changes are classified as breaking or added, and the client version is bumped.
</div>

<div markdown>
**Rules for every request**

`robots.txt`, rate limits and team rules are enforced, and each request Ghost API sends is logged.
</div>

</div>

[Read the full comparison](benchmark.md)

</section>

<section class="gh-section" markdown>

## Use it from your assistant

Claude Code, Cursor, VS Code and Claude Desktop can drive Ghost API as a set of tools: the
assistant decides where to click, and Ghost API records, checks and remembers. No second LLM
key is needed.

```bash
ghost-api integrate claude-code
```

[Set up an assistant](guide/assistants.md)

</section>

<section class="gh-section" markdown>

## Polite by design

- Follows `robots.txt` and limits its own request rate.
- Stops at bot checks and never tries to get past them. When a site needs a login, you log
  in yourself in the browser window.
- Never sends requests that change data on a site.
- Hides personal data (emails, phone numbers, card and ID numbers) before storing anything.
- Refuses to let a website steer it into your own network.
- Sends no telemetry. Apart from the site you explore, data goes only to the AI provider and
  notification service you choose, if any.

[Security and data](security.md) lists exactly what is collected and what leaves your machine.

</section>

</div>
