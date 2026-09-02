"""Local operations dashboard and model viewer.

Bound to 127.0.0.1. Serves three things:

  * pipeline operations - throughput, stage latency, failures, alerts
  * the model catalog    - what is live, what is blocked, and why
  * a viewer             - the delivered glTF rendered in the browser,
                           with the tag tree and attributes read back out
                           of the catalog

The viewer matters because it closes the loop. Without it the pipeline's
output is a number in a table; with it you can see that the published
artifact opens, that components are selectable, and that the tags in the
database line up with the geometry that shipped.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file)

from ..config import Config
from ..db import Database, UnsafeQuery, assert_read_only, open_read_only
from ..monitoring import (AlertEngine, failure_taxonomy, overview,
                          project_health, qa_summary, stage_latency)

PRESET_QUERIES = [
    {
        "name": "Delivery health by project",
        "sql": "SELECT project_code, jobs_total, jobs_succeeded, jobs_failed,\n"
               "       jobs_quarantined, success_rate_pct, avg_duration_s\n"
               "FROM v_job_health\nORDER BY success_rate_pct ASC;",
    },
    {
        "name": "Where is processing time actually going?",
        "sql": "SELECT stage,\n"
               "       COUNT(*)                AS runs,\n"
               "       ROUND(AVG(duration_ms)) AS avg_ms,\n"
               "       MAX(duration_ms)        AS max_ms,\n"
               "       ROUND(SUM(duration_ms) / 1000.0, 1) AS total_s\n"
               "FROM job_steps\nWHERE duration_ms IS NOT NULL\n"
               "GROUP BY stage\nORDER BY total_s DESC;",
    },
    {
        "name": "Models blocked from delivery, with the reason",
        "sql": "SELECT m.project_code, m.model_key, r.revision_label, r.status,\n"
               "       j.error_class, j.error_kind, j.error_message\n"
               "FROM model_revisions r\n"
               "JOIN models m ON m.model_id = r.model_id\n"
               "JOIN jobs   j ON j.revision_id = r.revision_id\n"
               "WHERE r.status IN ('FAILED', 'QUARANTINED')\n"
               "ORDER BY m.project_code, m.model_key;",
    },
    {
        # Scoped to validation.required_attributes: the fields every component
        # is supposed to carry regardless of discipline. Attributes that only
        # apply to a subset (ValveType, BendRadius) would read as a gap here
        # when they are simply not applicable, so they are excluded.
        "name": "Required-attribute completeness, by discipline",
        "sql": "SELECT m.discipline,\n"
               "       a.name                         AS attribute,\n"
               "       COUNT(DISTINCT a.component_id) AS populated,\n"
               "       d.total                        AS components,\n"
               "       ROUND(100.0 * COUNT(DISTINCT a.component_id)\n"
               "             / d.total, 1)            AS coverage_pct\n"
               "FROM component_attributes a\n"
               "JOIN components c      ON c.component_id = a.component_id\n"
               "                      AND c.has_geometry = 1\n"
               "JOIN model_revisions r ON r.revision_id  = c.revision_id\n"
               "JOIN models m          ON m.model_id     = r.model_id\n"
               "JOIN (SELECT m2.discipline, COUNT(*) AS total\n"
               "      FROM components c2\n"
               "      JOIN model_revisions r2 ON r2.revision_id = c2.revision_id\n"
               "      JOIN models m2          ON m2.model_id    = r2.model_id\n"
               "      WHERE c2.has_geometry = 1\n"
               "      GROUP BY m2.discipline) d ON d.discipline = m.discipline\n"
               "WHERE a.value <> ''\n"
               "  AND a.name IN ('System', 'Area', 'Material', 'CommissioningSystem')\n"
               "GROUP BY m.discipline, a.name\n"
               "ORDER BY coverage_pct ASC, m.discipline;",
    },
    {
        "name": "Heaviest components by triangle count",
        "sql": "SELECT m.model_key, c.tag, c.category, c.triangle_count\n"
               "FROM components c\n"
               "JOIN model_revisions r ON r.revision_id = c.revision_id\n"
               "JOIN models m ON m.model_id = r.model_id\n"
               "WHERE r.status = 'PUBLISHED'\n"
               "ORDER BY c.triangle_count DESC\nLIMIT 25;",
    },
    {
        "name": "Commissioning system rollup (downstream join surface)",
        "sql": "SELECT a.value AS commissioning_system,\n"
               "       COUNT(*)                  AS components,\n"
               "       SUM(c.triangle_count)     AS triangles,\n"
               "       COUNT(DISTINCT m.model_key) AS models\n"
               "FROM component_attributes a\n"
               "JOIN components c ON c.component_id = a.component_id\n"
               "JOIN model_revisions r ON r.revision_id = c.revision_id\n"
               "JOIN models m ON m.model_id = r.model_id\n"
               "WHERE a.name = 'CommissioningSystem' AND r.status = 'PUBLISHED'\n"
               "GROUP BY a.value\nORDER BY components DESC;",
    },
    {
        "name": "Retry and failure history per model",
        "sql": "SELECT m.model_key, j.job_id, j.status, j.attempt, j.error_class,\n"
               "       ROUND(j.duration_ms / 1000.0, 2) AS seconds\n"
               "FROM jobs j\n"
               "JOIN model_revisions r ON r.revision_id = j.revision_id\n"
               "JOIN models m ON m.model_id = r.model_id\n"
               "ORDER BY j.job_id;",
    },
]


def create_app(config: Config, db: Database) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_SORT_KEYS"] = False

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def rows(sql: str, params=()) -> list[dict]:
        return [dict(r) for r in db.query(sql, params)]

    def live_publication(project_code: str, model_key: str) -> dict | None:
        row = db.query_one(
            "SELECT p.*, r.revision_label, r.revision_id, m.model_name, m.discipline "
            "FROM publications p "
            "JOIN model_revisions r ON r.revision_id = p.revision_id "
            "JOIN models m ON m.model_id = r.model_id "
            "WHERE m.project_code = ? AND m.model_key = ? AND p.is_current = 1",
            (project_code, model_key))
        return dict(row) if row else None

    # -----------------------------------------------------------------
    # Pages
    # -----------------------------------------------------------------
    @app.route("/")
    def page_overview():
        return render_template(
            "overview.html",
            stats=overview(db),
            stages=stage_latency(db),
            health=project_health(db),
            taxonomy=failure_taxonomy(db),
            qa=qa_summary(db),
            recent=rows(
                "SELECT j.job_id, j.status, j.attempt, j.duration_ms, j.error_class, "
                "j.queued_at, m.project_code, m.model_key, r.revision_label "
                "FROM jobs j "
                "JOIN model_revisions r ON r.revision_id = j.revision_id "
                "JOIN models m ON m.model_id = r.model_id "
                "ORDER BY j.job_id DESC LIMIT 15"),
            usage=rows(
                "SELECT stat_date, SUM(unique_users) AS users, "
                "SUM(viewer_sessions) AS sessions "
                "FROM delivery_stats GROUP BY stat_date ORDER BY stat_date"),
            page="overview")

    @app.route("/jobs")
    def page_jobs():
        status = request.args.get("status", "")
        clause, params = "", []
        if status:
            clause = "WHERE j.status = ?"
            params = [status]
        return render_template(
            "jobs.html",
            jobs=rows(
                "SELECT j.job_id, j.status, j.attempt, j.max_attempts, j.trigger, "
                "j.duration_ms, j.error_class, j.error_kind, j.error_message, "
                "j.queued_at, j.finished_at, j.sla_breached, j.worker, "
                "m.project_code, m.model_key, r.revision_label "
                "FROM jobs j "
                "JOIN model_revisions r ON r.revision_id = j.revision_id "
                "JOIN models m ON m.model_id = r.model_id "
                f"{clause} ORDER BY j.job_id DESC", params),
            status=status, page="jobs")

    @app.route("/jobs/<int:job_id>")
    def page_job(job_id: int):
        job = db.query_one(
            "SELECT j.*, m.project_code, m.model_key, m.model_name, "
            "r.revision_label, r.source_filename, r.source_bytes, r.source_sha256 "
            "FROM jobs j "
            "JOIN model_revisions r ON r.revision_id = j.revision_id "
            "JOIN models m ON m.model_id = r.model_id WHERE j.job_id = ?", (job_id,))
        if job is None:
            abort(404)

        steps = rows(
            "SELECT * FROM job_steps WHERE job_id = ? ORDER BY step_id", (job_id,))
        for step in steps:
            try:
                step["metrics"] = json.loads(step["metrics_json"] or "{}")
            except json.JSONDecodeError:
                step["metrics"] = {}

        total = sum(s["duration_ms"] or 0 for s in steps) or 1
        for step in steps:
            step["share_pct"] = round(100.0 * (step["duration_ms"] or 0) / total, 1)

        return render_template(
            "job.html", job=dict(job), steps=steps,
            findings=rows("SELECT * FROM qa_findings WHERE job_id = ? "
                          "ORDER BY CASE severity WHEN 'ERROR' THEN 0 "
                          "WHEN 'WARN' THEN 1 ELSE 2 END", (job_id,)),
            logs=rows("SELECT * FROM job_log WHERE job_id = ? ORDER BY log_id",
                      (job_id,)),
            page="jobs")

    @app.route("/catalog")
    def page_catalog():
        return render_template(
            "catalog.html",
            models=rows(
                "SELECT c.*, p.project_name, p.subscriber_count "
                "FROM v_model_currency c "
                "JOIN projects p ON p.project_code = c.project_code "
                "ORDER BY c.project_code, c.model_key"),
            blocked=rows(
                "SELECT m.project_code, m.model_key, r.revision_label, r.status, "
                "r.received_at, j.job_id, j.error_class, j.error_message "
                "FROM model_revisions r "
                "JOIN models m ON m.model_id = r.model_id "
                "JOIN jobs j ON j.job_id = ("
                "  SELECT MAX(job_id) FROM jobs WHERE revision_id = r.revision_id) "
                "WHERE r.status IN ('FAILED','QUARANTINED') "
                "ORDER BY r.received_at DESC"),
            page="catalog")

    @app.route("/alerts")
    def page_alerts():
        return render_template(
            "alerts.html",
            alerts=rows("SELECT * FROM alerts ORDER BY alert_id DESC LIMIT 100"),
            page="alerts")

    @app.route("/alerts/evaluate", methods=["POST"])
    def evaluate_alerts():
        return jsonify(AlertEngine(config, db).run())

    @app.route("/query")
    def page_query():
        return render_template("query.html", presets=PRESET_QUERIES, page="query")

    @app.route("/viewer")
    @app.route("/viewer/<project_code>/<model_key>")
    def page_viewer(project_code: str | None = None, model_key: str | None = None):
        available = rows(
            "SELECT c.project_code, c.model_key, c.model_name, c.discipline, "
            "c.live_revision, c.component_count, c.triangle_count "
            "FROM v_model_currency c WHERE c.live_revision IS NOT NULL "
            "ORDER BY c.triangle_count DESC")
        if not available:
            return render_template("viewer.html", available=[], selected=None,
                                   page="viewer")
        if project_code is None or model_key is None:
            project_code = available[0]["project_code"]
            model_key = available[0]["model_key"]
        return render_template(
            "viewer.html", available=available,
            selected={"project_code": project_code, "model_key": model_key},
            page="viewer")

    @app.route("/pipeline")
    def page_pipeline():
        from ..adapters import registered
        from ..stages import STAGE_NAMES
        return render_template(
            "pipeline.html",
            stage_names=STAGE_NAMES,
            stages=stage_latency(db),
            adapters=sorted(registered().items()),
            config=config.as_dict(),
            page="pipeline")

    # -----------------------------------------------------------------
    # API
    # -----------------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        """Liveness probe: the catalog is reachable and has been populated."""
        jobs = db.scalar("SELECT COUNT(*) FROM jobs", default=0)
        return jsonify(status="ok" if jobs else "empty", jobs=jobs), 200 if jobs else 503

    @app.route("/api/overview")
    def api_overview():
        return jsonify(overview(db))

    @app.route("/api/query", methods=["POST"])
    def api_query():
        """Ad-hoc read-only SQL against the catalog.

        Guarded twice: the statement must parse as a single SELECT/WITH,
        and the connection itself is opened read-only at the OS level.
        """
        payload = request.get_json(silent=True) or {}
        try:
            sql = assert_read_only(payload.get("sql", ""))
        except UnsafeQuery as exc:
            return jsonify({"error": str(exc)}), 400

        conn = open_read_only(config.path("catalog_db"))
        try:
            cursor = conn.execute(sql)
            fetched = cursor.fetchmany(500)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return jsonify({
                "columns": columns,
                "rows": [list(r) for r in fetched],
                "truncated": len(fetched) == 500,
            })
        except sqlite3.Error as exc:
            return jsonify({"error": f"SQL error: {exc}"}), 400
        finally:
            conn.close()

    @app.route("/api/model/<project_code>/<model_key>")
    def api_model(project_code: str, model_key: str):
        pub = live_publication(project_code, model_key)
        if pub is None:
            return jsonify({"error": "No live publication for that model."}), 404

        manifest_path = Path(pub["delivery_path"]) / "manifest.json"
        manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest_path.exists() else {})
        return jsonify({
            "project_code": project_code,
            "model_key": model_key,
            "model_name": pub["model_name"],
            "discipline": pub["discipline"],
            "revision_label": pub["revision_label"],
            "published_at": pub["published_at"],
            "component_count": pub["component_count"],
            "triangle_count": pub["triangle_count"],
            "published_bytes": pub["published_bytes"],
            "wire_bytes": pub["wire_bytes"],
            "naive_bytes": pub["naive_bytes"],
            "lods": manifest.get("lods", []),
            "qa": manifest.get("qa", {}),
        })

    @app.route("/api/model/<project_code>/<model_key>/tree")
    def api_model_tree(project_code: str, model_key: str):
        pub = live_publication(project_code, model_key)
        if pub is None:
            return jsonify({"error": "No live publication."}), 404
        return jsonify({
            "components": rows(
                "SELECT component_id, tag, parent_tag, category, discipline, "
                "has_geometry, triangle_count FROM components "
                "WHERE revision_id = ? ORDER BY component_id",
                (pub["revision_id"],))
        })

    @app.route("/api/model/<project_code>/<model_key>/component/<path:tag>")
    def api_component(project_code: str, model_key: str, tag: str):
        """Attributes for one tag, read from the catalog rather than the file.

        This is the point of extracting metadata into SQL: the viewer asks
        for a tag and gets engineering data back, and the same rows are
        what any downstream report or integration would join against.
        """
        pub = live_publication(project_code, model_key)
        if pub is None:
            return jsonify({"error": "No live publication."}), 404

        component = db.query_one(
            "SELECT * FROM components WHERE revision_id = ? AND tag = ? "
            "ORDER BY component_id LIMIT 1", (pub["revision_id"], tag))
        if component is None:
            return jsonify({"error": f"No component tagged {tag}."}), 404

        duplicates = db.scalar(
            "SELECT COUNT(*) FROM components WHERE revision_id = ? AND tag = ?",
            (pub["revision_id"], tag), default=1)

        return jsonify({
            "component": dict(component),
            "duplicate_tag_count": duplicates,
            "attributes": rows(
                "SELECT name, value FROM component_attributes "
                "WHERE component_id = ? ORDER BY name",
                (component["component_id"],)),
        })

    @app.route("/api/model/<project_code>/<model_key>/geometry/<lod>.glb")
    def api_geometry(project_code: str, model_key: str, lod: str):
        pub = live_publication(project_code, model_key)
        if pub is None:
            abort(404)

        # Path traversal guard: the LOD name comes from the URL, so the
        # resolved file must still sit inside this model's delivery folder.
        root = Path(pub["delivery_path"]).resolve()
        target = (root / f"{lod}.glb").resolve()
        if root not in target.parents or not target.exists():
            abort(404)

        response = send_file(target, mimetype="model/gltf-binary",
                             conditional=True)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/api/jobs/<int:job_id>/steps")
    def api_job_steps(job_id: int):
        return jsonify(rows("SELECT stage, status, duration_ms, metrics_json "
                            "FROM job_steps WHERE job_id = ? ORDER BY step_id",
                            (job_id,)))

    @app.template_filter("bytes")
    def filter_bytes(n) -> str:
        n = float(n or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if abs(n) < 1024.0:
                return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
            n /= 1024.0
        return f"{n:,.1f} TB"

    @app.template_filter("ms")
    def filter_ms(n) -> str:
        n = float(n or 0)
        return f"{n:,.0f} ms" if n < 1000 else f"{n / 1000.0:,.2f} s"

    @app.template_filter("num")
    def filter_num(n) -> str:
        return f"{(n or 0):,}"

    return app
