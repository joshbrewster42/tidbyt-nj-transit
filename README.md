# NJ Departures — a Tidbyt and Tronbyt app

Watch up to six stops — bus, light rail and ferry, in any mix — and see the
next departure from each, in the order you chose them.

```
▌  Midtown / W   15m      ← ferry, NY Waterway blue
HBLR Tonnelle     16m      ← light rail, official line colour
156  Paramus       9m      ← bus, realtime
159  Fairview      8m
```

Four fit on screen. Beyond that it pages, four seconds per page.

Bus times are realtime (GPS-based), backed by the published timetable where
that feed has gaps — it has been seen returning no bus at all for a route that
both NJ Transit's own app and the timetable had. Light rail and ferry come from
timetables alone, because neither publishes a realtime feed this app can
consume. A row only shows the live marker when a vehicle is actually
transmitting.

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

### One address, six dropdowns

Configuration is a single `schema.Location` and one `schema.Generated` sourced
from it, returning all six stop dropdowns at once.

Three pixlet rules forced this shape, each found by the config UI failing
rather than by anything at load time:

- **A generated field cannot carry a handler.** So the slots are dropdowns,
  not `LocationBased` fields — a `LocationBased` returned from a generator is
  never registered, and asking for its options answers `no exported handler
  named 'stop1$stops_all'`.
- **Several generated fields cannot share one `source`.** Six generators all
  sourced from `home` collapse into one: whichever reply lands last is the only
  slot that appears. A handler may return *several* fields, so one generator
  returns all six dropdowns.
- **A generated field's `source` must be statically declared.** Direction
  therefore cannot be a second field hanging off a generated stop dropdown —
  that answers `schema.Generated references source that does not exist: stop1`.

- **A dropdown option's value cannot be empty**, and a dropdown must carry a
  default, hence the `SLOT_UNUSED` sentinel and guarding every `json.decode` of
  it, since a sentinel is not JSON.

### Direction lives inside the option

Because direction cannot be its own field, each option is self-contained: one
entry per stop *per direction of travel*.

```
BUS · 60th St at Hudson Ave to New York - 89, 159, 188 (0.2 mi)
BUS · 60th St at Hudson Ave to North Bergen - 89, 159, 188 (0.2 mi)
FERRY · Port Imperial / Weehawken to Midtown / W. 39th St. (0.8 mi)
FERRY · Port Imperial / Weehawken to Edgewater (0.8 mi)
```

The value carries what the render needs and nothing else — which stop code to
query, its mode, and the terminals that count as this direction:

```json
{"c":"21914","d":["Hoboken Term","Journal Sq"],"m":"b"}
```

The cost is a longer list, since a stop served both ways appears twice. The
benefit is that every row says exactly where that vehicle goes, and choosing
one is a single decision rather than two.

### Why the stop pickers are dropdowns, not LocationBased

Pixlet builds its handler table from the schema `get_schema()` returns, and
keys each entry by field id. **A handler that only appears on a field returned
by `schema.Generated` is never registered.** Asking for its options answers:

```
no exported handler named 'stop1$stops_all'
```

An earlier version chose a mode first and generated a mode-specific stop
picker from it. The field appeared correctly and the handler worked when called
directly in a test, so it looked fine — but nothing ever exercised pixlet's own
dispatch, and the picker would have failed for every real user. The dev UI hid
it too, since that calls the Python functions rather than going through pixlet.

Generated fields can still carry plain dropdowns, which is what the direction
pickers are. They just cannot carry anything needing a callback of its own.

So each slot declares its own `schema.LocationBased`, and the combined list
leans on mode tags and `MODE_GUARANTEE` instead of a mode filter. Worth
re-testing through `/api/v1/handlers/...` rather than a harness if this is ever
revisited.

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
Push this repo to GitHub and point `DATA_BASE` in `nj_departures.star` at
it:

```starlark
DATA_BASE = "https://raw.githubusercontent.com/<you>/<repo>/main/data/v1"
```

No server to run and no uptime to babysit — `raw.githubusercontent.com` serves
the committed files directly.

### 3. Add your NJ Transit credentials

Register at <https://developer.njtransit.com/registration/>. There is no API
key to copy: the bus endpoint authenticates with the account's own username
and password and returns a token good for roughly a day.

