# Quote Book: a small project made easy by Ghost API

**What it is:** a script that downloads every quote from
[quotes.toscrape.com](https://quotes.toscrape.com/scroll) (a practice website made for this
kind of thing) and turns them into a searchable page you can open offline.

## Run it

From the project folder, with the virtual environment active:

```bat
cd examples\quote_book
python quote_book.py
```

You'll see:

```
Downloaded 100 quotes by 50 authors.

Top authors: Albert Einstein (10), J.K. Rowling (9), Marilyn Monroe (7), Dr. Seuss (6), Mark Twain (6)
Top tags:    love (14), inspirational (13), life (13), humor (12), books (11), ...

Saved quote_book.html: open it in your browser (works offline).
```

Then open `quote_book.html`: search by words or author, and click a tag to filter.

## Where Ghost API helped

The website has no official API and no documentation. Getting its data normally means
one of these:

| Without Ghost API | With Ghost API |
|---|---|
| Open the browser's developer tools, watch the network traffic, find the hidden `/api/quotes?page=N` call, work out its parameters and its reply, write the code to call it and walk through the pages. About an hour or more. | One command: `ghost-api run https://quotes.toscrape.com/scroll --goal "list quotes with their authors and tags"`. It wrote `quotes_client.py` and `models.py` (the files in this folder), tested them, and documented them. About 2 minutes. |
| Or copy text from the page's HTML, which breaks when the design changes. | The client uses the same data the site itself uses. |

So this project's own code only needs one line to get all the data:

```python
for page in QuotesClient().iter_list_quotes():  # Ghost API wrote this
    ...  # you only write what to do with it
```

Everything else in `quote_book.py` is the project itself: counting, picking a random quote
and making the page. That's the point of Ghost API: **you skip the detective work and go
straight to building what you wanted.**

The two client files are copied here from `output/quotes.toscrape.com/` so the example works
on its own. To make them again: run the command above, then copy the files.
