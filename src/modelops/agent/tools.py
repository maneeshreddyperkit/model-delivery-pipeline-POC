"""The tool layer the assistant calls.

Design rules, in order of importance:

1. **Tools read, they never write.** Every one of these runs against a
   read-only connection. A language model cannot requeue a job, roll back
   a publication or edit an attribute, and that is not a limitation to be
   removed later: an agent that can only answer questions has a bounded
   blast radius, and this one is pointed at a catalog that 3,000 people
   depend on.

2. **Tools return the same numbers the pages do.** Each one calls the
   same function the corresponding view calls rather than issuing its own
   SQL. If the assistant and the dashboard ever disagree, one of them is
   lying, and the fastest way to guarantee they cannot is to give them
   one implementation.

3. **Tools that drive the viewer return state, not commands.** A tool
   says "the viewer should be showing this"; the browser decides how to
   get there. That keeps the viewer linkable by URL, replayable, and
   testable without a language model in the loop.

The schemas below are plain JSON Schema and are exposed unmodified at
/tools. That is deliberate: this is the same shape an MCP server
advertises, so the tool layer could be lifted behind an MCP endpoint and
pointed at a real viewer API without the definitions changing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import awp, handover
from ..db import Database
from ..monitoring import failure_taxonomy, overview, qa_summary

# Viewer colour modes, mirroring the dropdown in the viewer. Kept here as
# the single list the assistant is allowed to choose from.
COLOUR_MODES = ["category", "lifecycle", "readiness", "discipline",
                "package", "commissioning"]


@dataclass
class ToolResult:
    """What a tool hands back.

    `text` goes to the model: prose plus a plain-text rendering of any
    rows, because a model cannot see the table the browser draws.

    `headline` is the prose alone. When there is no model the panel shows
    this and renders `data` as a real table, rather than printing the same
    rows twice in two different formats.

    `viewer` is the optional state change the browser applies.
    """
    text: str
    data: Any = None
    viewer: dict | None = None
    columns: list[str] = field(default_factory=list)
    headline: str = ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., ToolResult]

    def schema(self) -> dict:
        """OpenAI/Ollama tool-calling shape, which is also MCP's."""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}


def _str(desc: str, enum: list[str] | None = None) -> dict:
    out: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        out["enum"] = enum
    return out


def _table(rows: list[dict], columns: list[str], limit: int = 25) -> str:
    """Render rows as text for the model. Compact beats pretty here."""
    if not rows:
        return "No rows."
    lines = [" | ".join(columns)]
    for row in rows[:limit]:
        lines.append(" | ".join(str(row.get(c, "")) for c in columns))
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more row(s) not shown")
    return "\n".join(lines)


# =====================================================================
# Tool implementations
# =====================================================================

def _query_catalog(db: Database, project_code: str | None = None,
                   discipline: str | None = None,
                   state: str = "all") -> ToolResult:
    clauses = ["is_simulated = 0"]
    params: list[Any] = []
    if project_code:
        clauses.append("project_code = ?")
        params.append(project_code.upper())
    if discipline:
        clauses.append("discipline = ?")
        params.append(discipline.upper())
    if state == "live":
        clauses.append("live_revision IS NOT NULL")
    elif state == "blocked":
        clauses.append("live_revision IS NULL")

    rows = [dict(r) for r in db.query(
        "SELECT project_code, model_key, model_name, discipline, "
        "live_revision, component_count, triangle_count, published_bytes, "
        "size_reduction_pct, age_hours FROM v_model_currency "
        f"WHERE {' AND '.join(clauses)} ORDER BY project_code, model_key",
        tuple(params))]

    stats = overview(db)
    columns = ["project_code", "model_key", "discipline", "live_revision",
               "component_count", "triangle_count", "size_reduction_pct"]
    header = (f"{len(rows)} model(s) match. Fleet-wide there are "
              f"{stats['models_live']} live models serving "
              f"{stats['subscribers']:,} viewer users, "
              f"{stats['measured_models']} of which this machine converted "
              f"itself rather than replayed from history.")
    return ToolResult(f"{header}\n\n{_table(rows, columns)}", rows,
                      columns=columns, headline=header)


