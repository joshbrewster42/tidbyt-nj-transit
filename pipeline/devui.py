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
DEV_APP = os.path.join(ROOT, ".dev", "nj_transit_dev.star")

PORT = 8090
CELL_SIZE = 0.1
USER_AGENT = "tidbyt-nj-transit-dev/1.0 (local development helper)"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearby(lat, lon, limit=25):
    """Mirror the app's own cell lookup so results match the real picker."""
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
        d = haversine_km(lat, lon, s["lat"], s["lon"])
        item = dict(s)
        item["mi"] = round(d * 0.621371, 2)
        scored.append(item)
    scored.sort(key=lambda s: s["mi"])
    return scored[:limit]


def render_stop(stop_obj):
    """Render the real app for one stop and return the image bytes."""
    if not os.path.exists(DEV_APP):
        raise RuntimeError(
            "Missing %s -- run: python3 pipeline/make_dev_copy.py" % DEV_APP)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.webp")
        proc = subprocess.run(
            ["pixlet", "render", os.path.basename(DEV_APP),
             "stop=" + json.dumps(stop_obj), "--magnify", "6", "-o", out],
            cwd=os.path.dirname(DEV_APP),
            capture_output=True, text=True, timeout=60)
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
  .tag.b { background: var(--amber); } .tag.l { background: #2bb3b3; }
  .preview { padding: 18px 15px; text-align: center; }
  .preview img { image-rendering: pixelated; max-width: 100%;
                 border-radius: 5px; border: 1px solid var(--line); }
  .err { color: #ff8787; font-size: 13px; padding: 14px 15px;
         white-space: pre-wrap; font-family: ui-monospace, monospace; }
  .empty { color: var(--dim); padding: 14px 15px; font-size: 14px; }
  .coords { color: var(--dim); font-size: 12px; padding: 0 15px 12px;
            font-variant-numeric: tabular-nums; }
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
  <div id="stops"></div>
  <div id="out"></div>
</div>

<script>
const $ = id => document.getElementById(id);
let timer = null, lastStops = [];

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

async function pick(lat, lon) {
  $('places').innerHTML = '';
  $('hint').textContent = 'Stops near that address:';
  const r = await fetch(`/api/stops?lat=${lat}&lon=${lon}`);
  lastStops = await r.json();
  if (!lastStops.length) {
    $('stops').innerHTML = card('Nearby stops',
      '<div class="empty">No stops within range. Is the address in New Jersey?</div>');
    return;
  }
  $('stops').innerHTML = card('Nearby stops',
    `<div class="coords">${lat.toFixed(5)}, ${lon.toFixed(5)}</div>` +
    lastStops.map((s, i) =>
    `<div class="row" id="s${i}" onclick="show(${i})">
       <span class="mi">${s.mi.toFixed(1)} mi</span>
       <span class="tag ${s.m}">${s.m === 'l' ? 'RAIL' : 'BUS'}</span>
       <span class="nm">${esc(s.n)}<small>${esc((s.r || []).join(', '))}</small></span>
     </div>`).join(''));
  show(0);
}

async function show(i) {
  document.querySelectorAll('.row.on').forEach(e => e.classList.remove('on'));
  const el = $('s' + i); if (el) el.classList.add('on');
  const s = lastStops[i];
  $('out').innerHTML = card('Tidbyt preview', '<div class="empty">Rendering…</div>');
  try {
    const r = await fetch('/api/render?stop=' + encodeURIComponent(JSON.stringify(s)));
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    $('out').innerHTML = card('Tidbyt preview',
      `<div class="preview"><img src="${URL.createObjectURL(blob)}" alt="preview"></div>`);
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
            return self._send(json.dumps(nearby(lat, lon)))

        if parts.path == "/api/render":
            raw = (q.get("stop") or [""])[0]
            try:
                stop = json.loads(raw)
                # The app only needs these; drop the UI's distance field.
                stop = {k: stop[k] for k in ("c", "n", "m", "r") if k in stop}
                return self._send(render_stop(stop), "image/webp")
            except Exception as exc:
                return self._send(str(exc), "text/plain", status=500)

        self._send("not found", "text/plain", status=404)


def main():
    if not os.path.isdir(DATA):
        sys.exit("No generated data -- run: python3 pipeline/build_index.py")
    if not os.path.exists(DEV_APP):
        sys.exit("No dev copy -- run: python3 pipeline/make_dev_copy.py")

    # The rendered app talks to the mock API, so bring it up alongside.
    mock = HTTPServer(("127.0.0.1", mock_server.PORT), mock_server.Handler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print("Mock NJ Transit API on http://127.0.0.1:%d" % mock_server.PORT,
          file=sys.stderr)
    print("Dev UI ready:  http://127.0.0.1:%d" % PORT, file=sys.stderr)

    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
