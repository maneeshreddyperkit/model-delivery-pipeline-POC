"""Ad-hoc inspection helper: what did the pipeline do to one model?

    python scripts/inspect_model.py TB2-HVAC-U20

Prints the job outcome, quality findings, optimise-stage metrics, the
component category breakdown and the delivered files. Used while
developing the synthetic generators to confirm that new geometry survives
the whole pipeline rather than only looking right in the builder.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

model_key = sys.argv[1] if len(sys.argv) > 1 else "TB2-HVAC-U20"

conn = sqlite3.connect("data/catalog.db")
conn.row_factory = sqlite3.Row

job = conn.execute(
    "SELECT j.job_id, j.status, j.duration_ms FROM jobs j "
    "JOIN model_revisions r ON r.revision_id = j.revision_id "
    "JOIN models m ON m.model_id = r.model_id "
    "WHERE m.model_key = ? AND j.is_simulated = 0 ORDER BY j.job_id DESC LIMIT 1",
    (model_key,)).fetchone()
if job is None:
    print(f"No measured job for {model_key}.")
    raise SystemExit(1)

print(f"job #{job['job_id']}  {job['status']}  {job['duration_ms']} ms")

print("\n-- quality findings --")
findings = conn.execute(
    "SELECT severity, rule_code, message FROM qa_findings WHERE job_id = ?",
    (job["job_id"],)).fetchall()
for f in findings:
    print(f"  {f['severity']:5s} {f['rule_code']:22s} {f['message'][:100]}")
if not findings:
    print("  (clean)")

print("\n-- optimise metrics --")
step = conn.execute(
    "SELECT metrics_json FROM job_steps WHERE job_id = ? AND stage = 'optimize'",
    (job["job_id"],)).fetchone()
if step and step["metrics_json"]:
    print(json.dumps(json.loads(step["metrics_json"]), indent=2))

print("\n-- component categories --")
for row in conn.execute(
        "SELECT c.category, COUNT(*) AS n, SUM(c.triangle_count) AS tris "
        "FROM components c "
        "JOIN model_revisions r ON r.revision_id = c.revision_id "
        "JOIN models m ON m.model_id = r.model_id "
        "WHERE m.model_key = ? GROUP BY c.category ORDER BY n DESC", (model_key,)):
    print(f"  {row['category']:22s} {row['n']:>5,}  {row['tris'] or 0:>9,} tris")

print("\n-- delivered files --")
pub = conn.execute(
    "SELECT delivery_path FROM publications p "
    "JOIN model_revisions r ON r.revision_id = p.revision_id "
    "JOIN models m ON m.model_id = r.model_id "
    "WHERE m.model_key = ? AND p.is_current = 1", (model_key,)).fetchone()
if pub:
    for path in sorted(pathlib.Path(pub["delivery_path"]).iterdir()):
        print(f"  {path.name:20s} {path.stat().st_size:>10,}")