def _find_components(db: Database, tag_contains: str | None = None,
                     category: str | None = None,
                     discipline: str | None = None,
                     iwp: str | None = None,
                     missing_attribute: str | None = None,
                     limit: int = 50) -> ToolResult:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if tag_contains:
        clauses.append("v.tag LIKE ?")
        params.append(f"%{tag_contains}%")
    if category:
        clauses.append("v.category = ?")
        params.append(category)
    if discipline:
        clauses.append("v.discipline = ?")
        params.append(discipline.upper())
    if iwp:
        clauses.append("v.iwp = ?")
        params.append(iwp)
    if missing_attribute:
        column = {"System": "system_code", "Material": "material",
                  "CommissioningSystem": "commissioning_system",
                  "CWA": "cwa", "IWP": "iwp",
                  "Weight": "weight_kg"}.get(missing_attribute)
        if column is None:
            return ToolResult(
                f"{missing_attribute!r} is not an attribute this catalog "
                f"tracks. Try one of: System, Material, CommissioningSystem, "
                f"CWA, IWP, Weight.")
        clauses.append(f"v.has_geometry = 1 AND (v.{column} IS NULL OR v.{column} = '')")

    rows = [dict(r) for r in db.query(
        "SELECT v.project_code, v.model_key, v.tag, v.category, v.discipline, "
        "v.iwp, v.lifecycle_status, v.material, v.commissioning_system "
        f"FROM v_component_packaging v WHERE {' AND '.join(clauses)} "
        "ORDER BY v.project_code, v.model_key, v.tag LIMIT ?",
        tuple(params) + (min(int(limit), 200),))]

    columns = ["model_key", "tag", "category", "discipline", "iwp",
               "lifecycle_status"]
    viewer = None
    if rows:
        # If everything found sits in one model, offer to open it. Jumping
        # the viewer to a model that holds only some of the answer would be
        # worse than not moving it at all.
        models = {(r["project_code"], r["model_key"]) for r in rows}
        if len(models) == 1:
            project, model = models.pop()
            viewer = {"model": f"{project}/{model}"}
            if iwp:
                viewer["iwp"] = iwp

    header = f"{len(rows)} component(s)."
    if rows and len({r["model_key"] for r in rows}) == 1:
        header = f"{len(rows)} component(s), all in {rows[0]['model_key']}."
    return ToolResult(f"{header}\n\n{_table(rows, columns)}",
                      rows, viewer, columns, headline=header)


def _package_readiness(db: Database, iwp: str | None = None,
                       project_code: str | None = None,
                       verdict: str | None = None,
                       blocked_by_data_only: bool = False) -> ToolResult:
    board = awp.package_board(db, project_code=project_code)
    if iwp:
        board = [p for p in board if p["iwp"].upper() == iwp.upper()]
        if not board:
            return ToolResult(f"No package named {iwp}.")
        pkg = board[0]
        detail = awp.package_detail(db, pkg["iwp"])
        lines = [
            f"{pkg['iwp']} is {pkg['verdict']}.",
            f"{pkg['discipline']}, {pkg['components']} components, "
            f"{pkg['weight_kg']:,.0f} kg, "
            f"{pkg['estimated_hours']:,.0f} estimated craft hours, "
            f"planned {pkg['planned_date']}, {pkg['crew']}.",
            "",
            "Constraints:",
        ]
        for c in detail["readiness"].constraints:
            lines.append(f"  [{'pass' if c.passed else 'FAIL'}] {c.label}: {c.detail}")
        return ToolResult("\n".join(lines), pkg,
                          {"model": f"{pkg['project_code']}/{pkg['model_key']}",
                           "iwp": pkg["iwp"], "colour": "lifecycle"})

    if blocked_by_data_only:
        board = [p for p in board if p["attribute_blocked_only"]]
    elif verdict:
        board = [p for p in board if p["verdict"] == verdict.upper()]

    summary = awp.board_summary(awp.package_board(db, project_code=project_code))
    columns = ["iwp", "discipline", "verdict", "components", "planned_date",
               "summary"]
    header = (f"{summary['total']} packages in scope: {summary['ready']} ready, "
              f"{summary['blocked']} blocked, {summary['complete']} complete. "
              f"{summary['attribute_gaps']} carry incomplete attributes and "
              f"{summary['blocked_on_data']} are blocked on that alone. "
              f"Showing {len(board)}.")
    return ToolResult(f"{header}\n\n{_table(board, columns)}", board,
                      columns=columns, headline=header)


