#!/usr/bin/env python3
"""Certify the current GENETIC sources for the native kernel.

Adds the fingerprint of the installed EOS GENETIC sources to
``akkudoktoreos-native/python/akkudoktoreos_native/certified.json`` - but only when a compare
run (``genetic_compare_plugin``) found no differences.

    python native/scripts/certify.py COMPARE.json [--semantics main|all] [--note TEXT]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

CERTIFIED = (
    Path(__file__).resolve().parents[1]
    / "akkudoktoreos-native"
    / "python"
    / "akkudoktoreos_native"
    / "certified.json"
)
SEMANTICS = ("neutral_dc_permission", "executed_ac_controls", "ev_charge_seeding")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("compare_report", type=Path)
    parser.add_argument("--semantics", choices=("main", "all"), default="main")
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    from akkudoktoreos.optimization.genetic.native import compute_fingerprint

    report = json.loads(args.compare_report.read_text(encoding="utf-8"))
    summary = report["summary"]
    if summary["different"]:
        print(f"refusing: {len(summary['different'])} runs differ", file=sys.stderr)
        return 1
    if summary["identical"] == 0:
        print("refusing: the compare run has no identical run", file=sys.stderr)
        return 1

    fingerprint = compute_fingerprint()
    data = json.loads(CERTIFIED.read_text(encoding="utf-8"))
    entries = [e for e in data["entries"] if e["fingerprint"] != fingerprint]
    entries.append(
        {
            "fingerprint": fingerprint,
            "semantics": {name: args.semantics == "all" for name in SEMANTICS},
            "verified": dt.date.today().isoformat(),
            "evidence": (
                f"{summary['identical']} identical runs, {summary['unsupported']} not supported "
                f"natively, {summary['patched']} with patched Python, 0 different"
            ),
            "note": args.note,
        }
    )
    data["entries"] = entries
    CERTIFIED.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"certified {fingerprint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
