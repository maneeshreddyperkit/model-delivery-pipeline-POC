-- =====================================================================
-- Model Delivery Pipeline - metadata catalog (SQL Server / Azure SQL)
--
-- WHAT THIS FILE IS
--   The target-platform schema. The proof of concept runs on SQLite
--   (sql/schema_sqlite.sql) because it has to run on one machine with no
--   services to install. This file is the same catalog expressed for the
--   platform it would actually live on.
--
--   Nothing in the POC connects to SQL Server. This is a design artifact,
--   not a live binding, and it is checked in so the migration path is a
--   reviewable thing rather than a verbal claim.
--
-- WHAT CHANGES BETWEEN THE TWO
--   1. Typed columns. SQLite stores everything as TEXT/INTEGER by
--      affinity; here every column has a real type and a real length.
--   2. Enumerations become CHECK constraints instead of comments. The
--      SQLite version documents valid statuses in a trailing comment;
--      at scale that belongs in the engine.
--   3. Index strategy diverges. SQL Server gets filtered indexes,
--      INCLUDE columns, and a columnstore index for the analytics
--      surface. None of those concepts exist in SQLite.
--   4. Time is DATETIME2(3) in UTC via SYSUTCDATETIME(), not a
--      'YYYY-MM-DD HH:MM:SS' string.
--
-- WHAT THE DATA ACCESS LAYER NEEDS
--   src/modelops/db.py is the only module that speaks SQL directly, so
--   the port is contained to that file plus the parameter marker style
--   (? becomes @p1, or pyodbc keeps ?). Query text lives in the stage
--   modules and the reporting views below, which are deliberately
--   written to be portable.
-- =====================================================================

SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------
-- Reference / configuration
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.projects', 'U') IS NULL
CREATE TABLE dbo.projects (
    project_code     NVARCHAR(32)   NOT NULL CONSTRAINT pk_projects PRIMARY KEY,
    project_name     NVARCHAR(200)  NOT NULL,
    district         NVARCHAR(100)  NULL,
    delivery_root    NVARCHAR(400)  NOT NULL,
    sla_minutes      INT            NOT NULL CONSTRAINT df_projects_sla DEFAULT 20,
    subscriber_count INT            NOT NULL CONSTRAINT df_projects_subs DEFAULT 0,
    is_active        BIT            NOT NULL CONSTRAINT df_projects_active DEFAULT 1,
    created_at       DATETIME2(3)   NOT NULL CONSTRAINT df_projects_created DEFAULT SYSUTCDATETIME()
);
GO

-- ---------------------------------------------------------------------
-- Model identity and revisions
--   models         - a logical deliverable, e.g. Unit 10 Piping
--   model_revisions- one specific source export of it
-- ---------------------------------------------------------------------
-- is_simulated marks the fleet-scale backfill described in
-- src/modelops/synth/history.py. The POC converts a handful of models for
-- real and writes the surrounding fleet history straight into the catalog,
-- so charts have realistic volume. Any figure presented as measured filters
-- on is_simulated = 0. It exists on every table the backfill touches.
--
-- In a real deployment this column would either disappear or become a
-- provenance enum (MEASURED / IMPORTED / RECONSTRUCTED), because the same
-- question comes up whenever history is migrated in from a previous system.
IF OBJECT_ID('dbo.models', 'U') IS NULL
CREATE TABLE dbo.models (
    model_id     INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_models PRIMARY KEY,
    project_code NVARCHAR(32)  NOT NULL CONSTRAINT fk_models_project
                     REFERENCES dbo.projects(project_code),
    model_key    NVARCHAR(64)  NOT NULL,
    model_name   NVARCHAR(200) NOT NULL,
    discipline   NVARCHAR(40)  NULL,
    source_tool  NVARCHAR(80)  NULL,
    is_simulated BIT           NOT NULL CONSTRAINT df_models_simulated DEFAULT 0,
    created_at   DATETIME2(3)  NOT NULL CONSTRAINT df_models_created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT uq_models_key UNIQUE (project_code, model_key)
);
GO