def _attribute_gaps(db: Database, project_code: str | None = None) -> ToolResult:
    gaps = handover.completeness(db, project_code)
    short = [a for a in gaps["overall"] if a["missing"]]
    lines = [f"{gaps['components']:,} published components in scope."]
    if not short:
        lines.append("Every handover attribute is fully populated.")
    else:
        lines.append("Attributes with gaps:")
        for a in short:
            lines.append(f"  {a['label']}: {a['pct']}% populated, "
                         f"{a['missing']} missing. Blocks: {a['needed_by']}.")
        lines.append("")
        lines.append("By discipline:")
        for d in gaps["disciplines"]:
            worst = [c for c in d["cells"] if c["missing"]]
            if worst:
                lines.append(
                    f"  {d['discipline']} ({d['components']:,}): " +
                    ", ".join(f"{c['label']} {c['pct']}%" for c in worst))
    return ToolResult("\n".join(lines), gaps,
                      {"colour": "lifecycle"} if short else None)


def _explain_failure(db: Database, model_key: str | None = None,
                     job_id: int | None = None) -> ToolResult:
    if job_id:
        job = db.query_one(
            "SELECT j.*, m.model_key, m.project_code FROM jobs j "
            "LEFT JOIN model_revisions r ON r.revision_id = j.revision_id "
            "LEFT JOIN models m ON m.model_id = r.model_id "
            "WHERE j.job_id = ?", (job_id,))
        if job is None:
            return ToolResult(f"No job {job_id}.")
        steps = [dict(r) for r in db.query(
            "SELECT stage, status, duration_ms, error_class, error_message "
            "FROM job_steps WHERE job_id = ? ORDER BY step_id", (job_id,))]
        lines = [f"Job {job_id} on {job['model_key'] or 'unknown model'} is "
                 f"{job['status']}."]
        if job["error_message"]:
            lines.append(f"Error: {job['error_class']}: {job['error_message']}")
        lines.append("")
        lines.append(_table(steps, ["stage", "status", "duration_ms",
                                    "error_class", "error_message"], limit=12))
        return ToolResult("\n".join(lines), steps)

    if model_key:
        findings = [dict(r) for r in db.query(
            """SELECT q.rule_code, q.severity, q.component_tag, q.message
               FROM qa_findings q
               JOIN model_revisions r ON r.revision_id = q.revision_id
               JOIN models m ON m.model_id = r.model_id
               WHERE m.model_key = ?
               ORDER BY CASE q.severity WHEN 'ERROR' THEN 0 WHEN 'WARNING'
                        THEN 1 ELSE 2 END, q.finding_id""",
            (model_key,))]
        if not findings:
            # No findings can mean two very different things: the model
            # sailed through, or it never reached the gate because an
            # earlier stage stopped it. Saying "passed every gate" for the
            # second case would be wrong in the most misleading direction.
            job = db.query_one(
                """SELECT j.job_id, j.status, j.error_class, j.error_message,
                          r.status AS revision_status
                   FROM jobs j
                   JOIN model_revisions r ON r.revision_id = j.revision_id
                   JOIN models m ON m.model_id = r.model_id
                   WHERE m.model_key = ?
                   ORDER BY j.job_id DESC LIMIT 1""", (model_key,))
            if job is None:
                return ToolResult(f"No model called {model_key} in the catalog.")
            if job["status"] in ("FAILED", "QUARANTINED"):
                stage = db.query_one(
                    "SELECT stage, error_class, error_message FROM job_steps "
                    "WHERE job_id = ? AND status = 'FAILED' "
                    "ORDER BY step_id LIMIT 1", (job["job_id"],))
                where = f" at the {stage['stage']} stage" if stage else ""
                return ToolResult(
                    f"{model_key} did not reach the quality gate. Job "
                    f"{job['job_id']} is {job['status']}{where}: "
                    f"{job['error_class']}: {job['error_message']}",
                    dict(job))
            return ToolResult(
                f"{model_key} has no QA findings recorded, which means it "
                f"passed every gate. Job {job['job_id']} is {job['status']} "
                f"and the revision is {job['revision_status']}.", dict(job))
        errors = sum(1 for f in findings if f["severity"] == "ERROR")
        head = (f"{model_key}: {len(findings)} finding(s), {errors} of them "
                f"blocking." if errors else
                f"{model_key}: {len(findings)} warning(s), none blocking "
                f"publication.")
        columns = ["rule_code", "severity", "component_tag", "message"]
        return ToolResult(f"{head}\n\n{_table(findings, columns)}",
                          findings, columns=columns, headline=head)

    taxonomy = [dict(r) for r in failure_taxonomy(db)]
    gates = [dict(r) for r in qa_summary(db)]
    lines = ["Nothing specified, so here is the fleet picture.", "",
             "Job failures by class:",
             _table(taxonomy, list(taxonomy[0].keys()) if taxonomy else [])]
    if gates:
        lines += ["", "Quality gates that fired:",
                  _table(gates, list(gates[0].keys()))]
    return ToolResult("\n".join(lines), {"failures": taxonomy, "gates": gates})


