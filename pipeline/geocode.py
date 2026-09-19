#!/usr/bin/env python3
"""
Turn an address into coordinates to paste into `pixlet serve`.

pixlet serve renders schema.Location as bare latitude/longitude inputs -- its
"Locality" box is cosmetic and does not geocode. The real address search only
exists in the Tidbyt mobile app. This bridges that gap for local testing.

    python3 pipeline/geocode.py "1 Hudson Pl, Hoboken NJ"
    python3 pipeline/geocode.py --stops "1 Hudson Pl, Hoboken NJ"

With --stops it also prints the stops the app would offer for that point,
reading the generated data directly, so you can check the picker without
clicking through the UI.

Geocoding uses OpenStreetMap's Nominatim, which means the address is sent to
their servers. It never goes anywhere else, and nothing is stored locally.
Skip it entirely by passing coordinates you already have:

    python3 pipeline/geocode.py --stops 40.7352 -74.0277
"""

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "v1")
CELL_SIZE = 0.1  # must match build_index.py and the .star app

NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Nominatim's usage policy requires an identifying User-Agent.
USER_AGENT = "tidbyt-nj-transit-dev/1.0 (local development helper)"


def geocode(address):
    query = urllib.parse.urlencode({
        "q": address,
        "format": "json",
        "limit": 1,
        # Bias toward the region the app covers.
        "countrycodes": "us",
    })
    req = urllib.request.Request("%s?%s" % (NOMINATIM, query),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        results = json.load(resp)

    if not results:
        sys.exit("No match for %r. Try adding the town and state." % address)

    hit = results[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", "")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearby(lat, lon, limit=12):
    """Mirror the app's nearest-stop search against the generated data."""
    ci = math.floor(lat / CELL_SIZE)
    cj = math.floor(lon / CELL_SIZE)
    di = 1 if (lat / CELL_SIZE - ci) >= 0.5 else -1
    dj = 1 if (lon / CELL_SIZE - cj) >= 0.5 else -1

    stops = []
    for (i, j) in [(ci, cj), (ci + di, cj), (ci, cj + dj), (ci + di, cj + dj)]:
        path = os.path.join(ROOT, "cells", "%d_%d.json" % (i, j))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                stops.extend(json.load(fh))

    scored = [(haversine_km(lat, lon, s["lat"], s["lon"]), s) for s in stops]
    scored.sort(key=lambda pair: pair[0])
    return scored[:limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("where", nargs="+",
                    help='an address in quotes, or a "LAT LON" pair')
    ap.add_argument("--stops", action="store_true",
                    help="also list the stops the app would offer")
    args = ap.parse_args()

    # Two bare numbers means coordinates; anything else is an address.
    if len(args.where) == 2:
        try:
            lat, lon = float(args.where[0]), float(args.where[1])
            label = "(coordinates given)"
        except ValueError:
            lat, lon, label = geocode(" ".join(args.where))
    else:
        lat, lon, label = geocode(" ".join(args.where))

    print()
    print("  %s" % label)
    print()
    print("  Latitude   %.6f" % lat)
    print("  Longitude  %.6f" % lon)
    print()
    print("  Paste those into the Stop section of pixlet serve.")

    if args.stops:
        if not os.path.isdir(ROOT):
            sys.exit("No generated data -- run build_index.py first.")
        print()
        print("  Stops the app would offer here:")
        print()
        found = nearby(lat, lon)
        if not found:
            print("    (none within range -- is this address in New Jersey?)")
        for dist_km, s in found:
            kind = "Light Rail" if s["m"] == "l" else "Bus"
            print("    %4.1f mi  %-11s %-30s %s" % (
                dist_km * 0.621371, kind, s["n"], ", ".join(s.get("r", [])[:6])))
    print()


if __name__ == "__main__":
    main()