There are two ways to supply them, and the app prefers the first.

**In the app config.** Fill in the "NJ Transit username" and "NJ Transit
password" fields when configuring the app. This is the only method that works
off Tidbyt's servers, and it keeps each install on its own account instead of
funnelling every user in the world through one.

**Encrypted into the source, as a fallback.**

```bash
pixlet encrypt nj-departures '<your username>'
pixlet encrypt nj-departures '<your password>'
```

Paste each result into `NJT_USERNAME_ENC` / `NJT_PASSWORD_ENC`. These are safe
to commit because only the holder of the matching private key can reverse
them — and that holder is Tidbyt's cloud. `secret.decrypt()` returns `None`
during local development and on any self-hosted server no matter how sound the
ciphertext is, so this path cannot be the only one. Note that `pixlet encrypt`
binds a secret to the **app ID**: renaming the app invalidates both values.

Credentials are **optional**. Without them the app falls back to the bus
timetable and shows scheduled times; you lose the live predictions and the live
marker, not the departures. With them, `bad login` on a row means NJ Transit
rejected the credentials rather than none being found. Light rail and ferry
never need credentials.

### 4. Run it

```bash
pixlet serve nj_departures.star
```

Or render a specific stop without the config UI:

```bash
pixlet render nj_departures.star \
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
- **Realtime bus departures, end to end** — on 2026-10-01 the app authenticated
  against the live API using credentials from the app config and rendered real
  predictions, running on a self-hosted Tronbyt server
- **Bus timetable fallback** — rendered four scheduled departures, including a
  159 the realtime feed omits, with no credentials configured at all

`secret.decrypt()` itself has still **never executed**. It only runs on
Tidbyt's servers, so nothing outside them can exercise it. The config path
above is the one with live proof behind it.

**Verified against the live API** on 2026-09-27. Both endpoints answer as
documented, and every field the app reads is present. Two *formats* were not
what the published client libraries implied:

| Field | Actual value | Assumption it broke |
|---|---|---|
| `departuretime` | `09:47 PM` | Read as a countdown — would show "9m" for a bus 20 minutes away |
| `departurestatus` | `in 20 mins` | The countdown actually lives here |
| `sched_dep_time` | `09/27/2026 09:45:58 PM` | Dated, not a duration |
| `header` | `158 NEW YORK  VIA RIVER ROAD` | Repeats the route number and appends the routing |

So departures are computed from the wall clock time rather than read off the
front of it, and the header is stripped the same way GTFS headsigns are.
`pipeline/mock_server.py` now emits these exact formats, so the dev UI tests
the real shape rather than a convenient fiction.

To check credentials or re-inspect the payload:

```bash
python3 pipeline/check_credentials.py 21923
```

Credentials are prompted for without echo, never written to disk, and the
session token is redacted from every line.

## Running on Tronbyt

Tidbyt was acquired and its community app repo stopped merging pull requests,
so [Tronbyt](https://github.com/tronbyt) picked the ecosystem up: a self-hosted
server, replacement ESP32 firmware, a maintained pixlet fork, and a hard fork
of the community apps repo.

The app runs there with no code changes, but two things differ.

**Credentials.** `secret.decrypt()` cannot work on a self-hosted server — the
private key is Tidbyt's. Use the config fields instead; see step 3 above. Note
that Tronbyt stores app config as plaintext JSON in its SQLite database, so the
password is readable by anyone with access to the host. On a personal server
that is the same trust boundary as the host itself, but it is worth knowing.

**The stop dropdowns need an upstream fix.** Tronbyt's config UI gives the
location field's wrapper `<div>` and its inner hidden input the same
`schema_<id>`, so `getElementById` returns the div, `.value` is `undefined`,
and a `schema.Generated` sourced from a location is handed an empty parameter.
The dropdowns never appear. The server and this app are both fine — calling the
handler endpoint directly returns all six fields.

Filed as [tronbyt/server#937](https://github.com/tronbyt/server/issues/937).
Until it lands, stop selections have to be written straight into the config:

```bash
docker run --rm -v tronbyt_data:/d python:3-alpine python3 -c "
import sqlite3, json
con = sqlite3.connect('/d/tronbyt.db')
app_id, cfg = con.execute('select id, config from apps where name=?', ('nj_departures',)).fetchone()
cfg = json.loads(cfg)
cfg['stop1'] = json.dumps({'c': '21923', 'd': ['New York'], 'm': 'b'})
con.execute('update apps set config=? where id=?', (json.dumps(cfg), app_id))
con.commit()
"
```

The `c` value is the stop code and `d` the list of headsigns to keep. Both come
from the handler's option values, which `pixlet serve` will print locally:

```bash
curl -s -X POST 'http://127.0.0.1:8080/api/v1/handlers/stop_pickers$stop_fields' \
  -H 'Content-Type: application/json' \
  -d '{"param":"{\"lat\":40.78,\"lng\":-74.01}"}'