def _isolate_in_viewer(db: Database, iwp: str) -> ToolResult:
    pkg = db.query_one(
        "SELECT * FROM v_work_package WHERE UPPER(iwp) = ?", (iwp.upper(),))
    if pkg is None:
        near = [r["iwp"] for r in db.query(
            "SELECT iwp FROM v_work_package WHERE iwp LIKE ? LIMIT 5",
            (f"%{iwp}%",))]
        hint = (" Closest matches: " + ", ".join(near)) if near else ""
        return ToolResult(f"No package named {iwp}.{hint}")

    board = awp.package_board(db, model_key=pkg["model_key"])
    verdict = next((p for p in board if p["iwp"] == pkg["iwp"]), None)
    return ToolResult(
        f"Showing {pkg['iwp']} in the viewer: {pkg['components']} components "
        f"in {pkg['model_key']}, "
        f"{(verdict or {}).get('verdict', 'unknown').lower()}. "
        f"{(verdict or {}).get('summary', '')}",
        dict(pkg),
        {"model": f"{pkg['project_code']}/{pkg['model_key']}",
         "iwp": pkg["iwp"]})


def _color_by(db: Database, mode: str, model: str | None = None) -> ToolResult:
    mode = (mode or "").lower()
    if mode not in COLOUR_MODES:
        return ToolResult(
            f"{mode!r} is not a colour mode. Available: "
            f"{', '.join(COLOUR_MODES)}.")
    explain = {
        "category": "the colours the model was published with",
        "lifecycle": "position on the eight-state chain from Designed to Commissioned",
        "readiness": "the readiness verdict of the package each component belongs to",
        "discipline": "discipline as recorded in the catalog",
        "package": "one colour per installation work package",
        "commissioning": "grouped by commissioning system",
    }[mode]
    state = {"colour": mode}
    if model:
        state["model"] = model
    return ToolResult(f"Colouring the model by {explain}.", {"mode": mode}, state)


# Only documents written to be read by a user of the system. The demo
# script, the outreach draft and the deployment notes all live in docs/ as
# well, and none of them are things to hand back to someone who asked what
# a page is for.
RETRIEVABLE_DOCS = ("ABOUT.md",)

# Words that carry no signal here. "page" in particular appears in almost
# every heading, so leaving it in makes every question match everything.
_STOPWORDS = {
    "page", "pages", "tab", "screen", "view", "what", "does", "this", "that",
    "with", "from", "have", "about", "tell", "show", "used", "when", "your",
    "here", "there", "them", "they", "will", "just", "into", "than", "then",
    "some", "much", "many", "doing",
}


