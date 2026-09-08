"""Print the figures the demo script quotes, so the script can be checked
against a real run rather than trusted.

    python scripts/demo_facts.py
"""
from modelops import awp, handover, monitoring
from modelops.config import load_config
from modelops.db import Database


def main() -> None:
    config = load_config()
    db = Database(config.path("catalog_db"))

    stats = monitoring.overview(db)
    print("== operations ==")
    for key in ("jobs_total", "measured_jobs", "simulated_jobs", "history_days",
                "success_rate_pct", "models_live", "measured_models",
                "subscribers", "components_live", "triangles_live",
                "payload_reduction_pct", "wire_reduction_pct",
                "measured_avg_duration_s", "measured_p95_duration_s",
                "open_alerts", "critical_alerts"):
        print(f"  {key:24} {stats.get(key)}")

    print("\n== districts ==")
    for d in monitoring.district_health(db):
        print(f"  {d['district']:26} {d['models']:>4} models  "
              f"{d['subscribers']:>5} users  {d['success_rate_pct']}%")

    board = awp.package_board(db)
    summary = awp.board_summary(board)
    print("\n== work packages ==")
    for key, value in summary.items():
        print(f"  {key:24} {value}")

    print("\n  blocked on data alone:")
    for p in board:
        if p["verdict"] == awp.BLOCKED and p["blockers"] == ["ATTRIBUTES_COMPLETE"]:
            print(f"    {p['iwp']:26} {p['discipline']:12} "
                  f"{p['components']:>4} items  {p['summary']}")

    print("\n  complete but with gaps:")
    gapped = [p for p in board
              if p["verdict"] == awp.COMPLETE and "ATTRIBUTES_COMPLETE" in p["blockers"]]
    for p in gapped[:5]:
        print(f"    {p['iwp']:26} {p['discipline']:12} {p['components']:>4} items")
    print(f"    ({len(gapped)} in total)")

    comp = handover.completeness(db)
    print("\n== handover completeness ==")
    print(f"  components in scope      {comp['components']}")
    for a in comp["overall"]:
        print(f"  {a['label']:24} {a['pct']}%  ({a['missing']} missing)")
    print("  worst discipline cells:")
    for d in comp["disciplines"]:
        for label, cell in zip(comp["attributes"], d["cells"]):
            if cell["missing"]:
                print(f"    {d['discipline']:12} {label:16} {cell['pct']}%  "
                      f"({cell['missing']} short)")

    print("\n== feeds ==")
    for f in handover.catalog(db):
        print(f"  {f['key']:22} {f['records']:>6} records  {f['fmt']:5} {f['consumer']}")

    print("\n== jobs table columns ==")
    cols = [r["name"] for r in db.query("PRAGMA table_info(jobs)")]
    print("  " + ", ".join(cols))

    print("\n== measured jobs, every one ==")
    for r in db.query(
            "SELECT j.job_id, m.model_key, r.revision_label, j.status, j.attempt, "
            "       j.error_class, j.error_message, j.duration_ms "
            "FROM jobs j "
            "JOIN model_revisions r ON r.revision_id = j.revision_id "
            "JOIN models m ON m.model_id = r.model_id "
            "WHERE j.is_simulated = 0 ORDER BY j.job_id"):
        print(f"  #{r['job_id']:<3} {r['model_key']:16} {r['revision_label']:5} "
              f"{r['status']:12} try {r['attempt']}  "
              f"{(r['duration_ms'] or 0) / 1000:.1f}s  {r['error_class'] or ''}")
        if r["error_message"]:
            print(f"       {r['error_message']}")


if __name__ == "__main__":
    main()
