# NJ Transit Nearby — a Tidbyt app

Watch up to six stops — bus, light rail and ferry, in any mix — and see the
next departure from each, in the order you chose them.

```
▌  Midtown / W   15m      ← ferry, NY Waterway blue
HBLR Tonnelle     16m      ← light rail, official line colour
156  Paramus       9m      ← bus, realtime
159  Fairview      8m
```

Four fit on screen. Beyond that it pages, four seconds per page.

Bus times are realtime (GPS-based). Light rail and ferry come from published
timetables, because neither publishes a realtime feed this app can consume.

---

## Why it is built this way

A Tidbyt app is a single sandboxed Starlark file. It has **no filesystem**, and
it cannot unzip or stream a 50 MB GTFS bundle at render time. But the whole
point of this app is "find stops near my address", which needs the geography of
all 16,564 bus stops.

The split that makes this work:

| Where | What runs | Cost |
|---|---|---|
| `pipeline/build_index.py` on your machine | Chews GTFS into small JSON files, committed to this repo | Minutes, occasionally |
| `stop_options()` on Tidbyt's servers | Downloads 1–4 small grid cells, ranks by distance | Once, when configuring |
| `main()` on the device | Fetches departures for **one** stop | Every refresh |

Stops are bucketed into 0.1° grid cells (`data/v1/cells/40_-74.json`). Finding
nearby stops means fetching the cell you are standing in plus the three
adjacent to the nearest corner — a few KB, not an index of every stop in
New Jersey.

### Six slots, shown in order

Configuration is six numbered slots. Each is a mode, a stop and a direction;
slots left as "Not used" are skipped. Slot order is display order.

Six is a fixed number because a Tidbyt schema is a **static form** — there is
no "add another" control, so the count has to be decided up front.

Each row is a departure: route badge, where it goes, minutes away. The stop
name is deliberately absent — four rows of "Blvd East at 47th St" would fill
the screen with names already known. What changes, and what is worth a glance,
is the destination and the countdown.

**The four rows are shared out between the slots.** Watch four or more stops
and each gets one row, paging through the rest. Watch fewer and the spare rows
go to extra departures rather than being left black:

| Slots | Rows each |
|---|---|
| 1 | next 4 from that stop |
| 2 | next 2 each |
| 3 | 2, then 1, 1 |
| 4+ | 1 each, paging |

Scaling the *font* instead was the obvious alternative and does not work: at
`6x13` the destination column is about three characters wide once the badge and
countdown have taken their share. The screen is short on width, not height.

Paging uses `render.Animation`, where **every child is exactly one frame**. A
page that stays up for four seconds therefore means repeating the same widget
`PAGE_HOLD_MS / DELAY_MS` times. Pixlet coalesces the identical frames into one
frame with a 4000 ms duration, so the output stays small — a six-slot render is
under 1 KB. `show_full_animation` asks the device to play the whole cycle
rather than cutting it off mid-rotation.

### One entry per place, not per kerb

A bus stop is a signpost on one side of the street, so a junction appears in
the feed twice — same name, different stop codes, one per direction. A light
rail platform and a ferry dock are single places where vehicles leave both
ways, so they were already one entry with a direction dropdown.

Buses now get the same shape. Same-name stops within 250 m collapse into one
entry whose direction dropdown names each kerb:

```
Blvd East at 47/48th St to North Bergen / New York - 23, 128, 165, 166 (0.2 mi)
    To New York     -> {"c":"21822"}
    To North Bergen -> {"c":"21818"}
```

Near Port Imperial this took 25 list entries covering 18 distinct places down
to 25 entries covering 25 places, with zero duplicates.

The stop codes matter, which is why the group carries them: the realtime API is
queried **per stop code**, so for a grouped bus stop the direction picker is
choosing which code to ask, not filtering what comes back. For light rail and
ferry there is one code and the direction filters departures instead.
`resolve_stop_code` and `direction_filter` sort out which is which.

Two entries sharing a name *and* a heading are the feed listing one place
twice; the nearer one wins.

### Mode first, then the stop

Configuration is a **Mode** dropdown followed by a stop picker that regenerates
to match: choose Ferry and the field becomes "Terminal — ferry terminals near
you", listing only boats.

This matters because bus stops are dense. A single combined list runs to two
dozen entries that are almost all buses — from a spot a few blocks inland of
Weehawken there are 24 within 0.4 miles, enough to push the ferry terminal half
a mile away off the list entirely. Picking the mode first turns that into ten
ferry terminals, nearest first.

