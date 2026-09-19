#!/usr/bin/env python3
"""
Run assertions against the app's Starlark helpers.

    python3 pipeline/run_tests.py

There is no Starlark test runner in pixlet, and the app must stay a single
file for the community repo, so this appends a test main() to a copy of the
app and runs it through `pixlet render`. The copy is thrown away, and the real
app never contains test code.

The destination matcher is the piece worth pinning down: it compares wording
from the live API against wording derived from GTFS, and the two disagree in
ways that are easy to get subtly wrong in both directions -- too strict and a
rider's filter silently shows nothing, too loose and "Newark" matches
"New York".
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "nj_transit_nearby.star"

# (live feed wording, the destination the rider picked, should they match?)
MATCH_CASES = [
    ("Jersey City Journal Sq", "Journal Square", True),
    ("Montgomery St", "Montgomery St West Side Ave Society Hill", True),
    ("Newark Penn Sta", "Newark Penn Station", True),
    ("Hoboken Term", "Hoboken Terminal", True),
    ("New York PABT", "New York", True),
    ("Tonnelle Ave", "Tonnelle Ave", True),
    ("Irvington Ctr", "Irvington Center", True),
    ("Union City", "Jersey City", False),
    ("West Side Ave", "Tonnelle Ave", False),
    ("Newark", "New York", False),
    ("Bayonne 8th St", "Journal Square", False),
    ("Paterson", "Passaic", False),
]

# (raw text, expected shortened form) for the countdown column.
WAIT_CASES = [
    ("12 min", "12m"),
    ("3 min", "3m"),
    ("now", "now"),
    ("DUE", "now"),
]

TEST_MAIN = '''

CASES = %s
WAITS = %s

def main(config):
    bad = 0
    for c in CASES:
        got = matches_destination(c[0], c[1])
        if got != c[2]:
            bad += 1
            print("FAIL match: " + c[0] + " vs " + c[1] + " -> " + str(got))
    for w in WAITS:
        got = _short_wait(w[0])
        if got != w[1]:
            bad += 1
            print("FAIL wait: " + w[0] + " -> " + got + " (want " + w[1] + ")")

    # A departure with no destination must never match a filter, or an empty
    # field would quietly pass every test the rider applies.
    if matches_destination("", "Newark"):
        bad += 1
        print("FAIL: empty destination matched a filter")

    # The stop unwrapper has to survive junk without crashing the render.
    for raw in ['{"display":"x","value":"{\\\\"c\\\\":\\\\"1\\\\",\\\\"m\\\\":\\\\"b\\\\"}"}', "{}", "[]"]:
        got = unwrap_stop(raw)
        if type(got) != "dict" or "c" not in got:
            bad += 1
            print("FAIL unwrap: " + raw)

    print("TESTS_FAILED=" + str(bad))
    return render.Root(child = render.Text("t", font = FONT))
'''


def main():
    src = APP.read_text()
    # The real main() would otherwise shadow the test one; Starlark forbids
    # rebinding a top-level name.
    src = src.replace("def main(config):", "def _app_main(config):", 1)
    src += TEST_MAIN % (repr(MATCH_CASES).replace("(", "[").replace(")", "]"),
                        repr(WAIT_CASES).replace("(", "[").replace(")", "]"))

    with tempfile.TemporaryDirectory() as tmp:
        # pixlet gets confused when several .star files share a directory.
        path = os.path.join(tmp, "apptest.star")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        proc = subprocess.run(
            ["pixlet", "render", "apptest.star", "-o", os.path.join(tmp, "o.webp")],
            cwd=tmp, capture_output=True, text=True)

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and "TESTS_FAILED" not in output:
        print(output.strip())
        sys.exit("Test run failed to execute.")

    for line in output.splitlines():
        if "FAIL" in line and "TESTS_FAILED" not in line:
            print(line.replace("[apptest.star] ", "").strip())

    found = re.search(r"TESTS_FAILED=(\d+)", output)
    if not found:
        print(output.strip())
        sys.exit("Could not read test results.")

    failures = int(found.group(1))
    total = len(MATCH_CASES) + len(WAIT_CASES) + 4
    if failures:
        sys.exit("\n%d of %d assertions failed." % (failures, total))
    print("All %d assertions passed." % total)


if __name__ == "__main__":
    main()