IF OBJECT_ID('dbo.model_revisions', 'U') IS NULL
CREATE TABLE dbo.model_revisions (
    revision_id       INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_revisions PRIMARY KEY,
    model_id          INT           NOT NULL CONSTRAINT fk_revisions_model
                          REFERENCES dbo.models(model_id),
    revision_label    NVARCHAR(32)  NOT NULL,
    source_filename   NVARCHAR(260) NOT NULL,
    source_path       NVARCHAR(400) NOT NULL,
    source_sha256     CHAR(64)      NOT NULL,
    source_bytes      BIGINT        NOT NULL,
    source_tool       NVARCHAR(80)  NULL,
    unit_system       NVARCHAR(16)  NULL,
    coordinate_system NVARCHAR(40)  NULL,
    exported_at       DATETIME2(3)  NULL,
    received_at       DATETIME2(3)  NOT NULL CONSTRAINT df_revisions_received DEFAULT SYSUTCDATETIME(),
    status            NVARCHAR(20)  NOT NULL CONSTRAINT df_revisions_status DEFAULT 'RECEIVED',
    is_simulated      BIT           NOT NULL CONSTRAINT df_revisions_simulated DEFAULT 0,
    -- Re-dropping a byte-identical export must not create a second revision.
    CONSTRAINT uq_revisions_content UNIQUE (model_id, source_sha256),
    CONSTRAINT ck_revisions_status CHECK (status IN
        ('RECEIVED','PROCESSING','PUBLISHED','FAILED','QUARANTINED','SUPERSEDED'))
);
GO

CREATE NONCLUSTERED INDEX ix_revisions_model
    ON dbo.model_revisions (model_id, received_at DESC);
CREATE NONCLUSTERED INDEX ix_revisions_status
    ON dbo.model_revisions (status) INCLUDE (model_id, revision_label);
GO

-- ---------------------------------------------------------------------
-- Pipeline execution
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.jobs', 'U') IS NULL
CREATE TABLE dbo.jobs (
    job_id           INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_jobs PRIMARY KEY,
    revision_id      INT           NOT NULL CONSTRAINT fk_jobs_revision
                         REFERENCES dbo.model_revisions(revision_id),
    pipeline_version NVARCHAR(20)  NOT NULL,
    trigger_source   NVARCHAR(16)  NOT NULL CONSTRAINT df_jobs_trigger DEFAULT 'AUTO',
    priority         INT           NOT NULL CONSTRAINT df_jobs_priority DEFAULT 100,
    status           NVARCHAR(20)  NOT NULL CONSTRAINT df_jobs_status DEFAULT 'QUEUED',
    attempt          INT           NOT NULL CONSTRAINT df_jobs_attempt DEFAULT 0,
    max_attempts     INT           NOT NULL CONSTRAINT df_jobs_maxattempt DEFAULT 3,
    worker           NVARCHAR(100) NULL,
    queued_at        DATETIME2(3)  NOT NULL CONSTRAINT df_jobs_queued DEFAULT SYSUTCDATETIME(),
    started_at       DATETIME2(3)  NULL,
    finished_at      DATETIME2(3)  NULL,
    duration_ms      INT           NULL,
    sla_deadline     DATETIME2(3)  NULL,
    sla_breached     BIT           NOT NULL CONSTRAINT df_jobs_sla DEFAULT 0,
    error_class      NVARCHAR(80)  NULL,
    error_kind       NVARCHAR(16)  NULL,
    error_message    NVARCHAR(2000) NULL,
    next_attempt_at  DATETIME2(3)  NULL,
    replay_of_job_id INT           NULL CONSTRAINT fk_jobs_replay
                         REFERENCES dbo.jobs(job_id),
    is_simulated     BIT           NOT NULL CONSTRAINT df_jobs_simulated DEFAULT 0,
    CONSTRAINT ck_jobs_status CHECK (status IN
        ('QUEUED','RUNNING','SUCCEEDED','RETRYING','FAILED','QUARANTINED','CANCELLED')),
    CONSTRAINT ck_jobs_trigger CHECK (trigger_source IN ('AUTO','MANUAL','REPLAY')),
    CONSTRAINT ck_jobs_errorkind CHECK (error_kind IS NULL OR error_kind IN ('TRANSIENT','PERMANENT'))
);
GO

