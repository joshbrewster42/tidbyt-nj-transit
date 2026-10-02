"""Which scheduled bus departures have a vehicle actually out there.

NJ Transit's departure-board feed (BUSDV2, what the app calls directly) has
gaps: on 2026-10-01 it returned no imminent 159R at stop 21923 even when asked
for route 159 specifically, while NJ Transit's own app showed one six minutes
away with a vehicle number. The timetable has those departures, but a timetable
cannot say whether the bus is real.

GTFS-Realtime VehiclePositions can, and it is available on the same credentials
-- but it is protobuf, which pixlet cannot decode, and its trip_ids match
nothing in the published GTFS. So this service sits between them:

  * decodes the protobuf
  * matches each vehicle to a scheduled trip the way OpenTripPlanner does,
    on (route, start time, service running today), since trip_id will not join
  * answers, per stop, which of today's scheduled departures are backed by a
    vehicle right now

87% of live vehicles match uniquely. An ambiguous match is dropped rather than
guessed: claiming a bus is tracked when it is not is worse than staying quiet,
because the app already shows the scheduled time either way.
"""

import io
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import csv
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

GTFS_API = "https://pcsdata.njtransit.com/api/GTFS"
BUS_FEED = "https://content.njtransit.com/sites/default/files/developers-resources/bus_data.zip"
TZ = ZoneInfo("America/New_York")

