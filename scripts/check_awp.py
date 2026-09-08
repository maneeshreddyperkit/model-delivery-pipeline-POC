"""Sanity check on the work-packaging attributes.

    python scripts/check_awp.py

Confirms that packages are project-scoped and sized like real installation
work packages rather than merged across models.
"""

from __future__ import annotations

import sqlite3

conn = sqlite3.connect("data/catalog.db")


def count_distinct(name: str) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT value) FROM component_attributes WHERE name = ?",
        (name,)).fetchone()[0]


for name in ("CWA", "CWP", "EWP", "IWP"):
    print(f"  {name}: {count_distinct(name):>5} distinct")

print("\n-- largest packages (cap is 90 components) --")
for value, n in conn.execute(
        "SELECT value, COUNT(*) FROM component_attributes WHERE name = 'IWP' "
        "GROUP BY value ORDER BY COUNT(*) DESC LIMIT 6"):
    print(f"  {value:34s} {n:>4}")

print("\n-- does any package span more than one model? --")
spanning = conn.execute(
    "SELECT a.value, COUNT(DISTINCT m.model_key) AS models "
    "FROM component_attributes a "
    "JOIN model_revisions r ON r.revision_id = a.revision_id "
    "JOIN models m ON m.model_id = r.model_id "
    "WHERE a.name = 'IWP' GROUP BY a.value HAVING models > 1").fetchall()
print(f"  {len(spanning)} package(s) span multiple models"
      f"{' (expected: 0)' if not spanning else ' <-- PROBLEM'}")

print("\n-- one package in detail --")
sample = conn.execute(
    "SELECT value FROM component_attributes WHERE name = 'IWP' "
    "GROUP BY value ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
print(f"  {sample}")
for row in conn.execute(
        "SELECT s.value AS state, COUNT(*) AS n FROM component_attributes a "
        "JOIN component_attributes s ON s.component_id = a.component_id "
        "                           AND s.name = 'LifecycleStatus' "
        "WHERE a.name = 'IWP' AND a.value = ? GROUP BY s.value ORDER BY n DESC",
        (sample,)):
    print(f"    {row[0]:16s} {row[1]:>4}")
