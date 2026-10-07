"""Chennai Turf Finder: find sports venues near an area, cheapest and closest first.

Built on the client Ghost API generated for turftown.in (turf_town_client.py, models.py).
It uses only Turf Town's public venue list, the same one its website shows when you browse.

    python turf_finder.py --area velachery --sport football
    python turf_finder.py --area porur --sport badminton --max 48

It prints the closest and the cheapest venues, and writes turf_finder.html, a page you can
open offline to sort and filter them.

Privacy: Turf Town's per-venue "details" call also returns private business data (staff
emails, PAN, GST and bank details). This project never calls it, and keeps only the
public fields listed in `public_view()`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from turf_town_client import TurfTownClient

OUT = Path(__file__).with_name("turf_finder.html")
PAGE_SIZE = 24

# Area centres, from Turf Town's own public list of areas (latitude, longitude).
AREAS = {
    "guduvanchery": (12.8452, 80.0650),
    "kayarambedu": (12.8048, 80.0752),
    "mannivakkam": (12.8871, 80.0604),
    "mudichur": (12.9137, 80.0722),
    "mylapore": (13.0381, 80.2696),
    "nolambur": (13.0749, 80.1680),
    "nungambakkam": (13.0611, 80.2430),
    "porur": (13.0376, 80.1546),
    "ramapuram": (13.0289, 80.1803),
    "royappa nagar": (12.9238, 80.0666),
    "sholinganallur": (12.8969, 80.2248),
    "thoraipakkam": (12.9366, 80.2379),
    "urapakkam": (12.8647, 80.0722),
    "vandalur": (12.8883, 80.0831),
    "varadharajapuram": (12.9266, 80.0758),
    "velachery": (12.9791, 80.2198),
}
SPORTS = [
    "badminton", "basketball", "cricket", "football", "padel",
    "pickleball", "squash", "swimming", "tabletennis", "tennis",
]  # fmt: skip


def number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def public_view(v: Any) -> dict[str, Any]:
    """Only what any visitor sees on the website. Nothing else is kept."""
    lng, lat = (v.loc.coordinates + [None, None])[:2]  # stored as [longitude, latitude]
    return {
        "name": v.venue.name,
        "sport": v.type,
        "area": v.venue.area or "",
        "address": v.venue.address or "",
        "pincode": v.venue.pincode or "",
        "distance_km": number(v.distance),
        "avg_price": number(v.configuration.avg_price),
        "reviews": v.ratings_and_reviews.total_ratings or 0,
        "offers": len(v.offers or []),
        "picture": v.venue.venue_display_picture or "",
        "map": f"https://www.google.com/maps?q={lat},{lng}" if lat and lng else "",
    }


def find_venues(area: str, sport: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    lat, lng = AREAS[area]
    venues: list[dict[str, Any]] = []
    total = 0
    with TurfTownClient() as api:  # Ghost API's client: 1 request per second, retries
        for skip in range(0, limit, PAGE_SIZE):
            reply = api.get_nearby_venues(
                area=area, lat_long=[lat, lng], sport=[sport],
                limit=min(PAGE_SIZE, limit - skip), skip=skip,
            )  # fmt: skip
            total = reply.data.all_venues
            venues += [public_view(v) for v in reply.data.final_venues]
            if len(reply.data.final_venues) < PAGE_SIZE:
                break
    return venues, total


def show(title: str, venues: list[dict[str, Any]]) -> None:
    print(title)
    for v in venues:
        price = f"avg Rs {v['avg_price']:.0f}" if v["avg_price"] is not None else "price ?"
        dist = f"{v['distance_km']:.1f} km" if v["distance_km"] is not None else "? km"
        print(f"  {v['name'][:32]:32}  {dist:>8}  {price:>12}  {v['reviews']:>3} reviews")
    print()


def write_page(venues: list[dict[str, Any]], area: str, sport: str) -> None:
    data = json.dumps(venues).replace("</", "<\\/")
    page = (
        PAGE.replace("__DATA__", data)
        .replace("__AREA__", area.title())
        .replace("__SPORT__", sport.title())
    )
    OUT.write_text(page, encoding="utf-8")
    print(f"Saved {OUT.name}: open it in your browser to sort and filter.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--area", default="velachery", choices=sorted(AREAS))
    parser.add_argument("--sport", default="football", choices=SPORTS)
    parser.add_argument("--max", type=int, default=24, help="how many venues (24 per request)")
    args = parser.parse_args()

    venues, total = find_venues(args.area, args.sport, max(1, min(args.max, 96)))
    if not venues:
        sys.exit(f"No {args.sport} venues found near {args.area}.")
    print(f"\n{len(venues)} {args.sport} venues near {args.area.title()} "
          f"(Turf Town lists {total} in all).\n")  # fmt: skip
    near = sorted(venues, key=lambda v: v["distance_km"] if v["distance_km"] is not None else 99)
    cheap = sorted(
        (v for v in venues if v["avg_price"]),
        key=lambda v: v["avg_price"],  # type: ignore[no-any-return]
    )
    show("Closest:", near[:5])
    show("Cheapest:", cheap[:5])
    write_page(venues, args.area, args.sport)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Turf Finder: __SPORT__ near __AREA__</title>
<style>
  :root { --bg:#f5f7f4; --card:#fff; --fg:#17201a; --muted:#5f6b62; --accent:#15803d;
          --line:#dde5df; --soft:#e7f5ec; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0c120e; --card:#141c17; --fg:#e8f0ea; --muted:#93a598; --accent:#4ade80;
            --line:#243029; --soft:#183223; } }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, "Segoe UI", sans-serif; background:var(--bg);
         color:var(--fg); }
  header { padding:28px 16px 8px; text-align:center; }
  h1 { margin:0; font-size:1.8rem; } h1 span { color:var(--accent); }
  .sub { color:var(--muted); margin-top:6px; }
  .bar { max-width:1000px; margin:16px auto; padding:0 16px; display:flex; gap:8px;
         flex-wrap:wrap; align-items:center; }
  input, select { padding:9px 11px; border-radius:10px; border:1px solid var(--line);
                  background:var(--card); color:var(--fg); font-size:.95rem; }
  input[type=search] { flex:1; min-width:180px; }
  label { color:var(--muted); font-size:.9rem; display:flex; gap:6px; align-items:center; }
  main { max-width:1000px; margin:8px auto 40px; padding:0 16px; display:grid; gap:12px;
         grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); }
  .v { background:var(--card); border:1px solid var(--line); border-radius:14px;
       overflow:hidden; display:flex; flex-direction:column; }
  .v img { width:100%; height:140px; object-fit:cover; background:var(--soft); }
  .v .b { padding:12px 14px; display:grid; gap:6px; }
  .v h2 { margin:0; font-size:1.05rem; }
  .v .a { color:var(--muted); font-size:.85rem; }
  .row { display:flex; gap:6px; flex-wrap:wrap; }
  .pill { background:var(--soft); color:var(--accent); border-radius:99px; padding:2px 9px;
          font-size:.8rem; font-weight:600; }
  .v a { color:var(--accent); font-size:.85rem; }
  .count { color:var(--muted); font-size:.9rem; max-width:1000px; margin:0 auto;
           padding:0 16px; }
</style></head><body>
<header><h1>Turf <span>Finder</span></h1>
<div class="sub">__SPORT__ near __AREA__ · public listings from Turf Town, via a client made by
Ghost API</div></header>
<div class="bar">
  <input type="search" id="q" placeholder="Search name, area or address...">
  <label>Sort <select id="sort">
    <option value="distance_km">Closest</option><option value="avg_price">Cheapest</option>
    <option value="reviews">Most reviewed</option></select></label>
  <label>Max price <input type="number" id="max" min="0" step="10" placeholder="any"
    style="width:90px"></label>
</div>
<p class="count" id="count"></p>
<main id="list"></main>
<script>
const VENUES = __DATA__;
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v, d) => v === null || v === undefined ? d : v;
["q", "sort", "max"].forEach(id => document.getElementById(id).oninput = render);
function render() {
  const term = document.getElementById("q").value.toLowerCase();
  const key = document.getElementById("sort").value;
  const max = parseFloat(document.getElementById("max").value);
  const dir = key === "reviews" ? -1 : 1;
  const hits = VENUES.filter(v =>
      (v.name + " " + v.area + " " + v.address).toLowerCase().includes(term) &&
      (isNaN(max) || (v.avg_price !== null && v.avg_price <= max)))
    .sort((a, b) => dir * (num(a[key], 1e9) - num(b[key], 1e9)));
  document.getElementById("count").textContent = `Showing ${hits.length} of ${VENUES.length}`;
  document.getElementById("list").innerHTML = hits.map(v => `
    <div class="v">${v.picture ? `<img src="${esc(v.picture)}" alt="" loading="lazy">` : ""}
      <div class="b"><h2>${esc(v.name)}</h2>
        <div class="a">${esc(v.address)}${v.pincode ? ", " + esc(v.pincode) : ""}</div>
        <div class="row">
          ${v.distance_km !== null
            ? `<span class="pill">${v.distance_km.toFixed(1)} km</span>` : ""}
          ${v.avg_price ? `<span class="pill">avg Rs ${Math.round(v.avg_price)}</span>` : ""}
          <span class="pill">${v.reviews} reviews</span>
          ${v.offers ? `<span class="pill">${v.offers} offers</span>` : ""}
          <span class="pill">${esc(v.sport)}</span></div>
        ${v.map ? `<a href="${esc(v.map)}" target="_blank" rel="noopener">Open in maps</a>` : ""}
      </div></div>`).join("");
}
render();
</script></body></html>"""


if __name__ == "__main__":
    main()