Filtering happens *before* the distance limit, or asking for ferries would mean
"the 24 nearest stops of any kind, ferries only" — which from that same spot is
exactly one.

"Everything nearby" keeps the combined list, where `MODE_GUARANTEE` reserves a
place for the nearest light rail station and ferry terminal however far down
they rank. It is a floor rather than a quota — set to 1, because at two it
drags in landings across the Hudson to fill a slot.

Mechanically the picker is a `schema.Generated` field sourced from `mode` that
returns a `schema.LocationBased` with a mode-specific handler. A LocationBased
handler is only ever handed the location, so it cannot read the chosen mode —
one small handler per mode is how the filter gets through.

Pixlet resolves a handler by its **function name**, so handlers must be
top-level and cannot be closures over a slot number. Hence the twelve thin
`stop_field_N` / `direction_field_N` wrappers: they exist only to carry the
slot number into the shared implementation.

There is no separate `schema.Location` field: `LocationBased` brings its own
address picker, and having both meant two places to type an address.

The heavy data is only needed **at configuration time**, not at render time.
That is what keeps the device-side path small.

---

## Data sources

NJ Transit publishes GTFS with **no registration required**:

- `bus_data.zip` — 263 bus routes, 16,564 stops (~50 MB)
- `rail_data.zip` — 14 commuter rail + 3 light rail routes, 230 stops (~6 MB)

Light rail (Hudson-Bergen, Newark Light Rail, River LINE) lives in the **rail**
feed as `route_type=0`, alongside commuter rail. The feed carries each line's
official color, which the app uses for its badges.

Realtime bus data is a **separate, registration-gated API**:
<https://developer.njtransit.com/registration/>

Note it is a username/password exchange, not a bare API key — you POST
credentials to `authenticateUser`, receive a `UserToken` good for ~24 hours,
then pass that token to the data endpoints. The app caches the token so a
device refreshing every few seconds is not re-authenticating constantly.

---

## Setup

### 1. Generate the data

```bash
python3 pipeline/build_index.py
```

Downloads both GTFS feeds and writes `data/v1/`. Takes a few seconds; no
dependencies beyond the Python standard library. Use `--cache DIR` to reuse
already-downloaded zips while iterating.

Re-run it whenever NJ Transit publishes a new booking (roughly quarterly, and
whenever schedules change).

### 2. Publish the data

The app fetches its data over HTTP, so the generated files need a public URL.
Push this repo to GitHub and point `DATA_BASE` in `nj_transit_nearby.star` at
it:

```starlark
DATA_BASE = "https://raw.githubusercontent.com/<you>/<repo>/main/data/v1"
```

No server to run and no uptime to babysit — `raw.githubusercontent.com` serves
the committed files directly.

### 3. Add your NJ Transit credentials

Register at <https://developer.njtransit.com/registration/>, then encrypt your
credentials so they can be safely committed:

```bash
pixlet encrypt nj-transit-nearby '<your username>'
pixlet encrypt nj-transit-nearby '<your password>'
```

Paste each result into `NJT_USERNAME_ENC` / `NJT_PASSWORD_ENC`. Only Tidbyt's
servers hold the key that reverses this. `secret.decrypt()` returns `None`
locally, which the app treats as "no realtime" and shows a placeholder — so
light rail still works fine during local development.

### 4. Run it

```bash
pixlet serve nj_transit_nearby.star
```

Or render a specific stop without the config UI:

```bash
pixlet render nj_transit_nearby.star \
  stop='{"c":"38441","n":"2nd St","m":"l","r":["HBLR"]}' \
  --magnify 8 -o out.webp
```

---

## Status

**Verified working** against live data:

- GTFS pipeline — both feeds, 16,564 bus + 65 light rail stops
- Nearest-stop search — correct results and distances
- Light rail departures — real timetables, correct next-departure math
- Rendering — light rail, River LINE colors, and the degraded no-credentials state

**Partly verified** — `authenticateUser` has been exercised against the live
server with deliberately invalid credentials. It exists, answers HTTP 200, and
returns `{"Authenticated": "False", "UserToken": ...}` — the documented shape,
and the keys `bus_token()` reads.

**Not yet verified** — needs working credentials:

- `getBusDV`. The field names the app parses (`DVTrip`, `public_route`,
  `header`, `departuretime`, `sched_dep_time`) come from published client
  libraries, not from a response observed here. **Expect to adjust the parsing
  once a real payload exists.**

