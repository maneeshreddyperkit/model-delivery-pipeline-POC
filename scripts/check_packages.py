"""Exercise the work-package readiness engine from the command line.

    python scripts/check_packages.py [IWP]

Prints the board summary and a sample of packages with their verdicts, or
the full constraint breakdown for one package if an IWP is given.
"""

from __future__ import annotations

import sys

from modelops import awp
from modelops.config import load_config
from modelops.db import Database

config = load_config()
db = Database(config.path("catalog_db"))

if len(sys.argv) > 1:
    detail = awp.package_detail(db, sys.argv[1])
    if detail is None:
        print(f"No package {sys.argv[1]}")
        raise SystemExit(1)
    readiness = detail["readiness"]
    print(f"{readiness.iwp}  {readiness.verdict}")
    for constraint in readiness.constraints:
        mark = "ok  " if constraint.passed else "FAIL"
        print(f"  [{mark}] {constraint.label}")
        print(f"         {constraint.detail}")
    print("\nbill of materials")
    for row in detail["bom"]:
        print(f"  {row['category']:20s} {row['quantity']:>5}  "
              f"{row['weight_kg'] or 0:>10,.0f} kg")
    raise SystemExit(0)

board = awp.package_board(db)
summary = awp.board_summary(board)
for key, value in summary.items():
    print(f"  {key:18s} {value:>8,}")

print("\n-- installed, handed over, data still incomplete --")
for p in [p for p in board if p["complete_with_gaps"]][:5]:
    gaps = [f"{p[k]} {k[8:].replace('_', ' ')}"
            for k in ("missing_commissioning_system", "missing_material",
                      "missing_system") if p[k]]
    print(f"  {p['iwp']:<30} {', '.join(gaps)}")

print("\n-- blocked purely by model data --")
data_blocked = [p for p in board if p["attribute_blocked_only"]]
for pkg in data_blocked[:6]:
    print(f"  {pkg['iwp']:30s} {pkg['summary'][:88]}")
if not data_blocked:
    print("  (none)")

print("\n-- a sample of the board --")
for pkg in board[:10]:
    print(f"  {pkg['iwp']:30s} {pkg['verdict']:9s} "
          f"{pkg['progress_pct']:>5.1f}%  {pkg['summary'][:70]}")