DB_PATH = os.environ.get("INDEX_DB", "/data/index.sqlite")
TRONBYT_DB = os.environ.get("TRONBYT_DB", "/tronbyt/tronbyt.db")
PORT = int(os.environ.get("PORT", "8081"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
INDEX_HOURS = int(os.environ.get("INDEX_HOURS", "24"))

_live = {}          # trip_id -> vehicle id
_live_at = None     # when that snapshot was taken
_lock = threading.Lock()

# Counters, surfaced on /health. The app treats any failure here as "no live
# data" and carries on silently, which is the right behaviour but means a
# broken integration looks identical to a quiet one. These make the difference
# visible without having to read nginx logs.
_stats = {"queries": 0, "stops": {}, "last_query": None}


def log(msg):
    print("%s %s" % (datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)


def credentials():
    """Read the NJ Transit login, preferring the one Tronbyt already stores.

    Keeping a second copy of the password in this service's environment would
    mean two places to rotate it. The Tronbyt volume is mounted read-only.
    """
    user, pw = os.environ.get("NJT_USER"), os.environ.get("NJT_PASS")
    if user and pw:
        return user, pw
    con = sqlite3.connect("file:%s?mode=ro" % TRONBYT_DB, uri=True)
    try:
        for (cfg,) in con.execute("select config from apps"):
            try:
                c = json.loads(cfg)
            except Exception:
                continue
            if c.get("njt_user") and c.get("njt_pass"):
                return c["njt_user"], c["njt_pass"]
    finally:
        con.close()
    raise RuntimeError("no NJ Transit credentials in env or the Tronbyt database")


# ---------------------------------------------------------------------------
# Static index
# ---------------------------------------------------------------------------

def build_index():
    """Rebuild the trip index from the published GTFS.

    Held in SQLite rather than memory: stop_times is 1.8M rows, and this shares
    a 2 GB box with Postgres and an API.
    """
    log("index: downloading bus feed")
    raw = urllib.request.urlopen(BUS_FEED, timeout=300).read()
    zf = zipfile.ZipFile(io.BytesIO(raw))

    def rows(name):
        with zf.open(name) as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
                yield r

    tmp = DB_PATH + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("pragma journal_mode=off")
    con.execute("pragma synchronous=off")
    con.execute("create table trip(trip_id text primary key, route text, service text, start text)")
    con.execute("create table dep(stop_code text, trip_id text, hhmm text, route text, service text)")
    con.execute("create table cal(date text, service text)")

    routes = {r["route_id"]: r["route_short_name"].strip() for r in rows("routes.txt")}
    trips = {}
    for t in rows("trips.txt"):
        trips[t["trip_id"]] = (routes.get(t["route_id"], ""), t.get("service_id", ""))
    log("index: %d trips" % len(trips))

    code_of = {}
    for s in rows("stops.txt"):
        c = (s.get("stop_code") or "").strip()
        if c:
            code_of[s["stop_id"]] = c

    starts = {}
    deps = []
    n = 0
    for st in rows("stop_times.txt"):
        n += 1
        tid = st["trip_id"]
        meta = trips.get(tid)
        if not meta:
            continue
        route, service = meta
        dep = (st.get("departure_time") or "").strip()
        if not dep:
            continue
        if st.get("stop_sequence") == "1":
            starts[tid] = dep[:8]
        code = code_of.get(st["stop_id"])
        if code:
            deps.append((code, tid, dep[:5], route, service))
        if len(deps) >= 200000:
            con.executemany("insert into dep values (?,?,?,?,?)", deps)
            deps = []
    if deps:
        con.executemany("insert into dep values (?,?,?,?,?)", deps)
    log("index: %d stop_times rows" % n)

    con.executemany("insert into trip values (?,?,?,?)",
                    [(tid, trips[tid][0], trips[tid][1], s) for tid, s in starts.items()])

    # NJ Transit ships only calendar_dates.txt: every operating day is listed.
    for cd in rows("calendar_dates.txt"):
        if cd.get("exception_type") == "1":
            con.execute("insert into cal values (?,?)", (cd["date"], cd["service_id"]))

    con.execute("create index dep_stop on dep(stop_code, service)")
    con.execute("create index trip_match on trip(route, start, service)")
    con.execute("create index cal_date on cal(date)")
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    log("index: rebuilt -> %s (%.0f MB)" % (DB_PATH, os.path.getsize(DB_PATH) / 1e6))


def services_today(con, when):
    """Service ids running on the given date."""
    d = when.strftime("%Y%m%d")
    return [r[0] for r in con.execute("select service from cal where date=?", (d,))]


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------

_token = {"value": None, "at": 0}


def token(user, pw):
    if _token["value"] and time.time() - _token["at"] < 20 * 3600:
        return _token["value"]
    body = urllib.parse.urlencode({"username": user, "password": pw}).encode()
    req = urllib.request.Request(GTFS_API + "/authenticateUser", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if not data.get("UserToken"):
        raise RuntimeError("authentication rejected")
    _token.update(value=data["UserToken"], at=time.time())
    log("auth: new token")
    return _token["value"]


def fetch_vehicles(tok):
    """VehiclePositions, decoded. multipart because that is what the API wants."""
    boundary = "----njtlive"
    body = ("--%s\r\nContent-Disposition: form-data; name=\"token\"\r\n\r\n%s\r\n--%s--\r\n"
            % (boundary, tok, boundary)).encode()
    req = urllib.request.Request(GTFS_API + "/getVehiclePositions", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(urllib.request.urlopen(req, timeout=60).read())
    out = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        if v.trip.route_id and v.trip.start_time:
            out.append((v.trip.route_id, v.trip.start_time, v.vehicle.id or "?"))
    return out


def poll_once():
    """Match every live vehicle to a scheduled trip, OTP-style."""
    user, pw = credentials()
    vehicles = fetch_vehicles(token(user, pw))
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        now = datetime.now(TZ)
        svcs = services_today(con, now)
        if not svcs:
            log("poll: no services today?")
            return
        marks = ",".join("?" * len(svcs))
        found, ambiguous, missed = {}, 0, 0
        for route, start, veh in vehicles:
            hits = [r[0] for r in con.execute(
                "select trip_id from trip where route=? and start=? and service in (%s)" % marks,
                [route, start] + svcs)]
            if len(hits) == 1:
                found[hits[0]] = veh
            elif hits:
                ambiguous += 1
            else:
                missed += 1
    finally:
        con.close()
    with _lock:
        global _live, _live_at
        _live, _live_at = found, datetime.now(TZ)
    log("poll: %d vehicles -> %d matched, %d ambiguous, %d unmatched"
        % (len(vehicles), len(found), ambiguous, missed))


def poller():
    while True:
        try:
            poll_once()
        except Exception as exc:          # a transient API failure must not kill the loop
            log("poll FAILED: %r" % exc)
        time.sleep(POLL_SECONDS)


def indexer():
    while True:
        try:
            if not os.path.exists(DB_PATH):
                build_index()
            else:
                age = time.time() - os.path.getmtime(DB_PATH)
                if age > INDEX_HOURS * 3600:
                    build_index()
        except Exception as exc:
            log("index FAILED: %r" % exc)
        time.sleep(3600)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            with _lock:
                age = (datetime.now(TZ) - _live_at).total_seconds() if _live_at else None
                top = sorted(_stats["stops"].items(), key=lambda kv: -kv[1])[:5]
                return self._send(200, {
                    "ok": _live_at is not None and age is not None and age < 300,
                    "tracked_trips": len(_live),
                    "seconds_since_poll": None if age is None else round(age),
                    "index": os.path.exists(DB_PATH),
                    "queries": _stats["queries"],
                    "last_query": _stats["last_query"],
                    "top_stops": dict(top),
                })
        if not path.startswith("/live/"):
            return self._send(404, {"error": "not found"})

        code = path[len("/live/"):]
        if not code.isdigit():
            return self._send(400, {"error": "stop code must be numeric"})

        with _lock:
            _stats["queries"] += 1
            _stats["stops"][code] = _stats["stops"].get(code, 0) + 1
            _stats["last_query"] = datetime.now(TZ).strftime("%H:%M:%S")

        with _lock:
            live, at = dict(_live), _live_at
        if not at or (datetime.now(TZ) - at).total_seconds() > 300:
            # Stale is worse than absent: the app would mark rows live using a
            # snapshot from an hour ago.
            return self._send(503, {"error": "no recent vehicle data"})

        try:
            con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        except sqlite3.OperationalError:
            return self._send(503, {"error": "index not built yet"})
        try:
            now = datetime.now(TZ)
            svcs = services_today(con, now)
            if not svcs:
                return self._send(200, {"live": []})
            marks = ",".join("?" * len(svcs))
            rows = con.execute(
                "select trip_id, hhmm, route from dep where stop_code=? and service in (%s)" % marks,
                [code] + svcs).fetchall()
        finally:
            con.close()

        now_min = now.hour * 60 + now.minute
        out = []
        for trip_id, hhmm, route in rows:
            veh = live.get(trip_id)
            if not veh:
                continue
            h, m = hhmm.split(":")
            wait = int(h) * 60 + int(m) - now_min
            if -10 <= wait <= 180:
                out.append({"t": hhmm, "r": route, "v": veh, "w": wait})
        out.sort(key=lambda d: d["w"])
        self._send(200, {"live": out, "age": round((now - at).total_seconds())})


def main():
    threading.Thread(target=indexer, daemon=True).start()
    while not os.path.exists(DB_PATH):
        log("waiting for the first index build")
        time.sleep(10)
    threading.Thread(target=poller, daemon=True).start()
    log("listening on :%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