-- The orchestrator's hot path is "give me claimable work", which only ever
-- looks at two statuses. A filtered index keeps that seek off the millions
-- of finished rows this table accumulates.
CREATE NONCLUSTERED INDEX ix_jobs_claimable
    ON dbo.jobs (priority, next_attempt_at, job_id)
    INCLUDE (revision_id, attempt, max_attempts)
    WHERE status IN ('QUEUED', 'RETRYING');

CREATE NONCLUSTERED INDEX ix_jobs_revision ON dbo.jobs (revision_id);
CREATE NONCLUSTERED INDEX ix_jobs_queued   ON dbo.jobs (queued_at DESC)
    INCLUDE (status, duration_ms, error_class);
GO

IF OBJECT_ID('dbo.job_steps', 'U') IS NULL
CREATE TABLE dbo.job_steps (
    step_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_job_steps PRIMARY KEY,
    job_id        INT           NOT NULL CONSTRAINT fk_steps_job REFERENCES dbo.jobs(job_id),
    seq           INT           NOT NULL,
    stage         NVARCHAR(20)  NOT NULL,
    attempt       INT           NOT NULL CONSTRAINT df_steps_attempt DEFAULT 1,
    status        NVARCHAR(16)  NOT NULL,
    started_at    DATETIME2(3)  NULL,
    finished_at   DATETIME2(3)  NULL,
    duration_ms   INT           NULL,
    error_class   NVARCHAR(80)  NULL,
    error_message NVARCHAR(2000) NULL,
    metrics_json  NVARCHAR(MAX) NULL
        CONSTRAINT ck_steps_metrics_json CHECK (metrics_json IS NULL OR ISJSON(metrics_json) = 1),
    CONSTRAINT ck_steps_stage CHECK (stage IN
        ('intake','validate','extract','convert','optimize','qa','publish')),
    CONSTRAINT ck_steps_status CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED'))
);
GO

CREATE NONCLUSTERED INDEX ix_steps_job   ON dbo.job_steps (job_id, seq);
CREATE NONCLUSTERED INDEX ix_steps_stage ON dbo.job_steps (stage, status)
    INCLUDE (duration_ms);
GO

IF OBJECT_ID('dbo.job_log', 'U') IS NULL
CREATE TABLE dbo.job_log (
    log_id  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_job_log PRIMARY KEY,
    job_id  INT           NOT NULL CONSTRAINT fk_joblog_job REFERENCES dbo.jobs(job_id),
    ts      DATETIME2(3)  NOT NULL CONSTRAINT df_joblog_ts DEFAULT SYSUTCDATETIME(),
    level   NVARCHAR(10)  NOT NULL CONSTRAINT df_joblog_level DEFAULT 'INFO',
    stage   NVARCHAR(20)  NULL,
    message NVARCHAR(2000) NOT NULL
);
GO

CREATE NONCLUSTERED INDEX ix_joblog_job ON dbo.job_log (job_id, log_id);
GO