def _doc_overview(docs_dir) -> ToolResult:
    """The opening two sections, for questions too general to score."""
    path = docs_dir / RETRIEVABLE_DOCS[0]
    if not path.exists():
        return ToolResult("The project documentation is not available.")
    text = path.read_text(encoding="utf-8")
    # Everything up to the third heading: the summary and the problem
    # statement, which together are the answer to "what is this".
    cut = [i for i, line in enumerate(text.splitlines()) if line.startswith("## ")]
    body = "\n".join(text.splitlines()[:cut[1]]) if len(cut) > 1 else text[:1400]
    return ToolResult(body.strip(), [{"file": path.name, "section": "overview"}])


def _search_docs(db: Database, question: str, limit: int = 3) -> ToolResult:
    """Retrieval over the project's own documentation.

    Deliberately keyword scoring rather than embeddings. The corpus is one
    markdown file; a vector index would be a dependency, a build step and a
    thing to explain, in exchange for no measurable gain at this size.
    """
    from ..config import project_root

    docs_dir = project_root() / "docs"
    words = {w for w in "".join(
        c.lower() if c.isalnum() else " " for c in question).split()
        if len(w) > 3 and w not in _STOPWORDS}
    if not words:
        # "What is this app for?" is entirely common words and is also the
        # single most likely question anyone will ask, so it gets the
        # overview rather than a request to rephrase.
        return _doc_overview(docs_dir)

    scored = []
    for name in RETRIEVABLE_DOCS:
        path = docs_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        # Score by heading section, so a hit returns the passage that
        # answers the question rather than an entire document.
        section, buffer = path.stem, []
        for line in text.splitlines() + ["## "]:
            if line.startswith("#"):
                body = "\n".join(buffer).strip()
                if body:
                    lowered = body.lower()
                    hits = sum(lowered.count(w) for w in words)
                    # A word in the heading is worth far more than the same
                    # word buried in a paragraph, and dividing by length
                    # stops the longest section winning every question
                    # simply by containing more words.
                    heading = sum(1 for w in words if w in section.lower())
                    if hits or heading:
                        # Floor the divisor so a one-line section cannot win
                        # on brevity alone; without it "The SQL page" beats
                        # every other section of every question.
                        score = (hits + heading * 6) / max(len(body), 400) ** 0.5
                        scored.append((score, path.name, section, body))
                section, buffer = line.lstrip("# ").strip(), []
            else:
                buffer.append(line)

    scored.sort(key=lambda s: -s[0])
    if not scored:
        return _doc_overview(docs_dir)

    parts = []
    for _, filename, section, body in scored[:limit]:
        parts.append(f"From {filename}, section {section!r}:\n"
                     f"{body[:1200]}")
    return ToolResult("\n\n---\n\n".join(parts),
                      [{"file": f, "section": s} for _, f, s, _ in scored[:limit]])


# =====================================================================
# Registry
# =====================================================================

