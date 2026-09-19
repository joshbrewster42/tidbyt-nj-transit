#!/usr/bin/env python3
"""
Build the static data the NJ Transit Tidbyt app consumes.

NJ Transit publishes two GTFS bundles with no registration required:

    bus_data.zip   ~50 MB   263 bus routes,      16.5k stops
    rail_data.zip  ~6 MB    14 rail + 3 light rail routes, 230 stops

A Tidbyt app is a single sandboxed Starlark file with no filesystem, so it
cannot unzip or stream a GTFS bundle at render time. Instead this script
pre-chews the feeds into small JSON files that get committed to the repo and
served over raw.githubusercontent.com:

    data/v1/meta.json                build metadata + feed versions
    data/v1/cells/<lat>_<lon>.json   stops bucketed into 0.1-degree grid cells
    data/v1/lr/calendar.json         date -> active light rail service ids
    data/v1/lr/<stop_id>.json        light rail timetable for one stop

The grid cells are what make "find stops near my address" cheap. The app's
schema.LocationBased handler converts a lat/lon into a cell key, fetches at
most a handful of small files, and ranks by haversine distance -- instead of
downloading an index of 16.5k stops.

Bus departures come from the realtime API at render time, so bus stops only
need geography here. Light rail has no comparable public realtime feed, so its
timetables are precomputed -- only 65 stops, which stays small.

Usage:
    python3 pipeline/build_index.py              # download feeds, build
    python3 pipeline/build_index.py --cache DIR  # reuse already-downloaded zips
"""

import argparse
import csv
import io
import json
import math
import os
import shutil
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

FEEDS = {
    "bus": "https://content.njtransit.com/sites/default/files/developers-resources/bus_data.zip",
    "rail": "https://content.njtransit.com/sites/default/files/developers-resources/rail_data.zip",
}

# GTFS route_type values we care about. NJ Transit's three light rail lines
# (Hudson-Bergen, Newark Light Rail, River LINE) live in the *rail* feed as
# type 0, alongside commuter rail as type 2.
ROUTE_TYPE_LIGHT_RAIL = "0"
ROUTE_TYPE_BUS = "3"

# 0.1 degrees is roughly 11 km of latitude and 8.5 km of longitude at NJ's
# latitude. Big enough that one cell almost always holds nearby stops, small
# enough that a cell file stays a few KB.
CELL_SIZE = 0.1

# GTFS direction_id splits a route into its two directions of travel, which is
# what a rider actually means by "which way". One direction can still end at
# several terminals -- the 159 outbound reaches Fort Lee, Cliffside Park and
# Fairview -- so each direction gets one representative label for display and
# keeps the full set for matching departures against.
MAX_MATCH_TERMINALS = 20

# Light rail timetables are only useful for so long, and NJ Transit's
# calendar_dates.txt is explicit (there is no calendar.txt), so we simply keep
# every date the feed declares.
OUT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "v1")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def cell_key(lat, lon):
    """Grid cell containing a point. Must match cell_key() in the .star app."""
    return "%d_%d" % (math.floor(lat / CELL_SIZE), math.floor(lon / CELL_SIZE))


def fetch_feed(name, url, cache_dir):
    """Return a zipfile.ZipFile for a feed, downloading unless cached."""
    if cache_dir:
        path = os.path.join(cache_dir, "%s_data.zip" % name)
        if os.path.exists(path):
            log("  %s: using cached %s (%.1f MB)" % (name, path, os.path.getsize(path) / 1e6))
            return zipfile.ZipFile(path)

    log("  %s: downloading %s" % (name, url))
    with urllib.request.urlopen(url, timeout=300) as resp:
        raw = resp.read()
    log("  %s: downloaded %.1f MB" % (name, len(raw) / 1e6))

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, "%s_data.zip" % name)
        with open(path, "wb") as fh:
            fh.write(raw)

    return zipfile.ZipFile(io.BytesIO(raw))


def read_csv(zf, name):
    """Stream a GTFS table as dicts. stop_times.txt is 77 MB, so never slurp."""
    with zf.open(name) as fh:
        for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")):
            yield row


# Words that read better lowercase in the middle of a stop name.
SMALL_WORDS = {"at", "and", "of", "the", "to", "via", "opp"}