-- ---------------------------------------------------------------------
-- Extracted model content
-- This is the surface the viewer's tag browser reads and the join target
-- for downstream CDE and analytics integrations.
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.components', 'U') IS NULL
CREATE TABLE dbo.components (
    component_id   BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_components PRIMARY KEY,
    revision_id    INT           NOT NULL CONSTRAINT fk_components_revision
                       REFERENCES dbo.model_revisions(revision_id),
    tag            NVARCHAR(120) NOT NULL,
    parent_tag     NVARCHAR(120) NULL,
    path           NVARCHAR(500) NULL,
    category       NVARCHAR(60)  NULL,
    discipline     NVARCHAR(40)  NULL,
    geometry_key   NVARCHAR(64)  NULL,
    has_geometry   BIT           NOT NULL CONSTRAINT df_components_hasgeom DEFAULT 0,
    triangle_count INT           NOT NULL CONSTRAINT df_components_tris DEFAULT 0,
    bbox_min_x FLOAT NULL, bbox_min_y FLOAT NULL, bbox_min_z FLOAT NULL,
    bbox_max_x FLOAT NULL, bbox_max_y FLOAT NULL, bbox_max_z FLOAT NULL
);
GO

CREATE NONCLUSTERED INDEX ix_components_rev ON dbo.components (revision_id)
    INCLUDE (tag, category, discipline, has_geometry);
CREATE NONCLUSTERED INDEX ix_components_tag ON dbo.components (revision_id, tag);
GO

-- Attributes are the highest-row-count table in the catalog: one row per
-- engineering property per component, so a 3,000-component model with 12
-- properties is 36,000 rows, and a project is hundreds of models.
IF OBJECT_ID('dbo.component_attributes', 'U') IS NULL
CREATE TABLE dbo.component_attributes (
    attribute_id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_attributes PRIMARY KEY,
    component_id BIGINT        NOT NULL CONSTRAINT fk_attrs_component
                     REFERENCES dbo.components(component_id),
    revision_id  INT           NOT NULL CONSTRAINT fk_attrs_revision
                     REFERENCES dbo.model_revisions(revision_id),
    name         NVARCHAR(80)  NOT NULL,
    value        NVARCHAR(400) NULL
);
GO

CREATE NONCLUSTERED INDEX ix_attrs_component ON dbo.component_attributes (component_id);
CREATE NONCLUSTERED INDEX ix_attrs_name      ON dbo.component_attributes (revision_id, name)
    INCLUDE (component_id, value);
GO

-- ---------------------------------------------------------------------
-- Quality gates
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.qa_findings', 'U') IS NULL
CREATE TABLE dbo.qa_findings (
    finding_id    BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_qa_findings PRIMARY KEY,
    job_id        INT           NOT NULL CONSTRAINT fk_qa_job REFERENCES dbo.jobs(job_id),
    revision_id   INT           NOT NULL CONSTRAINT fk_qa_revision
                      REFERENCES dbo.model_revisions(revision_id),
    rule_code     NVARCHAR(60)  NOT NULL,
    severity      NVARCHAR(10)  NOT NULL,
    component_tag NVARCHAR(120) NULL,
    message       NVARCHAR(2000) NOT NULL,
    detail_json   NVARCHAR(MAX) NULL
        CONSTRAINT ck_qa_detail_json CHECK (detail_json IS NULL OR ISJSON(detail_json) = 1),
    created_at    DATETIME2(3)  NOT NULL CONSTRAINT df_qa_created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_qa_severity CHECK (severity IN ('INFO','WARN','ERROR'))
);
GO

CREATE NONCLUSTERED INDEX ix_qa_job      ON dbo.qa_findings (job_id);
CREATE NONCLUSTERED INDEX ix_qa_severity ON dbo.qa_findings (severity, rule_code)
    INCLUDE (revision_id);
GO

