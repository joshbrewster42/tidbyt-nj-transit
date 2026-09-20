#!/usr/bin/env python3
"""
A local dev UI with the address search that `pixlet serve` lacks.

pixlet serve makes you type raw latitude and longitude, which nobody knows off
the top of their head. The Tidbyt mobile app has a proper search-as-you-type
address picker; this reproduces that experience locally so the app can be
tested the way a real user would configure it.

    python3 pipeline/devui.py

Then open http://127.0.0.1:8090 and type an address. It shows the stops the
app would offer, and renders the actual Tidbyt output for whichever you pick.

Runs the mock NJ Transit API in a background thread, so bus departures work
without credentials. Everything is local except address lookups, which go to
OpenStreetMap's Nominatim.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_server  # noqa: E402  (same directory)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "v1")
APP = os.path.join(ROOT, "nj_transit_nearby.star")
DEV_APP = os.path.join(ROOT, ".dev", "nj_transit_dev.star")
MAKE_DEV_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "make_dev_copy.py")

PORT = 8090
CELL_SIZE = 0.1
# Mirrors MODE_GUARANTEE in the app: a floor, not a quota. The nearest light
# rail station and ferry terminal always appear; anything else close enough
# earns its place on distance.
MODE_GUARANTEE = 1
USER_AGENT = "tidbyt-nj-transit-dev/1.0 (local development helper)"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


GROUP_RADIUS_KM = 0.25


def group_bus_stops(stops):
    """Mirror the app: collapse the two kerbs of a junction into one entry."""
    out, groups = [], {}
    for s in stops:
        if s["m"] != "b":
            out.append(s)
            continue
        found = groups.get(s["n"])
        if found and haversine_km(found["lat"], found["lon"],
                                  s["lat"], s["lon"]) <= GROUP_RADIUS_KM:
            heading = s.get("t", "")
            if any(m[1] == heading for m in found["g"]):
                continue  # the feed listing one place twice
            found["g"].append([s["c"], heading])
            for route in s.get("r", []):
                if route not in found["r"]:
                    found["r"].append(route)
            found["t"] = " / ".join(m[1] for m in found["g"] if m[1])
            continue
        group = dict(s)
        group["r"] = list(s.get("r", []))
        group["g"] = [[s["c"], s.get("t", "")]]
        groups[s["n"]] = group
        out.append(group)
    return out


def nearby(lat, lon, mode="", limit=25):
    """Mirror the app's own cell lookup so results match the real picker.

    Filtering happens before the limit, exactly as the app does it: asking for
    ferries must not mean "the 25 nearest stops of any kind, ferries only".
    """
    ci, cj = math.floor(lat / CELL_SIZE), math.floor(lon / CELL_SIZE)
    di = 1 if (lat / CELL_SIZE - ci) >= 0.5 else -1
    dj = 1 if (lon / CELL_SIZE - cj) >= 0.5 else -1

    stops = []
    for (i, j) in [(ci, cj), (ci + di, cj), (ci, cj + dj), (ci + di, cj + dj)]:
        path = os.path.join(DATA, "cells", "%d_%d.json" % (i, j))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                stops.extend(json.load(fh))

    scored = []
    for s in stops:
        if mode and s["m"] != mode:
            continue
        d = haversine_km(lat, lon, s["lat"], s["lon"])
        item = dict(s)
        item["mi"] = round(d * 0.621371, 2)
        scored.append(item)
    scored.sort(key=lambda s: s["mi"])
    scored = group_bus_stops(scored)

    if mode:
        # One mode is already its own group; nothing can be crowded out.
        return scored[:limit]

    chosen = scored[:limit]
    seen = {s["m"] + s["c"] for s in chosen}
    for guaranteed in ("l", "f"):
        present = sum(1 for s in chosen if s["m"] == guaranteed)
        for s in scored:
            if present >= MODE_GUARANTEE:
                break
            if s["m"] != guaranteed or (s["m"] + s["c"]) in seen:
                continue
            chosen.append(s)
            seen.add(s["m"] + s["c"])
            present += 1

    chosen.sort(key=lambda s: s["mi"])
    return chosen


def destinations_for(lat, lon, code):
    """The destination list the app's direction picker would offer."""
    ci, cj = math.floor(lat / CELL_SIZE), math.floor(lon / CELL_SIZE)
    path = os.path.join(DATA, "dirs", "%d_%d.json" % (ci, cj))
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get(code, [])


SLOT_KEYS = ("c", "n", "m", "r", "lat", "lon", "t")