When credentials arrive:

```bash
export NJT_USERNAME='...'
python3 pipeline/check_credentials.py 21923
```

It calls the API exactly as the app does, reports whether each expected field
is present, and prints one full departure. Credentials are prompted for without
echo, never written to disk, and the session token is redacted from every line,
so the output is safe to paste anywhere.

---

## Direction of travel

Almost every bus stop sits on one side of a street and serves a single
direction: **13,614 of 16,564 bus stops are one-directional.** A street corner
therefore appears in the picker as two stops with the same name and different
stop codes, which is useless unless each says where its buses go. So the
picker shows the heading:

```
Rt-9w (Sylvan Ave) at Clendinen Pl to New York          - 156, 186, 756 (0.2 mi)
Rt-9w (Sylvan Ave) at Clendinen Pl to Englewood Cliffs  - 156, 186, 756 (0.2 mi)
```

Direction is a property of the stop, not a filter to apply afterwards. Picking
the right side of the street is the whole job.

The headings come from GTFS `direction_id`, which splits each route into its
two directions of travel. One direction can still end at several terminals —
the 159 outbound reaches Fort Lee, Cliffside Park and Fairview — so each
direction is labelled with the place it heads toward, while keeping the full
set of terminals to match departures against.

**Labels are checked against NJ Transit's own naming.** MyBus offers exactly
two directions per route, named by place
(`selectdirection.jsp?route=159` → New York, Fort Lee), and the pipeline now
agrees:

| Route | MyBus | Pipeline |
|---|---|---|
| 156 | New York · Englewood Cliffs | New York · Englewood Cliffs |
| 158 | New York · Fort Lee | New York · Fort Lee |
| 159 | New York · Fort Lee | New York · Fort Lee |

Getting there needed two rules. The busiest headsign is often too specific —
the 159's is `Fort Lee Linwood Park` — so a name is trimmed back toward a
plainer place. But trimming is only allowed down to a form that is *itself* a
terminal somewhere in the network, and never below two words. Without the
first rule `West New York` becomes `West New`; without the second,
`Englewood Cliffs` becomes `Englewood`, which is a different town.

A **Direction** dropdown appears only at stops that genuinely run both ways,
which is where it earns its place. At the other 82% it would be a control with
one meaningful choice, so it is hidden.

Headsigns need heavy cleaning to get there. GTFS ships these as separate
strings for what a rider calls one direction:

```
156  NEW YORK VIA PARK AVE
156R NEW YORK VIA RIVER ROAD
```

Stripping the route code, the fare notice and the `VIA` qualifier collapses
both to `New York`.

**Matching is the fragile part.** Light rail destinations come from our own
data and compare exactly. Bus destinations arrive live from the API and may be
worded differently than the GTFS headsign they were derived from, so matching
compares word by word against whichever description is shorter, expanding known
abbreviations (`Sq`/`Square`, `Ctr`/`Center`). Generic prefix matching was tried
first and rejected — it pairs "Newark" with "New York". See
`pipeline/run_tests.py` for the cases this is pinned against.

If a filter matches nothing, the display says `none to <destination>` rather
than going blank, so a filter is never mistaken for an outage.

---

## Refreshing

A Tidbyt render is a still image with a fixed animation; it does not update
itself. The device shows a fresh one because Tidbyt's backend re-runs the app
on a cadence and pushes the result. Two settings shape that:

- `ttl_seconds` on each HTTP call caps how stale the fetched data can be.
  Realtime departures use 30s; the generated static files use a day.
- `max_age` on `render.Root` bounds how long a render may be *displayed*. It is
  set to 120s, because a countdown fails quietly: if the device loses
  connectivity it keeps showing the last image, and a stale "3 min" is worse
  than a blank screen because it is still believable.

The dev UI's preview is a single render and stays put until you click. The
**Auto-refresh** checkbox under the preview re-renders every 30s to approximate
the device. Polling faster only re-serves cached data.

---

## Ferry

NJ Transit runs no ferries — the Hudson crossings are **NY Waterway's**. They
publish through two unrelated platforms, and the choice matters:

```
https://nywaterway.connexionz.net/rtt/public/resource/gtfs.zip
```

**Connexionz** is republished daily and covers today. 14 routes, all of them
boats, 15 terminals, with real route names and headsigns.

