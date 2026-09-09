"""Advanced Work Packaging: readiness of installation work packages.

The argument this module exists to make
---------------------------------------
CII's AWP procedure states that work-packaging software "is critically
dependent upon the structure, attributes and integrity of the 3D model."
That is the whole thesis of this proof of concept, stated by someone
other than me.

The pipeline already enforces attribute integrity at the QA gate. This
module spends that integrity: it takes the same extracted attributes and
answers a question a superintendent actually asks, which is *can this
crew start on Monday*.

A package is released only when four constraints hold:

  1. **Engineering issued.** Nothing in the package is still at Designed.
     You cannot install to a drawing that has not been issued.
  2. **Materials on site.** Every component has reached Delivered.
     One missing spool stops the package, not 3% of it.
  3. **Predecessor complete.** The package immediately before this one in
     the Path of Construction is installed. You do not hang pipe before
     the steel it hangs from.
  4. **Attributes complete.** Every component carries the attributes the
     package is planned, counted and costed against.

Constraint 4 is the one that usually gets waved through, and it is the
one this pipeline can actually guarantee. The other three describe the
physical world; that one describes the data, and bad data fails the
package just as hard.

Deliberate simplification: real readiness also covers scaffold, permits,
equipment, craft availability and safety plans. Those are not modelled
here because none of them come from the 3D model, and inventing them
would blur what this pipeline can honestly claim to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import Database, data_version

# Order matters: this is the chain a component progresses along, and
# "has it reached state X" is an index comparison against it.
LIFECYCLE_STATES = [
    "Designed", "IFC", "Procured", "Fabricated",
    "Delivered", "Installed", "Tested", "Commissioned",
]
STATE_INDEX = {name: i for i, name in enumerate(LIFECYCLE_STATES)}

READY = "READY"
BLOCKED = "BLOCKED"
COMPLETE = "COMPLETE"


@dataclass
class Constraint:
    code: str
    label: str
    passed: bool
    detail: str


@dataclass
class PackageReadiness:
    iwp: str
    verdict: str
    constraints: list[Constraint] = field(default_factory=list)

    @property
    def blockers(self) -> list[Constraint]:
        return [c for c in self.constraints if not c.passed]

    @property
    def summary(self) -> str:
        if self.verdict == COMPLETE:
            # Being built does not close out the data. Say so here rather
            # than reporting a clean "handed over" on a package whose
            # records are incomplete, which is the failure this whole
            # page exists to make visible.
            gap = next((c for c in self.blockers
                        if c.code == "ATTRIBUTES_COMPLETE"), None)
            if gap:
                return "Installed, but handed over incomplete: " + \
                    gap.detail.split(": ", 1)[-1]
            return "Installed and handed over."
        if self.verdict == READY:
            return "All constraints satisfied; can be released to a crew."
        first = self.blockers[0]
        extra = len(self.blockers) - 1
        return first.detail + (f" (+{extra} more)" if extra else "")


def _packages(db: Database, where: str = "", params: tuple = ()) -> list[dict]:
    return [dict(r) for r in db.query(
        f"SELECT * FROM v_work_package {where} "
        "ORDER BY project_code, cwp, poc_sequence", params)]


def evaluate(package: dict, predecessors: list[dict]) -> PackageReadiness:
    """Apply the four constraints to one package."""
    total = package["components"] or 0
    constraints: list[Constraint] = []

    # 1. Engineering issued
    not_issued = package["not_yet_issued"] or 0
    constraints.append(Constraint(
        "ENGINEERING_ISSUED", "Engineering issued for construction",
        not_issued == 0,
        "Engineering issued." if not_issued == 0 else
        f"{not_issued} of {total} components are still at Designed, "
        f"so {package['ewp']} has not been fully issued."))

    # 2. Materials on site
    on_site = package["materials_on_site"] or 0
    short = total - on_site
    constraints.append(Constraint(
        "MATERIALS_DELIVERED", "Materials delivered to site",
        short == 0,
        "All materials on site." if short == 0 else
        f"{short} of {total} components have not reached Delivered."))

    # 3. Predecessor complete
    #
    # Only the package immediately before this one in the Path of
    # Construction, not every earlier package. Requiring the whole chain
    # would be both wrong and useless: wrong because AWP sequences the
    # handoff between adjacent packages, and useless because one stalled
    # package early in a CWP would mark everything behind it blocked and
    # bury the reason that actually matters.
    previous = max(predecessors, key=lambda p: p["poc_sequence"] or 0,
                   default=None)
    previous_done = (previous is None
                     or (previous["installed"] or 0) >= (previous["components"] or 0))
    constraints.append(Constraint(
        "PREDECESSORS_COMPLETE", "Preceding package installed",
        previous_done,
        "No outstanding predecessor." if previous_done else
        f"{previous['iwp']} comes before this one in {package['cwp']} and is "
        f"{previous['installed']} of {previous['components']} installed."))

    # 4. Attribute integrity
    gaps = {
        "commissioning system": package["missing_commissioning_system"] or 0,
        "material": package["missing_material"] or 0,
        "system": package["missing_system"] or 0,
    }
    missing = {k: v for k, v in gaps.items() if v}
    constraints.append(Constraint(
        "ATTRIBUTES_COMPLETE", "Model attributes complete",
        not missing,
        "Attributes complete." if not missing else
        "Cannot be planned from the model: " + ", ".join(
            f"{n} component(s) missing {k}" for k, n in missing.items()) + "."))

    if total and (package["installed"] or 0) >= total:
        verdict = COMPLETE
    elif all(c.passed for c in constraints):
        verdict = READY
    else:
        verdict = BLOCKED
    return PackageReadiness(package["iwp"], verdict, constraints)


def package_board(db: Database, *, project_code: str | None = None,
                  model_key: str | None = None) -> list[dict]:
    """Every package with its readiness verdict, ready for a table."""
    clauses, params = [], []
    if project_code:
        clauses.append("project_code = ?")
        params.append(project_code)
    if model_key:
        clauses.append("model_key = ?")
        params.append(model_key)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    packages = _packages(db, where, tuple(params))

    # Predecessors are packages in the same CWP earlier in the Path of
    # Construction. Grouped once here rather than queried per package.
    by_cwp: dict[str, list[dict]] = {}
    for pkg in packages:
        by_cwp.setdefault(pkg["cwp"], []).append(pkg)

    out = []
    for pkg in packages:
        predecessors = [p for p in by_cwp.get(pkg["cwp"], [])
                        if (p["poc_sequence"] or 0) < (pkg["poc_sequence"] or 0)]
        readiness = evaluate(pkg, predecessors)
        blockers = readiness.blockers
        out.append({
            **pkg,
            "verdict": readiness.verdict,
            "summary": readiness.summary,
            "blockers": [c.code for c in blockers],
            "blocker_count": len(blockers),
            # True when the model data is the *only* thing in the way:
            # everything physical is done and a missing attribute is
            # holding the package. That is the case worth arguing about.
            "attribute_blocked_only": (
                readiness.verdict == BLOCKED and len(blockers) == 1
                and blockers[0].code == "ATTRIBUTES_COMPLETE"),
            # Built, handed over, and still carrying incomplete data. Not a
            # scheduling problem any more, a handover problem.
            "complete_with_gaps": (readiness.verdict == COMPLETE
                                   and "ATTRIBUTES_COMPLETE" in
                                   [c.code for c in blockers]),
            "progress_pct": round(100.0 * (pkg["installed"] or 0)
                                  / (pkg["components"] or 1), 1),
        })
    return out


# The board is derived entirely from the current publications, and costs a
# scan of v_component_packaging to build. The packages page renders it on
# every visit and every filter change, so it is cached the same way the
# handover summary is: against a token that moves only when a model
# publishes. Filters are applied to the cached board in the view, which is
# why they are not part of the key.
_board_cache: dict[tuple[str | None, tuple], list[dict]] = {}


def cached_board(db: Database, project_code: str | None = None) -> list[dict]:
    """package_board, memoised against the live model set."""
    version = data_version(db)
    key = (project_code, version)
    board = _board_cache.get(key)
    if board is None:
        for stale in [k for k in _board_cache if k[1] != version]:
            del _board_cache[stale]
        board = _board_cache[key] = package_board(db, project_code=project_code)
    return board


def project_options(db: Database) -> list[dict]:
    """Projects that have packages, shaped for the filter row templates.

    Read off the cached board rather than queried, because a DISTINCT over
    v_work_package costs the same scan as the board itself and both the
    packages and handover pages need it on every render.
    """
    return [{"project_code": code}
            for code in sorted({p["project_code"] for p in cached_board(db)})]


def package_detail(db: Database, iwp: str) -> dict | None:
    """One package: constraints, bill of materials, and its components."""
    package = db.query_one("SELECT * FROM v_work_package WHERE iwp = ?", (iwp,))
    if package is None:
        return None
    package = dict(package)

    siblings = _packages(db, "WHERE cwp = ?", (package["cwp"],))
    predecessors = [p for p in siblings
                    if (p["poc_sequence"] or 0) < (package["poc_sequence"] or 0)]
    readiness = evaluate(package, predecessors)

    bom = [dict(r) for r in db.query(
        "SELECT category, COUNT(*) AS quantity, "
        "       ROUND(SUM(CAST(COALESCE(weight_kg, '0') AS REAL)), 1) AS weight_kg, "
        "       SUM(triangle_count) AS triangles, "
        "       COUNT(DISTINCT material) AS materials "
        "FROM v_component_packaging WHERE iwp = ? "
        "GROUP BY category ORDER BY quantity DESC", (iwp,))]

    lifecycle = [dict(r) for r in db.query(
        "SELECT lifecycle_status, COUNT(*) AS n FROM v_component_packaging "
        "WHERE iwp = ? GROUP BY lifecycle_status", (iwp,))]
    lifecycle.sort(key=lambda r: STATE_INDEX.get(r["lifecycle_status"], 99))

    components = [dict(r) for r in db.query(
        "SELECT tag, category, material, lifecycle_status, commissioning_system, "
        "       weight_kg, has_geometry "
        "FROM v_component_packaging WHERE iwp = ? ORDER BY tag", (iwp,))]

    return {
        "package": package,
        "readiness": readiness,
        "bom": bom,
        "lifecycle": lifecycle,
        "components": components,
        "predecessors": predecessors,
        "progress_pct": round(100.0 * (package["installed"] or 0)
                              / (package["components"] or 1), 1),
    }


def board_summary(board: list[dict]) -> dict:
    """Headline counts for the packages page."""
    return {
        "total": len(board),
        "ready": sum(1 for p in board if p["verdict"] == READY),
        "blocked": sum(1 for p in board if p["verdict"] == BLOCKED),
        "complete": sum(1 for p in board if p["verdict"] == COMPLETE),
        # Any package whose attributes are incomplete, and the subset where
        # that is the only obstacle left.
        "attribute_gaps": sum(
            1 for p in board if "ATTRIBUTES_COMPLETE" in p["blockers"]),
        "blocked_on_data": sum(
            1 for p in board if p["attribute_blocked_only"]),
        "complete_with_gaps": sum(1 for p in board if p["complete_with_gaps"]),
        "hours_ready": round(sum(p["estimated_hours"] or 0
                                 for p in board if p["verdict"] == READY)),
        "hours_blocked": round(sum(p["estimated_hours"] or 0
                                   for p in board if p["verdict"] == BLOCKED)),
    }