def refresh_dev_copy():
    """Rebuild the dev copy whenever the real app is newer.

    The copy used to be regenerated by hand, so editing the app and reloading
    the preview showed the *previous* version -- silently, and indefinitely.
    That is an excellent way to spend an afternoon debugging a fix that has
    already landed.
    """
    if not os.path.exists(APP):
        return
    if (os.path.exists(DEV_APP)
            and os.path.getmtime(DEV_APP) >= os.path.getmtime(APP)):
        return

    proc = subprocess.run([sys.executable, MAKE_DEV_COPY],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("could not refresh the dev copy:\n"
                           + (proc.stderr or proc.stdout).strip())
    print("  dev copy rebuilt from nj_transit_nearby.star", file=sys.stderr)


def render_slots(slots):
    """Render the real app for a list of watched stops, in order."""
    refresh_dev_copy()
    if not os.path.exists(DEV_APP):
        raise RuntimeError(
            "Missing %s -- run: python3 pipeline/make_dev_copy.py" % DEV_APP)

    args = []
    for i, slot in enumerate(slots[:6], start=1):
        stop = {k: slot["stop"][k] for k in SLOT_KEYS if k in slot["stop"]}
        args.append("mode%d=%s" % (i, stop.get("m", "b")))
        args.append("stop%d=%s" % (i, json.dumps(stop)))
        if slot.get("direction"):
            args.append("direction%d=%s" % (i, slot["direction"]))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.webp")
        proc = subprocess.run(
            ["pixlet", "render", os.path.basename(DEV_APP)] + args
            + ["--magnify", "6", "-o", out],
            cwd=os.path.dirname(DEV_APP),
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((proc.stderr or proc.stdout or "render failed").strip())
        with open(out, "rb") as fh:
            return fh.read()


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NJ Transit Nearby — dev</title>
<style>
  :root {
    --bg: #11151c; --panel: #1a212b; --line: #2c3644;
    --text: #e8eef5; --dim: #8496a8; --accent: #4ade80; --amber: #e8a33d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 24px 16px 64px;
  }
  .wrap { max-width: 780px; margin: 0 auto; }
  h1 { font-size: 19px; margin: 0 0 4px; font-weight: 650; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }
  input[type=search] {
    width: 100%; padding: 13px 15px; font-size: 16px;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--line); border-radius: 9px; outline: none;
  }
  input[type=search]:focus { border-color: var(--accent); }
  .hint { color: var(--dim); font-size: 12px; margin-top: 7px; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 9px; margin-top: 16px; overflow: hidden; }
  .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .07em;
             color: var(--dim); margin: 0; padding: 11px 15px;
             border-bottom: 1px solid var(--line); font-weight: 600; }
  .row { padding: 11px 15px; border-bottom: 1px solid var(--line);
         cursor: pointer; display: flex; gap: 11px; align-items: center; }
  .row:last-child { border-bottom: none; }
  .row:hover { background: #222c39; }
  .row.on { background: #23323f; }
  .mi { color: var(--dim); font-variant-numeric: tabular-nums;
        font-size: 13px; min-width: 52px; }
  .nm { flex: 1; min-width: 0; }
  .nm small { display: block; color: var(--dim); font-size: 12px;
              overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tag { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
         color: #11151c; letter-spacing: .03em; }
  .tag.b { background: var(--amber); }
  .tag.l { background: #2bb3b3; }
  .tag.f { background: #1e9bd7; }
  .preview { padding: 18px 15px; text-align: center; }
  .preview img { image-rendering: pixelated; max-width: 100%;
                 border-radius: 5px; border: 1px solid var(--line); }
  .err { color: #ff8787; font-size: 13px; padding: 14px 15px;
         white-space: pre-wrap; font-family: ui-monospace, monospace; }
  .empty { color: var(--dim); padding: 14px 15px; font-size: 14px; }
  .coords { color: var(--dim); font-size: 12px; padding: 0 15px 12px;
            font-variant-numeric: tabular-nums; }
  .chips { padding: 12px 15px; display: flex; flex-wrap: wrap; gap: 7px; }
  .chip { font-size: 13px; padding: 6px 11px; border-radius: 99px;
          border: 1px solid var(--line); background: #18202a;
          color: var(--dim); cursor: pointer; }
  .chip:hover { border-color: #46566a; color: var(--text); }
  .chip.on { background: var(--accent); border-color: var(--accent);
             color: #11151c; font-weight: 600; }
  .slot { display: flex; align-items: center; gap: 10px; padding: 9px 15px;
          border-bottom: 1px solid var(--line); }
  .slot:last-child { border-bottom: none; }
  .num { color: var(--dim); font-size: 12px; min-width: 14px;
         font-variant-numeric: tabular-nums; }
  .slot select { background: #18202a; color: var(--text); font-size: 12px;
                 border: 1px solid var(--line); border-radius: 6px;
                 padding: 3px 6px; max-width: 190px; }
  .x { color: var(--dim); cursor: pointer; padding: 0 4px; font-size: 16px; }
  .x:hover { color: #ff8787; }
  .full { color: var(--dim); font-size: 12px; padding: 9px 15px; }
  .live { display: flex; align-items: center; gap: 9px; padding: 11px 15px;
          border-top: 1px solid var(--line); color: var(--dim); font-size: 12px; }
  .live label { display: flex; align-items: center; gap: 6px; cursor: pointer;
                color: var(--text); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #3a4654; }
  .dot.on { background: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: .25; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>NJ Transit Nearby</h1>
  <div class="sub">Local preview. Type an address the way you actually would.</div>

  <input type="search" id="q" placeholder="e.g. 1 Hudson Place, Hoboken NJ"
         autocomplete="off" autofocus>
  <div class="hint" id="hint">Addresses are looked up via OpenStreetMap. Everything else stays on this machine.</div>

  <div id="places"></div>
  <div id="watch"></div>
  <div id="stops"></div>
  <div id="out"></div>
</div>

<script>
const $ = id => document.getElementById(id);
let timer = null, lastStops = [];

// Keyed by the mode letter the data uses, so a new mode shows up as its own
// letter rather than silently mislabelling itself as a bus. The colours match
// LINE_COLORS and FERRY_COLOR in the app.
const MODE_TAG = {b: 'BUS', l: 'RAIL', f: 'FERRY'};

// The real device re-renders on a cadence set by Tidbyt's backend; this
// approximates that so a preview is not a frozen snapshot. 30s matches the
// app's realtime cache window -- polling faster only re-serves cached data.
let live = false, liveTimer = null;

function setLive(on) {
  live = on;
  clearInterval(liveTimer);
  if (on) liveTimer = setInterval(() => { if (watching.length) draw(); }, 30000);
  // Reflect it now rather than waiting for the next redraw, or the toggle
  // looks like it did nothing for 30 seconds.
  const dot = document.querySelector('.dot');
  if (dot) dot.classList.toggle('on', on);
}

$('q').addEventListener('input', () => {
  clearTimeout(timer);
  const v = $('q').value.trim();
  if (v.length < 4) { $('places').innerHTML = ''; return; }
  // Debounced so we make one request per pause, not one per keystroke --
  // Nominatim asks for at most 1 req/sec.
  timer = setTimeout(() => search(v), 450);
});

async function search(q) {
  $('hint').textContent = 'Searching…';
  try {
    const r = await fetch('/api/geocode?q=' + encodeURIComponent(q));
    const places = await r.json();
    $('hint').textContent = places.length ? 'Pick the right match:'
                                          : 'No match — try adding town and state.';
    $('places').innerHTML = places.length ? card('Matches', places.map((p, i) =>
      `<div class="row" onclick="pick(${p.lat},${p.lon},${i})">
         <div class="nm">${esc(p.name)}</div></div>`).join('')) : '';
  } catch (e) {
    $('hint').textContent = 'Lookup failed: ' + e.message;
  }
}

let lastCoords = null;

async function pick(lat, lon) {
  $('places').innerHTML = '';
  $('hint').textContent = 'Stops near that address:';
  lastCoords = [lat, lon];
  const r = await fetch(`/api/stops?lat=${lat}&lon=${lon}`);
  lastStops = await r.json();
  if (!lastStops.length) {
    $('stops').innerHTML = card('Nearby stops',
      '<div class="empty">No stops within range. Is the address in New Jersey?</div>');
    return;
  }
  renderStops();
}

// Mirrors the app's Mode field: one long list is almost all bus stops, so the
// mode is chosen first and the list becomes short and readable.
let modeFilter = '';

async function setMode(m) {
  modeFilter = m;
  const [lat, lon] = lastCoords;
  const r = await fetch(`/api/stops?lat=${lat}&lon=${lon}&mode=${m}`);
  lastStops = await r.json();
  renderStops();
}

function renderStops() {
  const [lat, lon] = lastCoords;
  // Counts are only meaningful for the list currently loaded, so label the
  // tabs plainly rather than quoting a number that changes on click.
  const tabs = [['', 'Everything'], ['b', 'Bus'],
                ['l', 'Light Rail'], ['f', 'Ferry']]
    .map(t => `<span class="chip ${modeFilter === t[0] ? 'on' : ''}"
                     onclick="setMode('${t[0]}')">${t[1]}</span>`).join('');

  const shown = lastStops;
  $('stops').innerHTML = card('Nearby stops',
    `<div class="chips">${tabs}</div>` +
    `<div class="coords">${lat.toFixed(5)}, ${lon.toFixed(5)}</div>` +
    shown.map((s) =>
    `<div class="row" id="s${lastStops.indexOf(s)}" onclick="addStop(${lastStops.indexOf(s)})">
       <span class="mi">${s.mi.toFixed(1)} mi</span>
       <span class="tag ${s.m}">${MODE_TAG[s.m] || s.m.toUpperCase()}</span>
       <span class="nm">${esc(s.n)}${s.t ? ' <span style="color:var(--accent)">to ' + esc(s.t) + '</span>' : ''}<small>${esc((s.r || []).join(', '))}</small></span>
     </div>`).join(''));
  $('hint').textContent = 'Click stops to add them, in the order you want them shown.';
}

// The app has six numbered slots and shows them in order, so the preview
// keeps an ordered list rather than a single selection.
const MAX_SLOTS = 6;
let watching = [];

async function addStop(i) {
  if (watching.length >= MAX_SLOTS) return;
  const s = lastStops[i];
  if (watching.some(w => w.stop.m === s.m && w.stop.c === s.c)) return;

  let dirs = [];
  if ((s.g || []).length > 1) {
    // A grouped bus stop: the choice names which kerb to query. It carries
    // that kerb's terminals too, because 17% of bus stops serve both
    // directions and the code alone would not narrow what comes back.
    dirs = [];
    for (const m of s.g) {
      const rr = await fetch(`/api/dests?lat=${s.lat}&lon=${s.lon}&code=${encodeURIComponent(m[0])}`);
      const entries = await rr.json();
      let terms = [];
      for (const e of entries) if (!terms.length || e.l === m[1]) terms = e.m;
      dirs.push({l: m[1] || 'This stop', v: JSON.stringify({c: m[0], m: terms})});
    }
  } else {
    const r = await fetch(`/api/dests?lat=${s.lat}&lon=${s.lon}&code=${encodeURIComponent(s.c)}`);
    const d = await r.json();
    if (d.length >= 2) dirs = d.map(x => ({l: x.l, v: JSON.stringify(x.m)}));
  }
  watching.push({stop: s, direction: dirs.length ? dirs[0].v : '', dirs: dirs});
  renderWatch();
  draw();
}

function removeSlot(i) {
  watching.splice(i, 1);
  renderWatch();
  if (watching.length) draw(); else $('out').innerHTML = '';
}

function setSlotDir(i, value) {
  watching[i].direction = value;
  draw();
}

function renderWatch() {
  if (!watching.length) { $('watch').innerHTML = ''; return; }
  const rows = watching.map((w, i) => {
    const grouped = (w.stop.g || []).length > 1;
    const opts = w.dirs.length
      ? `<select onchange="setSlotDir(${i}, this.value)">` +
        (grouped ? '' : '<option value="">Both directions</option>') +
        w.dirs.map(d => `<option value='${esc(d.v)}'
             ${w.direction === d.v ? 'selected' : ''}>To ${esc(d.l)}</option>`).join('') +
        `</select>`
      : '';
    return `<div class="slot">
        <span class="num">${i + 1}</span>
        <span class="tag ${w.stop.m}">${MODE_TAG[w.stop.m]}</span>
        <span class="nm">${esc(w.stop.n)}</span>
        ${opts}
        <span class="x" onclick="removeSlot(${i})" title="remove">&times;</span>
      </div>`;
  }).join('');
  const note = watching.length >= MAX_SLOTS
    ? '<div class="full">All six slots used — remove one to add another.</div>' : '';
  $('watch').innerHTML = card('Watching (in order)', rows + note);
}

async function draw() {
  if (!watching.length) return;
  $('out').innerHTML = card('Tidbyt preview', '<div class="empty">Rendering…</div>');
  const payload = watching.map(w => ({stop: w.stop, direction: w.direction}));
  try {
    const r = await fetch('/api/render?slots=' + encodeURIComponent(JSON.stringify(payload)));
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const pages = Math.ceil(watching.length / 4);
    $('out').innerHTML = card('Tidbyt preview',
      `<div class="preview"><img src="${url}" alt="preview"></div>` +
      `<div class="live">
         <span class="dot ${live ? 'on' : ''}"></span>
         <label><input type="checkbox" ${live ? 'checked' : ''}
                onchange="setLive(this.checked)"> Auto-refresh every 30s</label>
         <span style="margin-left:auto">${pages} page${pages > 1 ? 's, 4s each' : ''} · updated ${new Date().toLocaleTimeString()}</span>
       </div>`);
  } catch (e) {
    $('out').innerHTML = card('Tidbyt preview', `<div class="err">${esc(e.message)}</div>`);
  }
}

const card = (t, body) => `<div class="card"><h2>${t}</h2>${body}</div>`;
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # the mock server already logs; keep this quiet

    def _send(self, body, ctype="application/json", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parts.query)

        if parts.path == "/":
            return self._send(PAGE, "text/html; charset=utf-8")

        if parts.path == "/api/geocode":
            term = (q.get("q") or [""])[0]
            try:
                url = "https://nominatim.openstreetmap.org/search?" + \
                    urllib.parse.urlencode({"q": term, "format": "json",
                                            "limit": 6, "countrycodes": "us"})
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    hits = json.load(resp)
                return self._send(json.dumps([
                    {"name": h.get("display_name", ""),
                     "lat": float(h["lat"]), "lon": float(h["lon"])}
                    for h in hits]))
            except Exception as exc:
                return self._send(json.dumps({"error": str(exc)}), status=502)

        if parts.path == "/api/stops":
            try:
                lat = float((q.get("lat") or ["0"])[0])
                lon = float((q.get("lon") or ["0"])[0])
            except ValueError:
                return self._send(json.dumps([]), status=400)
            return self._send(json.dumps(nearby(lat, lon, (q.get("mode") or [""])[0])))

        if parts.path == "/api/dests":
            try:
                lat = float((q.get("lat") or ["0"])[0])
                lon = float((q.get("lon") or ["0"])[0])
            except ValueError:
                return self._send(json.dumps([]), status=400)
            code = (q.get("code") or [""])[0]
            return self._send(json.dumps(destinations_for(lat, lon, code)))

        if parts.path == "/api/render":
            try:
                slots = json.loads((q.get("slots") or ["[]"])[0])
                if not slots:
                    return self._send("nothing selected", "text/plain", status=400)
                return self._send(render_slots(slots), "image/webp")
            except Exception as exc:
                return self._send(str(exc), "text/plain", status=500)

        self._send("not found", "text/plain", status=404)


def check_mock():
    """Prove the mock can answer before anyone clicks anything.

    The mock reads the same generated files the app does, so a change to their
    shape breaks it. When it raised mid-request the connection just closed,
    and the app reported a bare "EOF" -- which reads like an app bug rather
    than a stale fixture. Failing loudly at startup is cheaper to diagnose.
    """
    sample = None
    cells_dir = os.path.join(DATA, "cells")
    for fn in sorted(os.listdir(cells_dir)):
        with open(os.path.join(cells_dir, fn), encoding="utf-8") as fh:
            for stop in json.load(fh):
                if stop["m"] == "b":
                    sample = stop["c"]
                    break
        if sample:
            break
    if not sample:
        return

    try:
        body = urllib.parse.urlencode({"token": "x", "stop": sample}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/BUSDV2/getBusDV" % mock_server.PORT, data=body)
        with urllib.request.urlopen(req, timeout=10) as resp:
            trips = json.load(resp).get("DVTrip", [])
        if not trips:
            print("  WARNING: mock returned no departures for stop %s" % sample,
                  file=sys.stderr)
        else:
            print("  mock self-check ok (stop %s -> %d departures)"
                  % (sample, len(trips)), file=sys.stderr)
    except Exception as exc:
        print("  WARNING: mock self-check failed: %s" % exc, file=sys.stderr)
        print("  Bus previews will fail. The mock reads data/v1/ -- "
              "did its format change?", file=sys.stderr)


def main():
    if not os.path.isdir(DATA):
        sys.exit("No generated data -- run: python3 pipeline/build_index.py")
    try:
        refresh_dev_copy()
    except Exception as exc:
        sys.exit(str(exc))
    if not os.path.exists(DEV_APP):
        sys.exit("No dev copy -- run: python3 pipeline/make_dev_copy.py")

    # The rendered app talks to the mock API, so bring it up alongside.
    mock = HTTPServer(("127.0.0.1", mock_server.PORT), mock_server.Handler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print("Mock NJ Transit API on http://127.0.0.1:%d" % mock_server.PORT,
          file=sys.stderr)

    check_mock()
    print("Dev UI ready:  http://127.0.0.1:%d" % PORT, file=sys.stderr)

    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
