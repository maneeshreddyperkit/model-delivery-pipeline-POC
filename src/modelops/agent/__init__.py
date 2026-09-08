"""A local assistant over the delivery catalog.

What it is
----------
A tool-calling loop against a language model running on this machine,
wired to a read-only view of the same catalog the dashboard reads. It can
answer questions about the fleet and it can change what the 3D viewer is
showing, which is the part that matters: a chat panel that only produces
paragraphs is a worse interface than the pages it sits on top of.

What it is not
--------------
It cannot write. There is no tool that requeues a job, edits an
attribute, rolls back a publication or touches a file. That boundary is
enforced in the tool layer rather than in the prompt, because a boundary
a model is merely asked to respect is not a boundary.

If no model is installed it falls back to rule-based intent matching over
the same tools, and says so in the response. See planner.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db import Database
from . import planner, tools
from .llm import Health, Ollama

MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """\
You are the assistant built into a model delivery pipeline for Kiewit \
Nuclear Solutions. The pipeline takes engineering models out of design \
tools, validates them, extracts their attributes into a SQL catalog, \
optimises the geometry, puts every model through quality gates, and \
publishes what passes to a 3D viewer used across the project.

Answer using the tools. Do not guess numbers: if you have not called a \
tool for a figure, you do not know it. If a tool returns nothing, say so \
plainly rather than inventing a plausible answer.

The point the pipeline exists to make is that work packaging, \
commissioning and progress reporting all depend on the attributes in the \
model being complete, and that this is measurable rather than assumed. \
When a question touches readiness or handover, lead with what the data \
says and name the specific components or packages involved.

Be brief. Two or three sentences unless asked for detail. You are \
talking to engineers who will check what you tell them.\
"""


@dataclass
class Turn:
    """One exchange, including the machinery behind it.

    The tool trace is returned to the browser and shown in the panel. An
    assistant that says "14 packages are blocked" is a claim; one that
    shows it called package_readiness to find out is an audit trail, and
    the second is the only version worth putting in front of someone who
    owns the underlying platform.
    """
    reply: str
    engine: str                              # "model" or "rules"
    model: str | None = None
    calls: list[dict] = field(default_factory=list)
    viewer: dict | None = None
    data: object = None
    columns: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, db: Database, host: str | None = None,
                 model: str | None = None):
        self.db = db
        self.client = Ollama(host or "http://127.0.0.1:11434", model)
        self._health: Health | None = None

    def health(self, *, refresh: bool = False) -> Health:
        # Cached because the chat panel asks on every page load and a dead
        # Ollama would otherwise cost a socket timeout each time.
        if self._health is None or refresh:
            self._health = self.client.health()
            if self._health.available:
                self.client.model = self._health.model
        return self._health

    def ask(self, question: str, history: list[dict] | None = None) -> Turn:
        health = self.health()
        if not health.available:
            return self._ask_rules(question, health.detail)
        try:
            return self._ask_model(question, history or [])
        except Exception as exc:                  # noqa: BLE001
            # A model that times out or returns something unparseable must
            # not take the panel down mid-demo. Fall through to the rules
            # and say what happened.
            self._health = None
            return self._ask_rules(
                question, f"The model failed part way through "
                          f"({type(exc).__name__}), so this answer came from "
                          f"the rule-based fallback.")

    # -- rules ---------------------------------------------------------
    def _ask_rules(self, question: str, reason: str) -> Turn:
        name, arguments = planner.plan(question)
        result = tools.call(self.db, name, arguments)
        return Turn(
            # The panel draws the rows itself, so the prose alone here.
            reply=result.headline or result.text,
            engine="rules",
            calls=[{"name": name, "arguments": arguments,
                    "summary": reason or _first_line(result.text)}],
            viewer=result.viewer,
            data=result.data,
            columns=result.columns,
        )

    # -- model ---------------------------------------------------------
    def _ask_model(self, question: str, history: list[dict]) -> Turn:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Only the last few turns. Context is the main cost driver at this
        # model size and older turns rarely change the answer.
        messages += history[-6:]
        messages.append({"role": "user", "content": question})

        schemas = tools.schemas()
        calls: list[dict] = []
        viewer: dict | None = None
        data = None
        columns: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            message = self.client.chat(messages, schemas)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return Turn(
                    reply=(message.get("content") or "").strip() or
                          "I do not have an answer for that.",
                    engine="model", model=self.client.model,
                    calls=calls, viewer=viewer, data=data, columns=columns)

            messages.append(message)
            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = function.get("arguments", {})
                result = tools.call(self.db, name, arguments)

                calls.append({"name": name, "arguments": arguments,
                              "summary": _first_line(result.text)})
                # Later calls win: if the model isolates a package and then
                # recolours, the browser should end up in the second state.
                if result.viewer:
                    viewer = {**(viewer or {}), **result.viewer}
                if result.data is not None:
                    data, columns = result.data, result.columns

                messages.append({"role": "tool", "name": name,
                                 "content": result.text[:6000]})

        return Turn(
            reply="I went round the tools several times without settling on "
                  "an answer. The tool output above is what I found.",
            engine="model", model=self.client.model,
            calls=calls, viewer=viewer, data=data, columns=columns)


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()
    return line[0][:180] if line else ""
