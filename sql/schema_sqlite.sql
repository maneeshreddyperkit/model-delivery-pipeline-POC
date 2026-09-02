-- =====================================================================
-- Model Delivery Pipeline - metadata catalog (SQLite, local file)
--
-- This is the operational system of record for model delivery:
--   what arrived, what was done to it, whether it passed QA,
--   what is live in the viewer, and what broke.
--
-- The whole catalog is one file on disk: data/catalog.db
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Reference / configuration
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    project_code     TEXT PRIMARY KEY,
    project_name     TEXT NOT NULL,
    district         TEXT,
    delivery_root    TEXT NOT NULL,
    sla_minutes      INTEGER NOT NULL DEFAULT 20,
    subscriber_count INTEGER NOT NULL DEFAULT 0,   -- viewer users served by this project
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- Model identity and revisions
-- A "model" is a logical deliverable (e.g. Unit 10 Piping).
-- A "model_revision" is one specific source export of it.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS models (
    model_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_code TEXT NOT NULL REFERENCES projects(project_code),
    model_key    TEXT NOT NULL,
    model_name   TEXT NOT NULL,
    discipline   TEXT,
    source_tool  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_code, model_key)
);

CREATE TABLE IF NOT EXISTS model_revisions (
    revision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id         INTEGER NOT NULL REFERENCES models(model_id),
    revision_label   TEXT NOT NULL,
    source_filename  TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    source_sha256    TEXT NOT NULL,
    source_bytes     INTEGER NOT NULL,
    source_tool      TEXT,
    unit_system      TEXT,
    coordinate_system TEXT,
    exported_at      TEXT,
    received_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status           TEXT NOT NULL DEFAULT 'RECEIVED',
        -- RECEIVED | PROCESSING | PUBLISHED | FAILED | QUARANTINED | SUPERSEDED
    -- content hash makes re-drops of an identical file idempotent
    UNIQUE (model_id, source_sha256)
);