```

## Putting it on a device

`pixlet push` renders **on this machine** and uploads a still image. Two
consequences:

- `secret.decrypt()` does not work here, so pushing the committed app shows
  `no api key` for bus. Real bus data needs a build with real credentials.
- the app sets `max_age`, so the device stops displaying a pushed image once
  it goes stale. A single push blanks after a couple of minutes.

So pushing means pushing repeatedly:

```bash
pixlet login                                    # once
python3 pipeline/push_to_device.py --list       # find your device ID
python3 pipeline/push_to_device.py --device ABC123 \
    'stop1={"c":"21923","m":"b","d":["New York"]}' \
    'stop2={"c":"11","m":"f","d":["Midtown / W. 39th St."]}'
```

It prompts for credentials once and keeps them in memory, writing the
credentialled build to a private temp directory at mode 600 and deleting it on
exit, Ctrl-C included. `--every` sets the interval (default 60s, and it must
stay under `MAX_AGE`), `--once` pushes a single frame.

The dev UI prints the exact config values for whatever you have selected, under
the preview.

**This only runs while your machine does.** For a display that keeps working on
its own, publish the app: on Tidbyt's servers the encrypted credentials decrypt
and none of the above applies.

---

## Seeing real bus data before publishing

`secret.decrypt()` only works inside Tidbyt's cloud, so an encrypted credential
cannot be exercised on a laptop. **`pixlet push` renders locally**, which means
even pushing to a device shows `no api key` for bus. Encrypted credentials go
live only when the app runs on Tidbyt's servers — via the community repo, or
`pixlet private` on Tidbyt Plus.

To try it before either of those:

```bash
python3 pipeline/make_dev_copy.py --live      # prompts, no echo
DEVUI_APP=.dev/live/nj_departures_live.star python3 pipeline/devui.py
```

That build talks to the real API with real credentials. `DEVUI_APP` also tells
the dev UI not to rebuild or use the mock.

It lives in its own directory because pixlet mis-resolves paths when several
`.star` files share one, reporting `reading <other file>: file does not exist`.

**It holds your password in plain text.** It is written to `.dev/`, which is
gitignored, and chmod 600 — but delete it when you are done:

```bash
rm -rf .dev/live
```

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

Its **Locality** and **Timezone** boxes are inert: nothing fills them in when
the coordinates change, and this app never reads them. Only latitude and
longitude matter. In the Tidbyt mobile app the location picker populates all of
these from the address you choose.

So there is a local dev UI that fills the gap:

```bash
python3 pipeline/devui.py
```

Open <http://127.0.0.1:8090>, type an address, pick the match, and click stops
to add them in the order you want. Each row names its direction, matching the
real picker one for one — the preview offers exactly what the app can be
configured to do, and nothing it cannot. It starts the mock API itself, so bus
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
| `python3 pipeline/make_dev_copy.py --live` | Build a local copy using real credentials (see below) |
| `python3 pipeline/devui.py` | Address-search dev UI + mock API |
| `python3 pipeline/geocode.py --stops ADDR` | Coordinates and nearby stops for an address |
| `python3 pipeline/run_tests.py` | Assertions for the destination matcher and helpers |
| `python3 pipeline/check_credentials.py STOP` | Verify NJ Transit credentials and the live response shape |
| Auto-refresh checkbox in the dev UI | Re-render every 30s, like the device does |
| `pixlet check nj_departures.star` | Community-repo readiness |
