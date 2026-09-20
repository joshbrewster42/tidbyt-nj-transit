#!/usr/bin/env python3
"""
Verify NJ Transit bus credentials and show the real response shape.

The app's realtime bus parsing was written from published client libraries,
not from a response anyone here has seen. This calls the API the same way the
app does and prints what actually comes back, so the field names can be
confirmed rather than assumed.

    python3 pipeline/check_credentials.py 21923

Credentials are read from the environment, or prompted for without echo:

    NJT_USERNAME, NJT_PASSWORD

They are never written to disk and never printed. The session token is
redacted in all output, so the result of this script is safe to paste into a
conversation or an issue.

What to look for: the app expects a "DVTrip" list whose entries carry
"public_route", "header", "departuretime" and "sched_dep_time". This prints
whether each of those is present, and dumps a sample entry so anything named
differently is obvious.
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BUS_API = "https://pcsdata.njtransit.com/api/BUSDV2"

# What nj_transit_nearby.star reads out of a departure.
EXPECTED_TRIP_FIELDS = ["public_route", "header", "departuretime", "sched_dep_time"]
EXPECTED_ENVELOPE = ["DVTrip"]


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def post_multipart(url, fields):
    """The app sends getBusDV as multipart, so send it the same way."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append("--%s" % boundary)
        parts.append('Content-Disposition: form-data; name="%s"' % name)
        parts.append("")
        parts.append(str(value))
    parts.append("--%s--" % boundary)
    parts.append("")
    body = "\r\n".join(parts).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stop", nargs="?", default="21923",
                    help="rider-facing 5-digit stop code (default: 21923, "
                         "Port Imperial Blvd at Riverwalk Place toward New York)")
    args = ap.parse_args()

    try:
        username = os.environ.get("NJT_USERNAME") or input("NJ Transit username: ").strip()
        password = os.environ.get("NJT_PASSWORD") or getpass.getpass("NJ Transit password: ")
    except (EOFError, KeyboardInterrupt):
        # No terminal to prompt on, which is the usual case in a script.
        sys.exit("\nSet NJT_USERNAME and NJT_PASSWORD, or run this from a terminal.")

    if not username or not password:
        sys.exit("Need both a username and a password.")

    secrets = [username, password]

    print()
    print("1. authenticateUser")
    try:
        status, raw = post_form("%s/authenticateUser" % BUS_API,
                                {"username": username, "password": password})
    except urllib.error.HTTPError as exc:
        sys.exit("   HTTP %s -- %s" % (exc.code, redact(exc.read().decode("utf-8", "replace"), secrets)[:400]))
    except Exception as exc:
        sys.exit("   request failed: %s" % redact(str(exc), secrets))

    print("   HTTP %s" % status)
    try:
        auth = json.loads(raw)
    except ValueError:
        sys.exit("   not JSON:\n   %s" % redact(raw, secrets)[:500])

    token = auth.get("UserToken") or ""
    secrets.append(token)
    print("   keys: %s" % sorted(auth.keys()))
    print("   Authenticated=%r  UserToken=%s"
          % (auth.get("Authenticated"), "<redacted, %d chars>" % len(token) if token else "MISSING"))

    if not token:
        sys.exit("   No token came back, so the credentials were rejected.")

    print()
    print("2. getBusDV for stop %s" % args.stop)
    try:
        status, raw = post_multipart("%s/getBusDV" % BUS_API, {
            "token": token, "stop": args.stop, "route": "", "direction": "", "ip": "",
        })
    except urllib.error.HTTPError as exc:
        sys.exit("   HTTP %s -- %s" % (exc.code, redact(exc.read().decode("utf-8", "replace"), secrets)[:400]))
    except Exception as exc:
        sys.exit("   request failed: %s" % redact(str(exc), secrets))

    print("   HTTP %s" % status)
    try:
        body = json.loads(raw)
    except ValueError:
        print("   not JSON. First 800 characters:")
        print(redact(raw, secrets)[:800])
        return

    print("   top-level keys: %s" % (sorted(body.keys()) if isinstance(body, dict) else type(body).__name__))

    print()
    print("3. Does it match what the app expects?")
    for key in EXPECTED_ENVELOPE:
        ok = isinstance(body, dict) and key in body
        print("   %-16s %s" % (key, "found" if ok else "MISSING"))

    trips = body.get("DVTrip") if isinstance(body, dict) else None
    if not trips:
        print()
        print("   No departures in the response. That is normal outside service")
        print("   hours or at a quiet stop -- try a busier stop code.")
        print()
        print("   Full response:")
        print(redact(json.dumps(body, indent=2), secrets)[:1500])
        return

    first = trips[0]
    print("   %-16s %d entries" % ("departures", len(trips)))
    for field in EXPECTED_TRIP_FIELDS:
        present = isinstance(first, dict) and field in first
        print("   %-16s %s" % (field, "found" if present else "MISSING"))

    extra = [k for k in (first.keys() if isinstance(first, dict) else [])
             if k not in EXPECTED_TRIP_FIELDS]
    if extra:
        print("   fields we ignore: %s" % sorted(extra))

    print()
    print("4. One full departure, verbatim (safe to share):")
    print(redact(json.dumps(first, indent=2), secrets))

    missing = [f for f in EXPECTED_TRIP_FIELDS
               if not (isinstance(first, dict) and f in first)]
    print()
    if missing:
        print("   %d expected field(s) missing: %s" % (len(missing), missing))
        print("   The parsing in bus_departures() needs adjusting to match.")
    else:
        print("   Every field the app reads is present. Parsing should work as written.")


if __name__ == "__main__":
    main()