CREATE INDEX IF NOT EXISTS ix_revisions_model  ON model_revisions(model_id, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_revisions_status ON model_revisions(status);

-- ---------------------------------------------------------------------
-- Pipeline execution
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id      INTEGER NOT NULL REFERENCES model_revisions(revision_id),
    pipeline_version TEXT NOT NULL,
    trigger          TEXT NOT NULL DEFAULT 'AUTO',   -- AUTO | MANUAL | REPLAY
    priority         INTEGER NOT NULL DEFAULT 100,
    status           TEXT NOT NULL DEFAULT 'QUEUED',
        -- QUEUED | RUNNING | SUCCEEDED | RETRYING | FAILED | QUARANTINED | CANCELLED
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    worker           TEXT,
    queued_at        TEXT NOT NULL DEFAULT (datetime('now')),
    started_at       TEXT,
    finished_at      TEXT,
    duration_ms      INTEGER,
    sla_deadline     TEXT,
    sla_breached     INTEGER NOT NULL DEFAULT 0,
    error_class      TEXT,       -- e.g. SourceCorrupt, ValidationFailed, ConverterTimeout
    error_kind       TEXT,       -- TRANSIENT | PERMANENT
    error_message    TEXT,
    next_attempt_at  TEXT,
    replay_of_job_id INTEGER REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS ix_jobs_status   ON jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_jobs_revision ON jobs(revision_id);
CREATE INDEX IF NOT EXISTS ix_jobs_queued   ON jobs(queued_at DESC);

CREATE TABLE IF NOT EXISTS job_steps (
    step_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(job_id),
    seq           INTEGER NOT NULL,
    stage         TEXT NOT NULL,   -- intake|validate|extract|convert|optimize|qa|publish
    attempt       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,   -- RUNNING | SUCCEEDED | FAILED | SKIPPED
    started_at    TEXT,
    finished_at   TEXT,
    duration_ms   INTEGER,
    error_class   TEXT,
    error_message TEXT,
    metrics_json  TEXT             -- stage-specific counters, JSON
);

CREATE INDEX IF NOT EXISTS ix_steps_job   ON job_steps(job_id, seq);
CREATE INDEX IF NOT EXISTS ix_steps_stage ON job_steps(stage, status);

CREATE TABLE IF NOT EXISTS job_log (
    log_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(job_id),
    ts      TEXT NOT NULL DEFAULT (datetime('now')),
    level   TEXT NOT NULL DEFAULT 'INFO',
    stage   TEXT,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_joblog_job ON job_log(job_id, log_id);

-- ---------------------------------------------------------------------
-- Extracted model content (this is what the viewer's tag browser reads,
-- and what downstream analytics / CDE integrations join against)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS components (
    component_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id    INTEGER NOT NULL REFERENCES model_revisions(revision_id),
    tag            TEXT NOT NULL,
    parent_tag     TEXT,
    path           TEXT,
    category       TEXT,
    discipline     TEXT,
    geometry_key   TEXT,
    has_geometry   INTEGER NOT NULL DEFAULT 0,
    triangle_count INTEGER NOT NULL DEFAULT 0,
    bbox_min_x REAL, bbox_min_y REAL, bbox_min_z REAL,
    bbox_max_x REAL, bbox_max_y REAL, bbox_max_z REAL
);

CREATE INDEX IF NOT EXISTS ix_components_rev ON components(revision_id);
CREATE INDEX IF NOT EXISTS ix_components_tag ON components(revision_id, tag);

CREATE TABLE IF NOT EXISTS component_attributes (
    attribute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES components(component_id),
    revision_id  INTEGER NOT NULL REFERENCES model_revisions(revision_id),
    name         TEXT NOT NULL,
    value        TEXT
);

CREATE INDEX IF NOT EXISTS ix_attrs_component ON component_attributes(component_id);
CREATE INDEX IF NOT EXISTS ix_attrs_name      ON component_attributes(revision_id, name);

-- ---------------------------------------------------------------------
-- Quality gates
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_findings (
    finding_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(job_id),
    revision_id   INTEGER NOT NULL REFERENCES model_revisions(revision_id),
    rule_code     TEXT NOT NULL,
    severity      TEXT NOT NULL,   -- INFO | WARN | ERROR
    component_tag TEXT,
    message       TEXT NOT NULL,
    detail_json   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_qa_job      ON qa_findings(job_id);
CREATE INDEX IF NOT EXISTS ix_qa_severity ON qa_findings(severity, rule_code);

-- ---------------------------------------------------------------------
-- Delivery
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS publications (
    publication_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES model_revisions(revision_id),
    job_id          INTEGER NOT NULL REFERENCES jobs(job_id),
    delivery_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    published_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_bytes INTEGER NOT NULL DEFAULT 0,
    -- Size actually transferred to a viewer (HTTP compression), and the
    -- size a naive non-instanced conversion of the same model produced.
    -- Both are measured, so the reported reduction is like-for-like.
    wire_bytes      INTEGER NOT NULL DEFAULT 0,
    naive_bytes     INTEGER NOT NULL DEFAULT 0,
    source_bytes    INTEGER NOT NULL DEFAULT 0,
    triangle_count  INTEGER NOT NULL DEFAULT 0,
    component_count INTEGER NOT NULL DEFAULT 0,
    lod_levels      INTEGER NOT NULL DEFAULT 1,
    is_current      INTEGER NOT NULL DEFAULT 1,
    superseded_at   TEXT,
    rolled_back_at  TEXT
);

CREATE INDEX IF NOT EXISTS ix_pub_revision ON publications(revision_id);
CREATE INDEX IF NOT EXISTS ix_pub_current  ON publications(is_current, published_at DESC);

-- ---------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code       TEXT NOT NULL,
    severity        TEXT NOT NULL,   -- INFO | WARNING | CRITICAL
    subject         TEXT NOT NULL,
    body            TEXT,
    entity_type     TEXT,
    entity_id       TEXT,
    dedupe_key      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_alerts_dedupe ON alerts(dedupe_key, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_alerts_open   ON alerts(resolved_at, severity);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    detail      TEXT
);

-- Simulated downstream consumption, so the dashboard can show
-- "who is actually being served" rather than only pipeline internals.
CREATE TABLE IF NOT EXISTS delivery_stats (
    stat_date       TEXT NOT NULL,
    project_code    TEXT NOT NULL REFERENCES projects(project_code),
    model_id        INTEGER REFERENCES models(model_id),
    viewer_sessions INTEGER NOT NULL DEFAULT 0,
    unique_users    INTEGER NOT NULL DEFAULT 0,
    bytes_served    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stat_date, project_code, model_id)
);

-- =====================================================================
-- Reporting views - these are the shapes a Power BI / analytics layer
-- would bind to, and what the ops dashboard queries.
-- =====================================================================

DROP VIEW IF EXISTS v_job_health;
CREATE VIEW v_job_health AS
SELECT
    p.project_code,
    p.project_name,
    date(j.queued_at)                                            AS run_date,
    COUNT(*)                                                     AS jobs_total,
    SUM(CASE WHEN j.status = 'SUCCEEDED'   THEN 1 ELSE 0 END)    AS jobs_succeeded,
    SUM(CASE WHEN j.status = 'FAILED'      THEN 1 ELSE 0 END)    AS jobs_failed,
    SUM(CASE WHEN j.status = 'QUARANTINED' THEN 1 ELSE 0 END)    AS jobs_quarantined,
    SUM(CASE WHEN j.attempt > 1            THEN 1 ELSE 0 END)    AS jobs_retried,
    SUM(j.sla_breached)                                          AS sla_breaches,
    ROUND(100.0 * SUM(CASE WHEN j.status = 'SUCCEEDED' THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*), 0), 1)                              AS success_rate_pct,
    ROUND(AVG(j.duration_ms) / 1000.0, 1)                        AS avg_duration_s
FROM jobs j
JOIN model_revisions r ON r.revision_id = j.revision_id
JOIN models          m ON m.model_id    = r.model_id
JOIN projects        p ON p.project_code = m.project_code
GROUP BY p.project_code, p.project_name, date(j.queued_at);

DROP VIEW IF EXISTS v_stage_latency;
CREATE VIEW v_stage_latency AS
SELECT
    s.stage,
    COUNT(*)                                                  AS executions,
    SUM(CASE WHEN s.status = 'FAILED' THEN 1 ELSE 0 END)      AS failures,
    ROUND(AVG(s.duration_ms), 0)                              AS avg_ms,
    MAX(s.duration_ms)                                        AS max_ms,
    ROUND(SUM(s.duration_ms) / 1000.0, 1)                     AS total_s
FROM job_steps s
WHERE s.duration_ms IS NOT NULL
GROUP BY s.stage;

DROP VIEW IF EXISTS v_model_currency;
CREATE VIEW v_model_currency AS
SELECT
    m.model_id,
    m.project_code,
    m.model_key,
    m.model_name,
    m.discipline,
    live.revision_label   AS live_revision,
    live.published_at     AS live_published_at,
    live.component_count,
    live.triangle_count,
    live.published_bytes,
    live.wire_bytes,
    live.naive_bytes,
    live.source_bytes,
    -- Optimised delivery against a naive conversion of the same model.
    ROUND(100.0 * (live.naive_bytes - live.published_bytes)
          / NULLIF(live.naive_bytes, 0), 1) AS size_reduction_pct,
    ROUND(100.0 * (live.naive_bytes - live.wire_bytes)
          / NULLIF(live.naive_bytes, 0), 1) AS wire_reduction_pct,
    ROUND((julianday('now') - julianday(live.published_at)) * 24.0, 1) AS age_hours
FROM models m
LEFT JOIN (
    SELECT
        r.model_id,
        r.revision_label,
        p.published_at,
        p.component_count,
        p.triangle_count,
        p.published_bytes,
        p.wire_bytes,
        p.naive_bytes,
        p.source_bytes
    FROM publications p
    JOIN model_revisions r ON r.revision_id = p.revision_id
    WHERE p.is_current = 1
) live ON live.model_id = m.model_id;

DROP VIEW IF EXISTS v_qa_summary;
CREATE VIEW v_qa_summary AS
SELECT
    f.rule_code,
    f.severity,
    COUNT(*)                        AS finding_count,
    COUNT(DISTINCT f.revision_id)   AS revisions_affected
FROM qa_findings f
GROUP BY f.rule_code, f.severity;

DROP VIEW IF EXISTS v_failure_taxonomy;
CREATE VIEW v_failure_taxonomy AS
SELECT
    COALESCE(j.error_class, 'Unknown') AS error_class,
    COALESCE(j.error_kind,  'Unknown') AS error_kind,
    COUNT(*)                           AS occurrences,
    MAX(j.finished_at)                 AS last_seen
FROM jobs j
WHERE j.status IN ('FAILED', 'QUARANTINED')
GROUP BY j.error_class, j.error_kind;
