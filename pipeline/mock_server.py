#!/usr/bin/env python3
"""
Local stand-in for the NJ Transit realtime bus API, for development.

The real BUSDV2 API needs credentials that take days to be granted, and its
exact response shape is only documented second-hand. This server lets the bus
rendering path be exercised end to end before any of that arrives:

    python3 pipeline/mock_server.py

It serves two things on http://127.0.0.1:8777:

    /data/v1/...                  the generated static files, as GitHub would
    /api/BUSDV2/authenticateUser  a fake token
    /api/BUSDV2/getBusDV          plausible departures for the requested stop

Departures are generated from the *real* route list for the stop being asked
about, so a stop served by the 1, 25 and 40 produces departures on those
routes. That makes the multi-route layout -- badges, contrast, column widths --
testable with realistic data.

This mocks the response SHAPE as documented. It cannot confirm the real field
names; only a live response can do that.
"""

import json
import os
import random
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "v1")
PORT = 8777

FAKE_TOKEN = "mock-user-token-not-a-real-credential"

# Plausible destinations per route, so headers are not all identical.
DESTINATIONS = [
    "NEWARK PENN STATION",
    "JERSEY CITY JOURNAL SQ",
    "HOBOKEN TERMINAL",
    "NEW YORK PORT AUTHORITY",
    "IRVINGTON CENTER",
    "BAYONNE 8TH ST",
    "PATERSON BROADWAY",
    "ELIZABETH CENTER",
]

_stop_routes_cache = {}


def routes_for_stop(stop_code):
    """Look up which routes really serve a stop, from the generated cells."""
    if not _stop_routes_cache:
        cells_dir = os.path.join(ROOT, "cells")
        if os.path.isdir(cells_dir):
            for fn in os.listdir(cells_dir):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(cells_dir, fn), encoding="utf-8") as fh:
                    for stop in json.load(fh):
                        _stop_routes_cache[stop["c"]] = stop
    stop = _stop_routes_cache.get(str(stop_code))
    if not stop:
        return []
    return stop.get("r", [])


def make_departures(stop_code, seed=None):
    """Build a DVTrip array shaped like the documented BUSDV2 response."""
    routes = routes_for_stop(stop_code)
    if not routes:
        return []

    rng = random.Random(seed if seed is not None else stop_code)
    trips = []
    minutes = 0
    # Cycle through the routes that actually serve this stop so the display
    # shows a realistic mix rather than one route repeated.
    for i in range(min(6, max(3, len(routes)))):
        route = routes[i % len(routes)]
        minutes += rng.randint(1, 12)

        # Roughly one in five buses is not transmitting, so it falls back to
        # its scheduled time -- the same split the real feed shows.
        transmitting = rng.random() > 0.2
        trips.append({
            "public_route": route,
            "header": rng.choice(DESTINATIONS),
            "lanegate": "",
            "departuretime": ("%d min" % minutes) if transmitting else "",
            "sched_dep_time": "%d min" % minutes,
        })
    return trips


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("  mock: %s\n" % (fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read the request body, whether it is length-delimited or chunked.

        pixlet's multipart encoder streams the body, so it arrives chunked with
        no Content-Length. Reading Content-Length bytes gets you nothing.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            for _ in range(10000):  # guard against a malformed stream
                line = self.rfile.readline().strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length)

    def do_POST(self):
        raw = self._read_body().decode("utf-8", "replace")

        if self.path.endswith("/authenticateUser"):
            return self._json({"Authenticated": "True", "UserToken": FAKE_TOKEN})

        if self.path.endswith("/getBusDV"):
            # Pull the stop out of either form encoding without a parser: the
            # real client sends multipart, but urlencoded is easier to poke at
            # by hand and both are trivially greppable.
            match = re.search(r'name="stop"\r?\n\r?\n([^\r\n]*)', raw) or \
                re.search(r'(?:^|&)stop=([^&]*)', raw)
            stop = match.group(1).strip() if match else ""
            if os.environ.get("MOCK_DEBUG"):
                sys.stderr.write("  mock: raw body >>>\n%s\n<<< parsed stop=%r\n"
                                 % (raw, stop))
            trips = make_departures(stop)
            if not trips:
                return self._json({"message": "no scheduled service", "DVTrip": []})
            return self._json({"message": "", "DVTrip": trips})

        self._json({"message": "unknown endpoint"}, status=404)


def main():
    if not os.path.isdir(ROOT):
        sys.exit("No generated data at %s -- run build_index.py first." % ROOT)
    print("Mock NJ Transit API + data on http://127.0.0.1:%d" % PORT, file=sys.stderr)
    print("  static: /cells/... /lr/...", file=sys.stderr)
    print("  api:    /api/BUSDV2/authenticateUser, /api/BUSDV2/getBusDV", file=sys.stderr)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