**Trillium** (`data.trilliumtransit.com/gtfs/nywaterway-nj-us/…`) also serves
NY Waterway and is the copy most catalogs point to, but it carries only the
*next* booking. The copy fetched on 2026-09-19 covered `20261001`–`20270401` —
valid GTFS describing nothing but the future, which would have shipped an empty
ferry mode for twelve days. It also mixes boats with 19 free connector shuttle
buses and has two fewer terminals.

The much-cited `data.bytemark.co` S3 bucket is long dead (403), and most links
on the web still point at it.

### Finding a feed when its URL dies

The MobilityData catalog CSV is public, needs no account, and lists the
download URL plus a mirror for ~3,500 feeds:

```bash
curl -sSL https://bit.ly/catalogs-csv | grep -i waterway
```

That is how the Connexionz feed was found after the bytemark URL started
returning 403. Check it before concluding a feed is unavailable.

### Traps in this data

- **Some sailings loop back to their origin.** Taking "the trip's last stop" as
  the destination makes those look like they go nowhere, and they get dropped.
  Walk back to the last call that differs from the current stop.
- **A terminal's name can lie.** In the Trillium feed the stop called "Port
  Imperial Ferry Terminal" carries *zero* ferries — it is the shuttle bus bay.
- **Arrivals look like departures.** A boat terminating at your stop has a
  departure_time too. Excluding calls with no distinct later stop removes them.

### Realtime exists, but is out of reach

NY Waterway publishes live GTFS-realtime, unauthenticated:

```
nywaterway.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/tripupdate
                                                              /vehicleposition
                                                              /alert
```

All three are protobuf, and there is no JSON variant — the obvious `.json`
paths are soft-404 HTML. Pixlet has no protobuf module, so consuming this from
a Tidbyt app would need a decoding proxy. Ferry therefore uses timetables, like
light rail.

`ferry.nyc` is a **different operator** (NYC EDC/Hornblower). Open feed, has
realtime, but all 50 of its landings are inside the five boroughs — none in New
Jersey — so it is not used here. **Seastreak** has no working public feed.

---

## Layout notes

The display is 64×32 pixels, which is about eleven characters of `tom-thumb`
per row after the badge and the countdown. Two decisions follow from that:

- **Names are shortened at build time**, not render time. GTFS ships
  `2ND STREET LIGHT RAIL STATION`; the pipeline stores `2nd St`.
- **The route badge is dropped when it is redundant.** At a light rail platform
  every departure is the same line, so it moves to the header and the
  destinations get those pixels. At a bus stop served by six routes, it stays.

---

## Entering an address during development

`pixlet serve` has **no address search** — it renders `schema.Location` as bare
latitude/longitude inputs, and its "Locality" box is cosmetic (typing into it
does not geocode). The realtime address search users expect lives in the Tidbyt
**mobile app**, which is where `schema.Location` becomes a real map picker.

So there is a local dev UI that fills the gap:

```bash
python3 pipeline/devui.py
```

Open <http://127.0.0.1:8090>, type an address, pick the match, and click stops
to add them in the order you want. It starts the mock API itself, so bus
departures work with no credentials.

It also rebuilds the dev copy of the app whenever the source is newer. That
used to be a manual step, which meant editing the app and reloading the preview
showed the **previous** version — silently, and for as long as it took someone
to notice they were debugging a fix that had already landed.

For a one-off from the terminal:

```bash
python3 pipeline/geocode.py --stops "1 Hudson Place, Hoboken, NJ"
python3 pipeline/geocode.py --stops 40.7352 -74.0277   # skips the network
```

Address lookups go to OpenStreetMap Nominatim. Nothing else leaves the machine,
and nothing is stored.

---

## Development commands

| Command | What it does |
|---|---|
| `python3 pipeline/build_index.py` | Rebuild `data/v1/` from the GTFS feeds |
| `python3 pipeline/make_dev_copy.py` | Regenerate `.dev/` copy by hand (devui does this automatically) |
| `python3 pipeline/devui.py` | Address-search dev UI + mock API |
| `python3 pipeline/geocode.py --stops ADDR` | Coordinates and nearby stops for an address |
| `python3 pipeline/run_tests.py` | Assertions for the destination matcher and helpers |
| `python3 pipeline/check_credentials.py STOP` | Verify NJ Transit credentials and the live response shape |
| Auto-refresh checkbox in the dev UI | Re-render every 30s, like the device does |
| `pixlet check nj_transit_nearby.star` | Community-repo readiness |