# Noise that eats pixels without telling a rider anything. A Tidbyt row fits
# roughly a dozen characters, so "2nd Street Light Rail Station" needs to
# become "2nd Street".
NOISE_SUFFIXES = [
    "LIGHT RAIL STATION",
    "LIGHT RAIL",
    "RAIL STATION",
    "LIGHT",
    "STATION",
]

# Compass points stay uppercase; everything else that happens to be two letters
# (ST, RD, PL) is an abbreviation that reads better capitalised.
DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}

ABBREVIATIONS = {
    "TRANSP": "Transp",
    "CENT": "Ctr",
    "CTR": "Ctr",
    "TERMINAL": "Term",
    "AVENUE": "Ave",
    "STREET": "St",
    "HIGHWAY": "Hwy",
    "BOULEVARD": "Blvd",
    "PARKWAY": "Pkwy",
}


def title_case(name):
    """GTFS names are SHOUTED. Tidbyt has 64 pixels; mixed case reads better."""
    out = []
    for i, word in enumerate(name.split()):
        upper = word.upper()
        if upper in ABBREVIATIONS:
            out.append(ABBREVIATIONS[upper])
        elif i > 0 and word.lower() in SMALL_WORDS:
            out.append(word.lower())
        elif upper in DIRECTIONALS:
            out.append(upper)
        else:
            out.append(word.capitalize())
    return " ".join(out)


def strip_noise(name):
    """Drop trailing boilerplate like 'LIGHT RAIL STATION'."""
    up = name.upper().strip()
    changed = True
    while changed:
        changed = False
        for suffix in NOISE_SUFFIXES:
            if up.endswith(" " + suffix):
                up = up[: -(len(suffix) + 1)].strip()
                changed = True
    return up or name.upper().strip()


def clean_headsign(headsign, route):
    """'HBLR WEST SIDE AVENUE' -> 'West Side Ave'.

    Every light rail headsign repeats its own route code, which the app already
    shows as a colored badge, so it is pure waste on screen.
    """
    text = headsign.strip()
    if text.upper().startswith(route.upper() + " "):
        text = text[len(route) + 1:]
    return title_case(strip_noise(text))


# Bus headsigns carry a lot of operational noise: the route code they already
# belong to, a fare notice, and a "VIA <somewhere>" qualifier that splits one
# destination into several. Stripping all three collapses
#   "119 NEW YORK-Exact Fare"
#   "119J NEW YORK VIA JOURNAL SQUARE-Exact Fare"
# to a single "New York", which is what a rider actually picks between.
FARE_NOTICES = ("-EXACT FARE", "- EXACT FARE", "EXACT FARE")


def dest_label(headsign, route):
    """Reduce a GTFS headsign to the destination a rider would name."""
    text = headsign.strip().upper()

    # Leading route code, including branch letters: "119J NEW YORK" -> "NEW YORK".
    parts = text.split()
    if parts and route:
        head = parts[0]
        if head == route.upper() or (head.startswith(route.upper()) and
                                     head[len(route):].isalpha()):
            text = " ".join(parts[1:])

    for notice in FARE_NOTICES:
        if text.endswith(notice):
            text = text[: -len(notice)].strip(" -")

    # "NEW YORK VIA JOURNAL SQUARE" -> "NEW YORK"
    if " VIA " in text:
        text = text.split(" VIA ")[0]

    return title_case(strip_noise(text.strip())) or title_case(headsign)