-- ---------------------------------------------------------------------
-- Delivery
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.publications', 'U') IS NULL
CREATE TABLE dbo.publications (
    publication_id  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_publications PRIMARY KEY,
    revision_id     INT           NOT NULL CONSTRAINT fk_pub_revision
                        REFERENCES dbo.model_revisions(revision_id),
    job_id          INT           NOT NULL CONSTRAINT fk_pub_job REFERENCES dbo.jobs(job_id),
    delivery_path   NVARCHAR(400) NOT NULL,
    manifest_sha256 CHAR(64)      NOT NULL,
    published_at    DATETIME2(3)  NOT NULL CONSTRAINT df_pub_published DEFAULT SYSUTCDATETIME(),
    published_bytes BIGINT        NOT NULL CONSTRAINT df_pub_bytes DEFAULT 0,
    -- wire_bytes  : what a viewer actually transfers, after HTTP compression
    -- naive_bytes : what an unoptimised conversion of the same model produced
    -- Both measured on every run, so the reported reduction is like-for-like
    -- rather than an estimate.
    wire_bytes      BIGINT        NOT NULL CONSTRAINT df_pub_wire DEFAULT 0,
    naive_bytes     BIGINT        NOT NULL CONSTRAINT df_pub_naive DEFAULT 0,
    source_bytes    BIGINT        NOT NULL CONSTRAINT df_pub_source DEFAULT 0,
    triangle_count  INT           NOT NULL CONSTRAINT df_pub_tris DEFAULT 0,
    component_count INT           NOT NULL CONSTRAINT df_pub_components DEFAULT 0,
    lod_levels      INT           NOT NULL CONSTRAINT df_pub_lods DEFAULT 1,
    is_current      BIT           NOT NULL CONSTRAINT df_pub_current DEFAULT 1,
    superseded_at   DATETIME2(3)  NULL,
    rolled_back_at  DATETIME2(3)  NULL,
    is_simulated    BIT           NOT NULL CONSTRAINT df_pub_simulated DEFAULT 0
);
GO

CREATE NONCLUSTERED INDEX ix_pub_revision ON dbo.publications (revision_id);

-- "What is live right now" is the single most frequent read in the app.
-- Filtering the index to is_current = 1 keeps it proportional to the number
-- of live models rather than to the full publication history.
CREATE NONCLUSTERED INDEX ix_pub_current
    ON dbo.publications (published_at DESC)
    INCLUDE (revision_id, component_count, triangle_count,
             published_bytes, wire_bytes, naive_bytes)
    WHERE is_current = 1;
GO

-- ---------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------
IF OBJECT_ID('dbo.alerts', 'U') IS NULL
CREATE TABLE dbo.alerts (
    alert_id        BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_alerts PRIMARY KEY,
    rule_code       NVARCHAR(60)  NOT NULL,
    severity        NVARCHAR(10)  NOT NULL,
    subject         NVARCHAR(300) NOT NULL,
    body            NVARCHAR(2000) NULL,
    entity_type     NVARCHAR(40)  NULL,
    entity_id       NVARCHAR(80)  NULL,
    dedupe_key      NVARCHAR(200) NOT NULL,
    created_at      DATETIME2(3)  NOT NULL CONSTRAINT df_alerts_created DEFAULT SYSUTCDATETIME(),
    acknowledged_at DATETIME2(3)  NULL,
    acknowledged_by NVARCHAR(100) NULL,
    resolved_at     DATETIME2(3)  NULL,
    CONSTRAINT ck_alerts_severity CHECK (severity IN ('INFO','WARNING','CRITICAL'))
);
GO

CREATE NONCLUSTERED INDEX ix_alerts_dedupe ON dbo.alerts (dedupe_key, created_at DESC);
CREATE NONCLUSTERED INDEX ix_alerts_open   ON dbo.alerts (severity, created_at DESC)
    WHERE resolved_at IS NULL;
GO

IF OBJECT_ID('dbo.audit_log', 'U') IS NULL
CREATE TABLE dbo.audit_log (
    audit_id    BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_audit PRIMARY KEY,
    ts          DATETIME2(3)  NOT NULL CONSTRAINT df_audit_ts DEFAULT SYSUTCDATETIME(),
    actor       NVARCHAR(100) NOT NULL CONSTRAINT df_audit_actor DEFAULT 'system',
    action      NVARCHAR(80)  NOT NULL,
    entity_type NVARCHAR(40)  NULL,
    entity_id   NVARCHAR(80)  NULL,
    detail      NVARCHAR(2000) NULL
);
GO

