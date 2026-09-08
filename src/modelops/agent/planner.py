"""Rule-based fallback for when there is no language model.

Why this exists rather than an error message
--------------------------------------------
Ollama may not be installed. On a managed corporate machine that is a
policy question, not a technical one, and it is not a question worth
losing a demo over. So the assistant degrades instead of failing: the
same tools, the same answers, chosen by pattern matching rather than by a
model.

This is worth being explicit about, because it is the honest position.
The interesting engineering in an agent is the tool layer, the read-only
boundary and the fact that a tool call changes what is on screen. None of
that needs a language model to be real. What the model adds is tolerance
for how a question is phrased, and that is exactly what is lost here.

The panel says which of the two is answering. Passing off pattern
matching as a language model would be the one thing that could turn a
good demo into a bad interview.
"""

from __future__ import annotations

import re

from ..db import Database
from . import tools

COLOUR_WORDS = {
    "lifecycle": ("install status", "install state", "lifecycle", "installed",
                  "progress", "status"),
    "readiness": ("readiness", "ready", "blocked", "verdict"),
    "discipline": ("discipline",),
    "package": ("work package", "packages", "iwp"),
    "commissioning": ("commissioning", "turnover", "system"),
    "category": ("category", "categories", "default", "original"),
}

DISCIPLINES = ("PIPING", "STRUCTURAL", "ELECTRICAL", "HVAC", "EQUIPMENT", "CIVIL")
ATTRIBUTES = {
    "commissioning system": "CommissioningSystem",
    "material": "Material",
    "system": "System",
    "weight": "Weight",
    "cwa": "CWA",
}

IWP_PATTERN = re.compile(r"\b(IWP-[A-Z0-9-]+)\b", re.I)
MODEL_PATTERN = re.compile(r"\b([A-Z]{2,4}\d?-[A-Z]+-U\d+)\b", re.I)
PROJECT_PATTERN = re.compile(r"\b(KNS-[A-Z0-9]+)\b", re.I)
JOB_PATTERN = re.compile(r"\bjob\s*#?(\d+)\b", re.I)


def plan(question: str) -> tuple[str, dict]:
    """Pick one tool and its arguments. Never returns nothing."""
    q = question.lower()
    project = m.group(1).upper() if (m := PROJECT_PATTERN.search(question)) else None

    # Questions about the application rather than about the data in it.
    # Checked before the topic keywords, because "what does the work
    # packages page do" mentions packages but is not asking for any.
    if q.startswith(("how does", "how do ", "why does", "what happens",
                     "what is this", "what does this", "who is this")) or \
       any(w in q for w in ("explain the app", "what is the point",
                            "why was this built", "what is simulated",
                            "what is real", "what stages", "which stages")) or \
       (any(w in q for w in ("page", "tab", "screen")) and
            any(w in q for w in ("what", "why", "purpose", "for", "do", "does"))):
        return "search_docs", {"question": question}

    # Most specific first: an explicit identifier is unambiguous intent.
    if m := IWP_PATTERN.search(question):
        iwp = m.group(1).upper()
        if any(w in q for w in ("show", "isolate", "look at", "3d", "viewer",
                                "open", "see")):
            return "isolate_in_viewer", {"iwp": iwp}
        return "package_readiness", {"iwp": iwp}

    if m := JOB_PATTERN.search(question):
        return "explain_failure", {"job_id": int(m.group(1))}

    if any(w in q for w in ("why", "fail", "failed", "blocked from", "reject",
                            "quarantin", "gate")):
        if m := MODEL_PATTERN.search(question):
            return "explain_failure", {"model_key": m.group(1).upper()}
        if "package" not in q:
            return "explain_failure", {}

    if any(w in q for w in ("colour", "color", "shade", "highlight")):
        for mode, words in COLOUR_WORDS.items():
            if any(w in q for w in words):
                return "color_by", {"mode": mode}
        return "color_by", {"mode": "lifecycle"}

    mentions_packages = any(w in q for w in ("package", "iwp", "ready to release",
                                             "crew", "start on", "plannable"))
    mentions_gaps = any(w in q for w in ("gap", "missing", "incomplete", "coverage",
                                         "handover", "data quality", "populated"))

    # Gap words and package words together are a question about packages
    # held up by data, which is a different answer from either alone.
    if mentions_gaps and not mentions_packages:
        # "which components are missing X" is a component question; "how
        # complete are attributes" is a rollup question.
        if any(w in q for w in ("which", "list", "show me the", "what components")):
            for phrase, name in ATTRIBUTES.items():
                if phrase in q:
                    return "find_components", {"missing_attribute": name,
                                               "limit": 50}
        return "attribute_gaps", ({"project_code": project} if project else {})

    if mentions_packages:
        args: dict = {}
        if project:
            args["project_code"] = project
        if mentions_gaps or (
                "blocked" in q and any(w in q for w in ("data", "attribute", "model"))):
            args["blocked_by_data_only"] = True
        elif "ready" in q:
            args["verdict"] = "READY"
        elif "blocked" in q:
            args["verdict"] = "BLOCKED"
        elif "complete" in q:
            args["verdict"] = "COMPLETE"
        return "package_readiness", args

    # Whole words only. "pipe" as a substring matches "pipeline", which
    # turns a question about how the system works into a parts list.
    def has_word(*candidates: str) -> str | None:
        for candidate in candidates:
            if re.search(rf"\b{candidate}s?\b", q):
                return candidate
        return None

    if has_word("component", "valve", "elbow", "beam", "pipe", "tag",
                "flange", "duct", "pump"):
        args = {"limit": 50}
        for discipline in DISCIPLINES:
            if discipline.lower() in q:
                args["discipline"] = discipline
        category = has_word("valve", "elbow", "beam", "flange", "pump",
                            "damper", "diffuser", "brace", "column")
        if category:
            args["category"] = category.capitalize()
        return "find_components", args

    if any(w in q for w in ("model", "catalog", "published", "live", "revision",
                            "how many", "size", "triangle")):
        args = {}
        if project:
            args["project_code"] = project
        for discipline in DISCIPLINES:
            if discipline.lower() in q:
                args["discipline"] = discipline
        if "blocked" in q or "not published" in q:
            args["state"] = "blocked"
        elif "live" in q or "published" in q:
            args["state"] = "live"
        return "query_catalog", args

    # Anything else is a question about the app itself.
    return "search_docs", {"question": question}


def answer(db: Database, question: str) -> tuple[str, tools.ToolResult]:
    name, arguments = plan(question)
    result = tools.call(db, name, arguments)
    return name, result
