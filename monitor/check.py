#!/usr/bin/env python3
"""Health check for NJ Departures, with push alerts through ntfy.

Runs from cron every ten minutes on the droplet. Each check is either ok or
failing, and a notification goes out only when one changes state: once when
it breaks, once when it recovers. A check still failing a day later is
re-announced, because the vehicle feed died on 2026-10-21 and nothing said so
for 33 days.

Checks, in the order they would make the display wrong:

  board    NJ Transit's departure board answers for the watched stop
  device   the Tidbyt has been seen by Tronbyt in the last 15 minutes
  tronbyt  the Tronbyt container reports healthy
  data     the weekly timetable rebuild is under 9 days old

The ntfy topic lives in ~/njtmonitor/ntfy_topic (mode 600), never in git:
anyone who knows a topic on ntfy.sh can read it and post to it.

    python3 check.py           run the checks
    python3 check.py --test    send one test notification and exit
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HOME = os.path.expanduser("~/njtmonitor")
TOPIC_FILE = os.path.join(HOME, "ntfy_topic")
STATE_FILE = os.path.join(HOME, "state.json")

STOP = "21923"
BUS_API = "https://pcsdata.njtransit.com/api/BUSDV2"
META_URL = "https://raw.githubusercontent.com/joshbrewster42/tidbyt-nj-transit/main/data/v1/meta.json"
DEVICE_ID = "tidbyt"
DOCKER = "/usr/bin/docker"

DEVICE_STALE = timedelta(minutes=15)
DATA_STALE = timedelta(days=9)
REMIND_EVERY = timedelta(hours=24)

# A board with no trips is only suspicious while buses should be running.
# Outside these hours an empty board is just night.
SERVICE_HOURS = (6, 21)

TZ = ZoneInfo("America/New_York")


def log(msg):
    print("%s %s" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def notify(title, body, priority, tags):
    """Post to ntfy, matching the 86Proof notifier's conventions."""
    with open(TOPIC_FILE) as fh:
        topic = fh.read().strip()
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=body.encode(),
        headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags},
    )
    urllib.request.urlopen(req, timeout=20).read()