IF OBJECT_ID('dbo.delivery_stats', 'U') IS NULL
CREATE TABLE dbo.delivery_stats (
    stat_date       DATE          NOT NULL,
    project_code    NVARCHAR(32)  NOT NULL CONSTRAINT fk_stats_project
                        REFERENCES dbo.projects(project_code),
    model_id        INT           NOT NULL CONSTRAINT fk_stats_model
                        REFERENCES dbo.models(model_id),
    viewer_sessions INT           NOT NULL CONSTRAINT df_stats_sessions DEFAULT 0,
    unique_users    INT           NOT NULL CONSTRAINT df_stats_users DEFAULT 0,
    bytes_served    BIGINT        NOT NULL CONSTRAINT df_stats_bytes DEFAULT 0,
    CONSTRAINT pk_delivery_stats PRIMARY KEY (stat_date, project_code, model_id)
);
GO

-- =====================================================================
-- Reporting views
-- These are the shapes an analytics layer (Power BI, Databricks, or a
-- Data Factory copy activity) would bind to, and what the ops dashboard
-- queries directly.
-- =====================================================================

IF OBJECT_ID('dbo.v_job_health', 'V') IS NOT NULL DROP VIEW dbo.v_job_health;
GO
CREATE VIEW dbo.v_job_health AS
SELECT
    p.project_code,
    p.project_name,
    CAST(j.queued_at AS DATE)                                       AS run_date,
    COUNT_BIG(*)                                                    AS jobs_total,
    SUM(CASE WHEN j.status = 'SUCCEEDED'   THEN 1 ELSE 0 END)       AS jobs_succeeded,
    SUM(CASE WHEN j.status = 'FAILED'      THEN 1 ELSE 0 END)       AS jobs_failed,
    SUM(CASE WHEN j.status = 'QUARANTINED' THEN 1 ELSE 0 END)       AS jobs_quarantined,
    SUM(CASE WHEN j.attempt > 1            THEN 1 ELSE 0 END)       AS jobs_retried,
    SUM(CAST(j.sla_breached AS INT))                                AS sla_breaches,
    ROUND(100.0 * SUM(CASE WHEN j.status = 'SUCCEEDED' THEN 1 ELSE 0 END)
          / NULLIF(COUNT_BIG(*), 0), 1)                             AS success_rate_pct,
    ROUND(AVG(CAST(j.duration_ms AS FLOAT)) / 1000.0, 1)            AS avg_duration_s
FROM dbo.jobs j
JOIN dbo.model_revisions r ON r.revision_id  = j.revision_id
JOIN dbo.models          m ON m.model_id     = r.model_id
JOIN dbo.projects        p ON p.project_code = m.project_code
GROUP BY p.project_code, p.project_name, CAST(j.queued_at AS DATE);
GO

IF OBJECT_ID('dbo.v_stage_latency', 'V') IS NOT NULL DROP VIEW dbo.v_stage_latency;
GO
CREATE VIEW dbo.v_stage_latency AS
SELECT
    s.stage,
    COUNT_BIG(*)                                              AS executions,
    SUM(CASE WHEN s.status = 'FAILED' THEN 1 ELSE 0 END)      AS failures,
    ROUND(AVG(CAST(s.duration_ms AS FLOAT)), 0)               AS avg_ms,
    MAX(s.duration_ms)                                        AS max_ms,
    ROUND(SUM(CAST(s.duration_ms AS BIGINT)) / 1000.0, 1)     AS total_s
FROM dbo.job_steps s
WHERE s.duration_ms IS NOT NULL
GROUP BY s.stage;
GO

