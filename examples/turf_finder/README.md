# Chennai Turf Finder: a project built on Ghost API

**What it is:** a small tool that finds sports venues (football turfs, badminton courts,
swimming pools...) near an area of Chennai, using the public listings on
[turftown.in](https://turftown.in/chennai). It shows the **closest** and the **cheapest**
venues, and makes a page where you can sort and filter them.

## Run it

From the project folder, with the virtual environment active:

```bat
cd examples\turf_finder
python turf_finder.py --area velachery --sport football
```

```
24 football venues near Velachery (Turf Town lists 222 in all).

Closest:
  turf ONE 27                         0.1 km     avg Rs 80    4 reviews
  VR Sports Hub                       1.2 km    avg Rs 100    2 reviews
  Tiki Taka                           1.2 km    avg Rs 214   77 reviews
  ...

Cheapest:
  DG Multisport Turf                  2.5 km     avg Rs 60    0 reviews
  ...

Saved turf_finder.html: open it in your browser to sort and filter.
```

Then open `turf_finder.html`: search by name or address, sort by distance, price or
reviews, set a maximum price, and click "Open in maps".

Other choices:

```bat
python turf_finder.py --area porur --sport badminton
python turf_finder.py --area sholinganallur --sport swimming --max 48
python turf_finder.py --help
```

Areas: guduvanchery, kayarambedu, mannivakkam, mudichur, mylapore, nolambur, nungambakkam,
porur, ramapuram, royappa nagar, sholinganallur, thoraipakkam, urapakkam, vandalur,
varadharajapuram, velachery. Sports: badminton, basketball, cricket, football, padel,
pickleball, squash, swimming, tabletennis, tennis.

## No code at all

The same venue list as a spreadsheet, straight from Ghost API:

```bat
ghost-api fetch turftown.in/chennai get_nearby_venues area=velachery sport=football "latLong=[12.9791,80.2198]" --pages 2 --format xlsx
```

Get told when a new turf appears or a price changes:

```bat
ghost-api watch add turftown.in/chennai get_nearby_venues area=velachery sport=football "latLong=[12.9791,80.2198]" --every 1d --notify ntfy:a-long-private-topic --track configuration.avg_price,venue.name
```

Or let Claude answer questions about it: `claude mcp add ghost-turftown -- ghost-api mcp turftown.in/chennai`.
Emails and phone numbers in the listings stay hidden in all of these.

## Where Ghost API helped

Turf Town has no public API and no documentation. Its website loads venues from a hidden
server (`web-gateway.turftown.in`) with requests you can't see on the page.

| Without Ghost API | With Ghost API |
|---|---|
| Open the browser's developer tools, click around, find which of dozens of requests carries the venue list, work out what it needs (city, area, location, sport, page size) and what it returns (a big nested JSON), then write and test code for it. Hours. | `ghost-api run https://turftown.in/chennai --goal "get every details"` found the calls and showed exactly what each one needs and returns (in its `report.html`). Then `ghost-api generate turftown.in/chennai --allow "POST /api/v2/venue/web/venues_around_you"` wrote the client: `get_nearby_venues(area=..., sport=[...])`, with typed results, retries and polite rate limiting. Minutes. |

This project's own code only needs one call to get the data:

```python
reply = TurfTownClient().get_nearby_venues(
    area="velachery", lat_long=[12.9791, 80.2198], sport=["football"]
)
for venue in reply.data.final_venues:
    print(venue.venue.name, venue.distance)
```

Everything else (picking the public fields, sorting, the page) is the project itself.

**Why `--allow`?** The venue list is fetched with a POST request, the kind websites
normally use to change things (book, pay, post). Ghost API never turns those into client
methods on its own. We looked at it first: it only searches. `--allow` tells Ghost API
"I checked this one, it only reads".

## Privacy: what this project does not do

While building this, we found that Turf Town's per-venue **details** call returns private
business data to anyone, without a login: staff and owner email addresses, money received,
PAN and GST numbers, bank (UPI) details. That's a problem on Turf Town's side.

So this project:

- **never calls** the details endpoint (`get_venue_details` is in the generated client,
  but don't use it);
- keeps only what any visitor sees on the website: name, area, address, distance, average
  price, number of reviews, offers, picture (see `public_view()` in `turf_finder.py`);
- makes a few requests per run (24 venues each, one per second).

If you use the data, keep it to personal or learning use, and respect Turf Town's terms.
The files `turf_town_client.py` and `models.py` are copied from
`output/turftown.in/chennai/`, made by the commands above.
