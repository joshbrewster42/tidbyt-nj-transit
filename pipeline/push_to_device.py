#!/usr/bin/env python3
"""
Render the app with real credentials and push it to a Tidbyt, repeatedly.

Why this exists: `pixlet push` renders on this machine and uploads a still
image. secret.decrypt() only works inside Tidbyt's cloud, so the encrypted
credentials in the committed app cannot be used here -- a push of the shipped
app shows "no api key" for bus. And because the app sets max_age, the device
stops displaying a pushed image once it goes stale, so a single push blanks
after a couple of minutes.

So this builds a copy that uses real credentials, then renders and pushes on a
loop until interrupted.

    pixlet login                       # once, stores an API token
    python3 pipeline/push_to_device.py --list
    python3 pipeline/push_to_device.py --device ABC123 \\
        'stop1={"c":"21923","m":"b","d":["New York"]}' \\
        'stop2={"c":"11","m":"f","d":["Midtown / W. 39th St."]}'

The credentials are prompted for once and held in memory. The build that
contains them is written to a private temporary directory, chmod 600, and
deleted when the script exits -- including on Ctrl-C.

For a display that keeps working without this machine running, publish the app
instead: on Tidbyt's servers the encrypted credentials decrypt and none of this
is needed.
"""

import argparse
import atexit
import getpass
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "nj_transit_nearby.star")

SECRET_CALLS = ("    username = secret.decrypt(NJT_USERNAME_ENC)\n"
                "    password = secret.decrypt(NJT_PASSWORD_ENC)")


def build_live(workdir):
    """Write a credentialled copy of the app into a private directory."""
    username = os.environ.get("NJT_USERNAME")
    password = os.environ.get("NJT_PASSWORD")
    try:
        if not username:
            username = input("NJ Transit username: ").strip()
        if not password:
            password = getpass.getpass("NJ Transit password: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nNeeds a terminal, or set NJT_USERNAME and NJT_PASSWORD.")

    if not username or not password:
        sys.exit("Need both a username and a password.")

    src = open(APP, encoding="utf-8").read()
    if SECRET_CALLS not in src:
        sys.exit("Could not find the secret.decrypt calls to replace.")
    src = src.replace(SECRET_CALLS,
                      "    username = %r\n    password = %r" % (username, password))

    # Its own directory: pixlet mis-resolves paths when several .star files
    # share one.
    path = os.path.join(workdir, "app.star")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def render(app_path, config, out_path):
    proc = subprocess.run(["pixlet", "render", os.path.basename(app_path)]
                          + config + ["-o", out_path],
                          cwd=os.path.dirname(app_path),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "render failed").strip()
    return None


def push(device, out_path, installation_id, background):
    cmd = ["pixlet", "push", device, out_path, "--installation-id", installation_id]
    if background:
        cmd.append("--background")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "push failed").strip()
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="*",
                    help='config values, e.g. \'stop1={"c":"21923","m":"b","d":["New York"]}\'')
    ap.add_argument("--device", help="Tidbyt device ID (see --list)")
    ap.add_argument("--list", action="store_true", help="list your devices and exit")
    ap.add_argument("--installation-id", default="njtransitnearby",
                    help="keeps the app in the device's rotation under this id")
    ap.add_argument("--every", type=int, default=60,
                    help="seconds between pushes (default 60; must stay under "
                         "the app's max_age or the device blanks it)")
    ap.add_argument("--once", action="store_true",
                    help="push a single time and exit")
    ap.add_argument("--background", action="store_true",
                    help="do not interrupt what the device is showing")
    args = ap.parse_args()

    if args.list:
        os.execvp("pixlet", ["pixlet", "devices"])

    if not args.device:
        sys.exit("Need --device. Run with --list to see yours "
                 "(after `pixlet login`).")
    if not args.config:
        sys.exit("Need at least one config value, e.g.\n"
                 '  \'stop1={"c":"21923","m":"b","d":["New York"]}\'')

    workdir = tempfile.mkdtemp(prefix="njtransit-live-")
    os.chmod(workdir, stat.S_IRWXU)
    atexit.register(shutil.rmtree, workdir, True)

    app_path = build_live(workdir)
    out_path = os.path.join(workdir, "out.webp")
    print("Built a credentialled copy in %s (removed on exit)" % workdir)
    print()

    pushes = 0
    while True:
        err = render(app_path, args.config, out_path)
        if err:
            print("  render failed: %s" % err[:300])
        else:
            err = push(args.device, out_path, args.installation_id, args.background)
            if err:
                print("  push failed: %s" % err[:300])
            else:
                pushes += 1
                print("  %s  pushed (%d)" % (time.strftime("%H:%M:%S"), pushes))

        if args.once:
            break
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            break

    print()
    print("Stopped. Credentials removed with %s" % workdir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
