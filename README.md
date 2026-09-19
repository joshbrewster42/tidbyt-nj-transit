# NJ Transit Nearby — a Tidbyt app

Pick a spot on the map in the Tidbyt app and see the next departures from the
closest NJ Transit **bus stop** or **light rail station**.

```
▌HBLR 2nd St
West Side Av   7m
Tonnelle Ave  16m
West Side Av  27m
```

Bus times are realtime (GPS-based). Light rail times come from the published
timetable, because NJ Transit does not publish a realtime light rail feed.

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

**Not yet verified** — needs credentials:

- The realtime bus path (`bus_token()` / `bus_departures()`) is written from the
  documented API shape but has never been run against a live response. The
  field names it parses (`DVTrip`, `public_route`, `header`, `departuretime`,
  `sched_dep_time`) come from published client libraries, not from a response
  observed here. **Expect to adjust the parsing once you have a real payload.**

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

Not implemented. NJ Transit does not operate ferries — that is NY Waterway and
Seastreak. The commonly-cited NY Waterway GTFS feed on S3 now returns
`403 AccessDenied`, so there is no working open source for it as of this
writing.

The seam for adding it: give ferry stops `"m": "f"` in the pipeline, add a
branch in `main()` alongside the light rail one, and a color in `LINE_COLORS`.
Everything else — nearest-stop search, the picker, rendering — already works by
mode and needs no changes.

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

Open <http://127.0.0.1:8090>, type an address, pick the match, and it lists the
stops the app would offer — click any one to see the actual rendered Tidbyt
output. It starts the mock API itself, so bus departures work with no
credentials.

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
| `python3 pipeline/make_dev_copy.py` | Regenerate `.dev/` copy (run after editing the app) |
| `python3 pipeline/devui.py` | Address-search dev UI + mock API |
| `python3 pipeline/geocode.py --stops ADDR` | Coordinates and nearby stops for an address |
| `python3 pipeline/run_tests.py` | Assertions for the destination matcher and helpers |
| Auto-refresh checkbox in the dev UI | Re-render every 30s, like the device does |
| `pixlet check nj_transit_nearby.star` | Community-repo readiness |
