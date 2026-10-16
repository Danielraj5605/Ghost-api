# Data without code

```bash
ghost-api fetch quotes.toscrape.com/scroll                       # what can be fetched
ghost-api fetch quotes.toscrape.com/scroll get_quotes --pages 10 --format xlsx
```

## Watches and alerts

```bash
ghost-api watch add quotes.toscrape.com/scroll get_quotes --every 6h --notify ntfy:my-topic
ghost-api watch start            # or: ghost-api watch schedule (Windows Task Scheduler)
```

A watch reports what is new, changed or gone: to the console, your phone (ntfy), a webhook
or the desktop.

## From other tools

```bash
ghost-api serve                              # a local REST API, http://127.0.0.1:8900/docs
ghost-api mcp quotes.toscrape.com/scroll     # tools for Claude and other AI assistants
ghost-api mock quotes.toscrape.com/scroll    # an offline copy of the API, from captured replies
```

All of these only read data, follow the same rules as exploring (robots.txt, one request
per second per site), and keep personal data hidden.
