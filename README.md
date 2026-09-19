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