def build_bus(zf):
    """Bus stop geography, plus which way each stop faces.

    Nearly every bus stop is on one side of a street and therefore serves a
    single direction of travel: of the stops on routes 156/158/159, 279 of 295
    are one-directional. A street corner shows up as two stops with the same
    name and different stop_codes -- which is useless in a picker unless each
    one says where its buses are headed.
    """
    routes = {}
    for r in read_csv(zf, "routes.txt"):
        routes[r["route_id"]] = r["route_short_name"].strip()

    trip_info = {}
    for t in read_csv(zf, "trips.txt"):
        route_name = routes.get(t["route_id"], "")
        trip_info[t["trip_id"]] = (
            t["route_id"],
            t.get("direction_id", "0"),
            dest_label(t["trip_headsign"], route_name),
        )

    log("  bus: %d routes, %d trips" % (len(routes), len(trip_info)))

    # The expensive pass: 77 MB of stop_times. Keep only which routes serve a
    # stop and, per direction of travel, how often each terminal appears.
    stop_routes = defaultdict(set)
    stop_dirs = defaultdict(lambda: defaultdict(Counter))
    seen = 0
    for st in read_csv(zf, "stop_times.txt"):
        seen += 1
        if seen % 2_000_000 == 0:
            log("    ...%d stop_times rows" % seen)
        entry = trip_info.get(st["trip_id"])
        if entry:
            rid, direction, dest = entry
            name = routes.get(rid)
            if name:
                stop_routes[st["stop_id"]].add(name)
                if dest:
                    stop_dirs[st["stop_id"]][direction][dest] += 1
    log("  bus: scanned %d stop_times rows" % seen)

    # Seed the known-place set from every terminal in the feed, so labels can
    # be shortened safely.
    for per_direction in stop_dirs.values():
        for counter in per_direction.values():
            for name in counter:
                KNOWN_PLACES.add(strip_label_noise(name))
    log("  bus: %d distinct terminals" % len(KNOWN_PLACES))

    stops = []
    dests = {}
    skipped_no_code = 0
    one_way = 0
    for s in read_csv(zf, "stops.txt"):
        # The realtime API keys off the rider-facing 5-digit stop_code printed
        # on the bus stop sign, not the internal stop_id. A stop without one
        # cannot be queried, so it is useless to us.
        code = (s.get("stop_code") or "").strip()
        if not code:
            skipped_no_code += 1
            continue
        served = stop_routes.get(s["stop_id"])
        if not served:
            continue
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (TypeError, ValueError):
            continue
        if lat == 0.0 and lon == 0.0:
            continue

        directions = summarise_directions(stop_dirs.get(s["stop_id"], {}))
        if len(directions) <= 1:
            one_way += 1

        stops.append({
            "c": code,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "n": title_case(strip_noise(s["stop_name"])),
            "m": "b",
            "r": sorted(served, key=_route_sort_key),
            # Where buses from this stop are headed, so two stops sharing a
            # name are tellable apart in the picker.
            "t": " / ".join(d["l"] for d in directions[:2]),
        })
        if directions:
            dests[code] = directions

    log("  bus: %d usable stops (%d skipped: no stop_code)" % (len(stops), skipped_no_code))
    log("  bus: %d of %d stops serve a single direction" % (one_way, len(stops)))
    return stops, dests


# Operational detail that NJ Transit's own MyBus direction names leave off.
# "Fairview Njt Garage" is the Fairview direction; the garage is where the bus
# sleeps, not a place a rider is going.
LABEL_NOISE = [
    "NJT GARAGE", "GARAGE", "PARK RIDE", "PARK/RIDE", "P/R",
    "PARK & RIDE", "TERMINAL", "TERM",
]

# Almost every New Jersey municipality is one or two words, so a name that long
# is already the place itself and must be left alone. Anything longer carries
# detail on top of a place -- "Fort Lee Linwood Park" -- and can be trimmed back
# to the "Fort Lee" that MyBus shows.
PLACE_NAME_WORDS = 2


# Every terminal name in the feed. A long name can be shortened to its first
# words only when that shorter form is itself somewhere a bus goes -- which is
# what separates "Fort Lee Linwood Park" -> "Fort Lee" (Fort Lee is a real
# terminal) from "West New York" -> "West New" (it is not).
KNOWN_PLACES = set()


def strip_label_noise(text):
    up = text.upper()
    for noise in LABEL_NOISE:
        if up.endswith(" " + noise):
            return text[: -(len(noise) + 1)].strip()
    return text


def direction_label(counter):
    """The place a direction heads toward, named the way MyBus names it.

    NJ Transit's own MyBus offers exactly two directions per route and names
    them by place: the 159 is "New York" or "Fort Lee". Our busiest headsign is
    "Fort Lee Linwood Park", which is the same direction said too precisely.

    So take the busiest terminal, drop operational detail like a garage name,
    then shorten toward a plainer place name -- but only to a form that is
    itself a terminal somewhere in the network. That guard is what stops
    "West New York" becoming "West New".
    """
    if not counter:
        return ""

    label = strip_label_noise(counter.most_common(1)[0][0])
    words = label.split()

    # A one or two word name is already a municipality -- "Englewood Cliffs"
    # must not be shortened to "Englewood", which is a different town. Only
    # names carrying extra detail beyond the place get trimmed.
    if len(words) <= PLACE_NAME_WORDS:
        return label

    # Prefer the longest recognisable place, so "Fort Lee Linwood Park" lands
    # on "Fort Lee" rather than "Fort".
    for take in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:take])
        if candidate in KNOWN_PLACES:
            return candidate
    return label