IF OBJECT_ID('dbo.v_model_currency', 'V') IS NOT NULL DROP VIEW dbo.v_model_currency;
GO
CREATE VIEW dbo.v_model_currency AS
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
    ROUND(100.0 * (live.naive_bytes - live.published_bytes)
          / NULLIF(live.naive_bytes, 0), 1) AS size_reduction_pct,
    ROUND(100.0 * (live.naive_bytes - live.wire_bytes)
          / NULLIF(live.naive_bytes, 0), 1) AS wire_reduction_pct,
    ROUND(DATEDIFF(MINUTE, live.published_at, SYSUTCDATETIME()) / 60.0, 1) AS age_hours
FROM dbo.models m
OUTER APPLY (
    SELECT TOP (1)
        r.revision_label,
        p.published_at,
        p.component_count,
        p.triangle_count,
        p.published_bytes,
        p.wire_bytes,
        p.naive_bytes,
        p.source_bytes
    FROM dbo.publications p
    JOIN dbo.model_revisions r ON r.revision_id = p.revision_id
    WHERE r.model_id = m.model_id AND p.is_current = 1
    ORDER BY p.published_at DESC
) live;
GO

IF OBJECT_ID('dbo.v_qa_summary', 'V') IS NOT NULL DROP VIEW dbo.v_qa_summary;
GO
CREATE VIEW dbo.v_qa_summary AS
SELECT
    f.rule_code,
    f.severity,
    COUNT_BIG(*)                    AS finding_count,
    COUNT(DISTINCT f.revision_id)   AS revisions_affected
FROM dbo.qa_findings f
GROUP BY f.rule_code, f.severity;
GO

IF OBJECT_ID('dbo.v_failure_taxonomy', 'V') IS NOT NULL DROP VIEW dbo.v_failure_taxonomy;
GO
CREATE VIEW dbo.v_failure_taxonomy AS
SELECT
    COALESCE(j.error_class, 'Unknown') AS error_class,
    COALESCE(j.error_kind,  'Unknown') AS error_kind,
    COUNT_BIG(*)                       AS occurrences,
    MAX(j.finished_at)                 AS last_seen
FROM dbo.jobs j
WHERE j.status IN ('FAILED', 'QUARANTINED')
GROUP BY j.error_class, j.error_kind;
GO

-- =====================================================================
-- Notes for a real deployment
--
-- Retention. job_log and job_steps grow linearly with throughput. At
-- 100+ models a day across 7 stages that is roughly a quarter of a
-- million step rows a year, and log rows an order of magnitude above
-- that. Partition both on the date column and switch out old partitions
-- rather than deleting rows.
--
-- Analytics. The dashboard aggregates over jobs and job_steps on every
-- page load. Once history is large enough that those scans hurt, a
-- nonclustered columnstore index on job_steps (stage, status,
-- duration_ms, started_at) serves the rollups without disturbing the
-- row-store access the orchestrator depends on.
--
-- Concurrency. The POC serialises writes behind a lock because SQLite
-- takes a database-level write lock. On SQL Server that lock comes out
-- and job claiming becomes an atomic UPDATE ... OUTPUT with READPAST,
-- which is what lets workers scale horizontally:
--
--   UPDATE TOP (1) dbo.jobs WITH (ROWLOCK, READPAST)
--      SET status = 'RUNNING', worker = @worker,
--          started_at = SYSUTCDATETIME(), attempt = attempt + 1
--   OUTPUT inserted.job_id, inserted.revision_id
--    WHERE status IN ('QUEUED','RETRYING')
--      AND (next_attempt_at IS NULL OR next_attempt_at <= SYSUTCDATETIME());
--
-- Azure SQL specifics. Use a database-scoped credential plus Managed
-- Identity rather than SQL auth. The delivery_root and delivery_path
-- columns hold UNC or blob paths, so they stay NVARCHAR rather than
-- becoming a storage-specific type.
-- =====================================================================