TOOLS: list[Tool] = [
    Tool("query_catalog",
         "List engineering models in the delivery catalog with their live "
         "revision, size and optimisation results. Use for questions about "
         "what is published, what is blocked, or how big a model is.",
         {"type": "object", "properties": {
             "project_code": _str("Project code such as KNS-NI1. Omit for all."),
             "discipline": _str("PIPING, STRUCTURAL, ELECTRICAL, HVAC, "
                                "EQUIPMENT or CIVIL. Omit for all."),
             "state": _str("Filter by whether a model is currently served.",
                           ["all", "live", "blocked"]),
         }, "required": []},
         _query_catalog),

    Tool("find_components",
         "Search individual components across published models by tag, "
         "category, discipline, work package, or by which attribute they are "
         "missing. Use for 'which components...' questions.",
         {"type": "object", "properties": {
             "tag_contains": _str("Substring of the component tag."),
             "category": _str("Component category, e.g. Valve, Elbow, Beam."),
             "discipline": _str("Discipline filter."),
             "iwp": _str("Restrict to one installation work package."),
             "missing_attribute": _str(
                 "Return only components missing this attribute.",
                 ["System", "Material", "CommissioningSystem", "CWA", "IWP",
                  "Weight"]),
             "limit": {"type": "integer",
                       "description": "Maximum rows, default 50, cap 200."},
         }, "required": []},
         _find_components),

    Tool("package_readiness",
         "Readiness of installation work packages against four constraints: "
         "engineering issued, materials delivered, predecessor installed, and "
         "model attributes complete. Pass an iwp for the full constraint "
         "breakdown of one package.",
         {"type": "object", "properties": {
             "iwp": _str("One package, e.g. IWP-NI1-100-PIP-01-003."),
             "project_code": _str("Restrict to one project."),
             "verdict": _str("Filter the board by verdict.",
                             ["READY", "BLOCKED", "COMPLETE"]),
             "blocked_by_data_only": {
                 "type": "boolean",
                 "description": "Only packages where incomplete model "
                                "attributes are the sole remaining blocker."},
         }, "required": []},
         _package_readiness),

    Tool("attribute_gaps",
         "Handover attribute completeness across published components, "
         "overall and per discipline. Use for questions about data quality, "
         "handover readiness or what is missing.",
         {"type": "object", "properties": {
             "project_code": _str("Restrict to one project."),
         }, "required": []},
         _attribute_gaps),

    Tool("explain_failure",
         "Explain why a model was not published or why a job failed. Pass a "
         "model_key for its quality-gate findings, a job_id for its per-stage "
         "trace, or neither for the fleet-wide failure breakdown.",
         {"type": "object", "properties": {
             "model_key": _str("Model key, e.g. NI1-HVAC-U10."),
             "job_id": {"type": "integer", "description": "Delivery job id."},
         }, "required": []},
         _explain_failure),

    Tool("isolate_in_viewer",
         "Show one installation work package in the 3D viewer, with the rest "
         "of the model ghosted for context. Use when asked to look at, show, "
         "isolate or find a package in 3D.",
         {"type": "object", "properties": {
             "iwp": _str("Package to isolate, e.g. IWP-NI1-100-PIP-01-003."),
         }, "required": ["iwp"]},
         _isolate_in_viewer),

    Tool("color_by",
         "Recolour the 3D viewer by a property. Use when asked to colour, "
         "shade or highlight the model by status, readiness, discipline, "
         "package or commissioning system.",
         {"type": "object", "properties": {
             "mode": _str("What to colour by.", COLOUR_MODES),
             "model": _str("Optional project/model to switch to first, "
                           "e.g. KNS-NI1/NI1-STEEL-U10."),
         }, "required": ["mode"]},
         _color_by),

    Tool("search_docs",
         "Search this project's own documentation. Use for questions about "
         "what the pipeline is, how it works, why it was built, or what a "
         "particular page is for.",
         {"type": "object", "properties": {
             "question": _str("The user's question, in their words."),
             "limit": {"type": "integer", "description": "Passages, default 3."},
         }, "required": ["question"]},
         _search_docs),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS]


def call(db: Database, name: str, arguments: dict | str) -> ToolResult:
    """Dispatch one tool call, tolerating the ways models mangle arguments."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return ToolResult(f"There is no tool called {name!r}. Available: "
                          f"{', '.join(TOOLS_BY_NAME)}.")

    if isinstance(arguments, str):
        # Some models emit the argument object as a JSON string.
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return ToolResult(f"Could not parse the arguments to {name}.")
    if not isinstance(arguments, dict):
        arguments = {}

    # Drop anything not in the schema instead of raising. A hallucinated
    # extra argument should not turn into a stack trace during a demo.
    allowed = set(tool.parameters.get("properties", {}))
    kwargs = {k: v for k, v in arguments.items() if k in allowed and v not in (None, "")}
    missing = [r for r in tool.parameters.get("required", []) if r not in kwargs]
    if missing:
        return ToolResult(f"{name} needs {', '.join(missing)}.")

    try:
        return tool.run(db, **kwargs)
    except Exception as exc:                      # noqa: BLE001
        return ToolResult(f"{name} failed: {type(exc).__name__}: {exc}")