def summarise_directions(by_direction):
    """One entry per direction of travel, newest-busiest terminal as its label.

    Returns [{"l": label, "m": [terminals to match departures against]}].
    The label names the place, matching NJ Transit's own MyBus direction names;
    the match list keeps every terminal in that direction, so a filter on it
    still catches the Cliffside Park and Fairview runs that share the heading.
    """
    out = []
    for direction in sorted(by_direction.keys()):
        counter = by_direction[direction]
        if not counter:
            continue
        ranked = counter.most_common(MAX_MATCH_TERMINALS)
        out.append({
            "l": direction_label(counter),
            "m": [name for (name, _count) in ranked],
        })

    # Two directions that resolve to the same label tell a rider nothing.
    if len(out) == 2 and out[0]["l"] == out[1]["l"]:
        merged = out[0]["m"] + [m for m in out[1]["m"] if m not in out[0]["m"]]
        return [{"l": out[0]["l"], "m": merged[:MAX_MATCH_TERMINALS]}]
    return out


def _route_sort_key(name):
    """Sort 1, 2, 10, 119 numerically but keep AC1/GO25 style names sane."""
    digits = "".join(ch for ch in name if ch.isdigit())
    return (0 if name.isdigit() else 1, int(digits) if digits else 0, name)


def build_light_rail(zf):
    """Light rail stops plus full precomputed timetables."""
    lr_routes = {}
    for r in read_csv(zf, "routes.txt"):
        if r["route_type"].strip() == ROUTE_TYPE_LIGHT_RAIL:
            lr_routes[r["route_id"]] = {
                "name": r["route_short_name"].strip(),
                "long": r["route_long_name"].strip(),
                "color": (r.get("route_color") or "").strip() or "FFFFFF",
            }
    log("  light rail: %d routes (%s)" % (
        len(lr_routes), ", ".join(v["name"] for v in lr_routes.values())))

    trips = {}
    for t in read_csv(zf, "trips.txt"):
        if t["route_id"] in lr_routes:
            route_name = lr_routes[t["route_id"]]["name"]
            trips[t["trip_id"]] = {
                "route": route_name,
                "head": clean_headsign(t["trip_headsign"], route_name),
                "svc": t["service_id"],
                "dir": t.get("direction_id", "0"),
            }
    log("  light rail: %d trips" % len(trips))

    # stop_id -> service_id -> list of (departure_time, route, headsign)
    per_stop = defaultdict(lambda: defaultdict(list))
    stop_routes = defaultdict(set)
    stop_dirs = defaultdict(lambda: defaultdict(Counter))
    for st in read_csv(zf, "stop_times.txt"):
        trip = trips.get(st["trip_id"])
        if not trip:
            continue
        dep = (st.get("departure_time") or "").strip()
        if not dep:
            continue
        # GTFS allows hours >= 24 for trips after midnight; keep them as-is and
        # let the app normalise, so a 24:15 departure still sorts after 23:50.
        per_stop[st["stop_id"]][trip["svc"]].append((dep[:5], trip["route"], trip["head"]))
        stop_routes[st["stop_id"]].add(trip["route"])
        stop_dirs[st["stop_id"]][trip["dir"]][trip["head"]] += 1

    # date -> active service ids. NJ Transit ships only calendar_dates.txt,
    # where exception_type 1 means "service added on this date".
    calendar = defaultdict(list)
    for cd in read_csv(zf, "calendar_dates.txt"):
        if cd["exception_type"].strip() == "1":
            calendar[cd["date"].strip()].append(cd["service_id"].strip())

    # Trim the calendar to services light rail actually uses, so the file the
    # app downloads stays small.
    lr_services = {t["svc"] for t in trips.values()}
    calendar = {
        date: sorted(s for s in svcs if s in lr_services)
        for date, svcs in calendar.items()
    }
    calendar = {d: s for d, s in calendar.items() if s}
    log("  light rail: %d service dates" % len(calendar))

    stops = []
    timetables = {}
    lr_dests = {}
    for s in read_csv(zf, "stops.txt"):
        sid = s["stop_id"]
        if sid not in per_stop:
            continue
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (TypeError, ValueError):
            continue
        name = title_case(strip_noise(s["stop_name"]))

        # Dedupe headsigns into a table; they repeat hundreds of times per stop
        # and are the bulk of the bytes otherwise.
        heads = sorted({h for svc in per_stop[sid].values() for (_, _, h) in svc})
        directions = summarise_directions(stop_dirs.get(sid, {}))

        stops.append({
            "c": sid,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "n": name,
            "m": "l",
            "r": sorted(stop_routes[sid]),
            "t": " / ".join(d["l"] for d in directions[:2]),
        })
        # Light rail headsigns are already cleaned, so these match what the
        # app renders exactly -- filtering is an equality test, not a guess.
        if directions:
            lr_dests[sid] = directions
        head_idx = {h: i for i, h in enumerate(heads)}
        svc_dep = {}
        for svc, deps in per_stop[sid].items():
            svc_dep[svc] = [[t, r, head_idx[h]] for (t, r, h) in sorted(deps)]
        timetables[sid] = {
            "n": name,
            "heads": heads,
            "svc": svc_dep,
        }

    log("  light rail: %d stops with timetables" % len(stops))
    return stops, timetables, calendar, lr_routes, lr_dests


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        # Compact separators: these files are downloaded by the app, not read
        # by humans, and the savings are meaningful across 1000+ cell files.
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", metavar="DIR",
                    help="directory to cache the downloaded GTFS zips in")
    ap.add_argument("--out", default=OUT_ROOT, help="output directory")
    args = ap.parse_args()

    log("Fetching GTFS feeds...")
    bus_zf = fetch_feed("bus", FEEDS["bus"], args.cache)
    rail_zf = fetch_feed("rail", FEEDS["rail"], args.cache)

    log("Building bus stop geography...")
    bus_stops, bus_dests = build_bus(bus_zf)

    log("Building light rail timetables...")
    lr_stops, timetables, calendar, lr_routes, lr_dests = build_light_rail(rail_zf)

    # Wipe previous output so removed stops don't linger as stale files.
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)

    log("Writing grid cells...")
    cells = defaultdict(list)
    for stop in bus_stops + lr_stops:
        cells[cell_key(stop["lat"], stop["lon"])].append(stop)

    total_bytes = 0
    for key, stops in cells.items():
        # Light rail first, then by route count: denser stops are likelier to
        # be what someone means when they pick a spot on a map.
        stops.sort(key=lambda s: (s["m"] != "l", -len(s["r"]), s["n"]))
        total_bytes += write_json(os.path.join(args.out, "cells", "%s.json" % key), stops)

    log("Writing destination lists...")
    all_dests = {}
    all_dests.update(bus_dests)
    all_dests.update(lr_dests)
    dest_cells = defaultdict(dict)
    for stop in bus_stops + lr_stops:
        found = all_dests.get(stop["c"])
        if found:
            dest_cells[cell_key(stop["lat"], stop["lon"])][stop["c"]] = found
    for key, mapping in dest_cells.items():
        total_bytes += write_json(os.path.join(args.out, "dirs", "%s.json" % key), mapping)

    log("Writing light rail timetables...")
    for sid, tt in timetables.items():
        total_bytes += write_json(os.path.join(args.out, "lr", "%s.json" % sid), tt)
    total_bytes += write_json(os.path.join(args.out, "lr", "calendar.json"), calendar)

    meta = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cell_size": CELL_SIZE,
        "counts": {
            "bus_stops": len(bus_stops),
            "light_rail_stops": len(lr_stops),
            "cells": len(cells),
            "max_match_terminals": MAX_MATCH_TERMINALS,
            "service_dates": len(calendar),
        },
        "light_rail_routes": {
            v["name"]: {"long": v["long"], "color": v["color"]}
            for v in lr_routes.values()
        },
    }
    total_bytes += write_json(os.path.join(args.out, "meta.json"), meta)

    log("")
    log("Done. %d cells, %d bus stops, %d light rail stops, %.1f MB total." % (
        len(cells), len(bus_stops), len(lr_stops), total_bytes / 1e6))
    biggest = max(cells.items(), key=lambda kv: len(kv[1]))
    log("Densest cell %s holds %d stops." % (biggest[0], len(biggest[1])))


if __name__ == "__main__":
    main()