def tronbyt_db():
    """Credentials and device last_seen, read from Tronbyt's database.

    The database sits in a Docker volume the host cannot read directly, so a
    throwaway container mounts it read-only and prints what is needed.
    """
    # Selected by app name: other installed apps may store their config as a
    # blob, and only this one carries the NJ Transit login.
    code = (
        "import sqlite3,json;"
        "c=sqlite3.connect('file:/d/tronbyt.db?mode=ro',uri=True);"
        "r=c.execute(\"select config from apps where name='nj_departures'\").fetchone();"
        "cfg=json.loads(r[0]) if r and r[0] else {};"
        "seen=c.execute('select last_seen from devices where id=?',(%r,)).fetchone();"
        "print(json.dumps({'user':cfg.get('njt_user'),'pass':cfg.get('njt_pass'),"
        "'seen':str(seen[0]) if seen and seen[0] else None}))" % DEVICE_ID
    )
    out = subprocess.run(
        [DOCKER, "run", "--rm", "-v", "tronbyt_data:/d:ro", "python:3-alpine", "python3", "-c", code],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError("could not read the Tronbyt database: " + out.stderr.strip()[-200:])
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Checks. Each returns (ok, detail).
# ---------------------------------------------------------------------------

def check_board(db):
    if not db.get("user") or not db.get("pass"):
        return False, "no NJ Transit credentials in the app config"

    body = urllib.parse.urlencode({"username": db["user"], "password": db["pass"]}).encode()
    req = urllib.request.Request(BUS_API + "/authenticateUser", data=body)
    try:
        auth = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as exc:
        return False, "authenticateUser failed: %s" % exc
    token = auth.get("UserToken")
    if not token or str(auth.get("Authenticated")).lower() != "true":
        return False, "NJ Transit rejected the login"

    boundary = "----njtmonitor"
    fields = {"token": token, "stop": STOP, "route": "", "direction": "", "ip": ""}
    parts = "".join(
        "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)
        for k, v in fields.items()
    ) + "--%s--\r\n" % boundary
    req = urllib.request.Request(
        BUS_API + "/getBusDV", data=parts.encode(),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as exc:
        return False, "getBusDV failed: %s" % exc

    trips = resp.get("DVTrip", []) if isinstance(resp, dict) else []
    hour = datetime.now(TZ).hour
    if not trips and SERVICE_HOURS[0] <= hour < SERVICE_HOURS[1]:
        msg = resp.get("message") if isinstance(resp, dict) else None
        return False, "stop %s returned no trips during service hours%s" % (
            STOP, " (%s)" % msg if msg else "")
    return True, "%d trips at stop %s" % (len(trips), STOP)


def check_device(db):
    seen = db.get("seen")
    if not seen:
        return False, "Tronbyt has no last_seen for device %r" % DEVICE_ID
    # Tronbyt stores nanoseconds ("15:01:16.63225212+00:00"), which
    # fromisoformat will not take, so trim the fraction to microseconds.
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", seen.strip())
    if not m:
        return False, "unreadable last_seen %r" % seen
    date, clock, frac, offset = m.groups()
    stamp = "%sT%s%s%s" % (date, clock, "." + frac[:6] if frac else "", offset or "")
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - when
    if age > DEVICE_STALE:
        return False, "the Tidbyt was last seen %d minutes ago" % (age.total_seconds() // 60)
    return True, "seen %ds ago" % age.total_seconds()


def check_tronbyt():
    out = subprocess.run(
        [DOCKER, "inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
         "tronbyt-web-1"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        return False, "Tronbyt container not found"
    state = out.stdout.strip()
    if not state.startswith("running") or "unhealthy" in state:
        return False, "Tronbyt container is %s" % state
    return True, state


def check_data():
    try:
        meta = json.loads(urllib.request.urlopen(META_URL, timeout=30).read())
    except Exception as exc:
        return False, "could not fetch meta.json: %s" % exc
    built = datetime.fromisoformat(meta["built"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - built
    if age > DATA_STALE:
        return False, "timetables last rebuilt %d days ago; the weekly workflow may have stopped" % age.days
    return True, "rebuilt %d days ago" % age.days


CHECKS = [
    ("board", "Departure board"),
    ("device", "Tidbyt connection"),
    ("tronbyt", "Tronbyt server"),
    ("data", "Timetable data"),
]


def run():
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}

    db = tronbyt_db()
    results = {
        "board": check_board(db),
        "device": check_device(db),
        "tronbyt": check_tronbyt(),
        "data": check_data(),
    }

    now = datetime.now(timezone.utc)
    for key, label in CHECKS:
        ok, detail = results[key]
        prev = state.get(key, {})
        was_ok = prev.get("ok", True)

        if ok and not was_ok:
            since = prev.get("since", "")
            notify("✅ NJ Departures: %s recovered" % label,
                   "%s\nWas failing since %s." % (detail, since[:16].replace("T", " ") + " UTC"),
                   "low", "white_check_mark,njdepartures")
            log("RECOVERED %s: %s" % (key, detail))
            state[key] = {"ok": True}

        elif not ok and was_ok:
            notify("❌ NJ Departures: %s" % label, detail, "high", "warning,njdepartures")
            log("FAILING %s: %s" % (key, detail))
            state[key] = {"ok": False, "since": now.isoformat(), "told": now.isoformat()}

        elif not ok:
            told = datetime.fromisoformat(prev.get("told", now.isoformat()))
            if now - told > REMIND_EVERY:
                notify("❌ NJ Departures: %s still failing" % label,
                       "%s\nFailing since %s." % (detail, prev.get("since", "")[:16].replace("T", " ") + " UTC"),
                       "high", "warning,njdepartures")
                log("REMINDED %s: %s" % (key, detail))
                prev["told"] = now.isoformat()
                state[key] = prev

        else:
            state[key] = {"ok": True}

    with open(STATE_FILE + ".tmp", "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def main():
    if "--test" in sys.argv:
        notify("✅ NJ Departures: monitor installed",
               "Checks run every 10 minutes. You will hear from this topic only when "
               "something breaks or recovers.", "low", "white_check_mark,njdepartures")
        print("test notification sent")
        return

    try:
        run()
    except Exception as exc:
        # The monitor failing is itself worth knowing about, once.
        flag = os.path.join(HOME, "crashed")
        if not os.path.exists(flag):
            try:
                notify("❌ NJ Departures: monitor crashed", repr(exc)[:500], "high", "warning,njdepartures")
            except Exception:
                pass
            open(flag, "w").close()
        log("CRASHED: %r" % exc)
        raise
    else:
        flag = os.path.join(HOME, "crashed")
        if os.path.exists(flag):
            os.remove(flag)


if __name__ == "__main__":
    main()
