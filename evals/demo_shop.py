"""A small single-page shop whose API is known exactly, for evaluating Ghost API.

Run it on its own with:  python -m evals.demo_shop --port 8765

The API (ground truth):
    GET  /api/categories                       categories list
    GET  /api/products?page=&per_page=&category=   page-based pagination
    GET  /api/products/{id}                    product detail
    GET  /api/products/{id}/reviews?cursor=    cursor-based pagination
    GET  /api/search?q=                        search
    POST /api/cart                             changes data: must never be generated
Noise the tool must ignore:
    POST /collect          analytics beacon
    GET  /api/health       health check
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

CATEGORIES = ["books", "games", "music"]
PRODUCTS: list[dict[str, Any]] = [
    {
        "id": i,
        "name": f"{CATEGORIES[i % 3].title()} item {i}",
        "category": CATEGORIES[i % 3],
        "price": round(4.5 + i * 1.25, 2),
        "in_stock": i % 4 != 0,
        "description": None if i % 5 == 0 else f"A fine {CATEGORIES[i % 3]} product.",
    }
    for i in range(1, 31)
]
REVIEWS = {
    p["id"]: [{"id": p["id"] * 100 + r, "stars": 1 + (p["id"] + r) % 5, "text": f"Review {r}"}
              for r in range(1, 8)]
    for p in PRODUCTS
}  # fmt: skip

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Demo Shop</title>
<style>body{font-family:sans-serif;margin:2em} .card{border:1px solid #ccc;padding:.5em;margin:.3em;
display:inline-block;width:12em} nav button{margin-right:.5em}</style></head><body>
<header><h1>Demo Shop</h1><a href="/logout">Log out</a></header>
<nav id="cats"></nav>
<p><input id="q" type="search" placeholder="Search products"></p>
<main id="list"></main>
<p><button id="more">Load more</button></p>
<section id="detail"></section>
<script>
let page = 1, category = null;
fetch('/api/health');
fetch('/collect', {method: 'POST', body: 'view'});
async function loadCats() {
  const cats = await (await fetch('/api/categories')).json();
  document.getElementById('cats').innerHTML = cats.categories
    .map(c => `<button onclick="pickCategory('${c.slug}')">${c.name}</button>`).join('');
}
async function loadProducts(reset) {
  if (reset) { page = 1; document.getElementById('list').innerHTML = ''; }
  const qs = new URLSearchParams({page, per_page: 6});
  if (category) qs.set('category', category);
  const data = await (await fetch('/api/products?' + qs)).json();
  render(data.products);
  document.getElementById('more').style.display = data.has_more ? '' : 'none';
}
function render(items) {
  document.getElementById('list').insertAdjacentHTML('beforeend', items.map(p =>
    `<div class="card"><a href="#" onclick="openProduct(${p.id});return false">${p.name}</a>
     <div>$${p.price}</div></div>`).join(''));
}
function pickCategory(c) { category = c; loadProducts(true); }
document.getElementById('more').onclick = () => { page += 1; loadProducts(false); };
document.getElementById('q').addEventListener('keydown', async e => {
  if (e.key !== 'Enter') return;
  const data = await (await fetch('/api/search?q=' + encodeURIComponent(e.target.value))).json();
  document.getElementById('list').innerHTML = '';
  render(data.results);
});
let reviewCursor = null, current = null;
async function openProduct(id) {
  current = id; reviewCursor = null;
  const p = await (await fetch('/api/products/' + id)).json();
  document.getElementById('detail').innerHTML = `<h2>${p.name}</h2><p>${p.description || ''}</p>
    <button onclick="addToCart(${p.id})">Add to cart</button>
    <h3>Reviews</h3><ul id="reviews"></ul>
    <button id="morerev" onclick="loadReviews()">More reviews</button>`;
  loadReviews();
}
async function loadReviews() {
  const qs = reviewCursor ? '?cursor=' + reviewCursor : '';
  const data = await (await fetch(`/api/products/${current}/reviews${qs}`)).json();
  document.getElementById('reviews').insertAdjacentHTML('beforeend',
    data.reviews.map(r => `<li>${r.stars}★ ${r.text}</li>`).join(''));
  reviewCursor = data.next_cursor;
  document.getElementById('morerev').style.display = reviewCursor ? '' : 'none';
}
async function addToCart(id) {
  await fetch('/api/cart', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({product_id: id, qty: 1})});
}
loadCats(); loadProducts(true);
</script></body></html>"""


class ShopHandler(BaseHTTPRequestHandler):
    calls: list[str] = []  # every request, for checking what the tool did

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, text: str, ctype: str, status: int = 200) -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        ShopHandler.calls.append(f"GET {url.path}")
        parts = url.path.strip("/").split("/")
        if url.path in ("/", "/index.html"):
            self._text(PAGE, "text/html; charset=utf-8")
        elif url.path == "/robots.txt":
            self._text("User-agent: *\nDisallow: /logout\n", "text/plain")
        elif url.path == "/api/health":
            self._json({"ok": True})
        elif url.path == "/api/categories":
            self._json({"categories": [{"slug": c, "name": c.title()} for c in CATEGORIES]})
        elif url.path == "/api/products":
            items = [p for p in PRODUCTS if not q.get("category") or p["category"] == q["category"]]
            page, size = int(q.get("page", "1")), min(int(q.get("per_page", "6")), 20)
            chunk = items[(page - 1) * size : page * size]
            self._json({"page": page, "per_page": size, "total": len(items),
                        "has_more": page * size < len(items), "products": chunk})  # fmt: skip
        elif len(parts) == 3 and parts[:2] == ["api", "products"] and parts[2].isdigit():
            product = next((p for p in PRODUCTS if p["id"] == int(parts[2])), None)
            self._json(product or {"error": "not found"}, 200 if product else 404)
        elif len(parts) == 4 and parts[:2] == ["api", "products"] and parts[3] == "reviews":
            reviews = REVIEWS.get(int(parts[2]) if parts[2].isdigit() else -1)
            if reviews is None:
                self._json({"error": "not found"}, 404)
                return
            start = int(q.get("cursor", "0"))
            chunk = reviews[start : start + 3]
            nxt = start + 3 if start + 3 < len(reviews) else None
            self._json({"reviews": chunk, "next_cursor": None if nxt is None else str(nxt)})
        elif url.path == "/api/search":
            term = q.get("q", "").lower()
            hits = [p for p in PRODUCTS if term and term in p["name"].lower()]
            self._json({"query": term, "count": len(hits), "results": hits[:10]})
        elif url.path == "/logout":
            self._text("logged out", "text/plain")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        ShopHandler.calls.append(f"POST {url.path}")
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if url.path == "/collect":
            self.send_response(204)
            self.end_headers()
        elif url.path == "/api/cart":
            self._json({"ok": True, "items": 1})
        else:
            self._json({"error": "not found"}, 404)


def serve(port: int = 0) -> ThreadingHTTPServer:
    """Start the shop in a background thread. Port 0 picks a free port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ShopHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ghost API demo shop.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    httpd = serve(args.port)
    print(f"Demo shop on http://127.0.0.1:{httpd.server_address[1]}/  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
