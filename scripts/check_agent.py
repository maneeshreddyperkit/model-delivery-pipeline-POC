"""Exercise every agent tool and a spread of rule-based questions.

Runs without Ollama: the point is to prove the tool layer and the
fallback answer correctly on their own, since those are what the demo
depends on if no model is installed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from modelops.agent import Agent, tools          # noqa: E402
from modelops.config import load_config          # noqa: E402
from modelops.db import Database                 # noqa: E402

config = load_config()
db = Database(config.path("catalog_db"))
BAR = "-" * 78


def show(label, result):
    print(f"\n{BAR}\n{label}\n{BAR}")
    text = result.text if hasattr(result, "text") else result
    print("\n".join(text.splitlines()[:10]))
    viewer = getattr(result, "viewer", None)
    if viewer:
        print(f"  -> viewer: {viewer}")


print("=== every tool, called directly ===")
for name, args in [
    ("query_catalog", {"state": "live"}),
    ("find_components", {"category": "Valve", "limit": 5}),
    ("find_components", {"missing_attribute": "CommissioningSystem", "limit": 5}),
    ("package_readiness", {"blocked_by_data_only": True}),
    ("package_readiness", {"iwp": "IWP-BOP-200-STL-01-001"}),
    ("attribute_gaps", {}),
    ("explain_failure", {"model_key": "NI1-HVAC-U10"}),
    ("isolate_in_viewer", {"iwp": "IWP-NI1-100-PIP-01-003"}),
    ("color_by", {"mode": "readiness"}),
    ("search_docs", {"question": "what problem does this pipeline solve"}),
    ("color_by", {"mode": "nonsense"}),
    ("isolate_in_viewer", {"iwp": "IWP-DOES-NOT-EXIST"}),
]:
    show(f"{name}({args})", tools.call(db, name, args))

print(f"\n\n=== rule-based routing (no model installed) ===")
agent = Agent(db)
print(f"Ollama: {agent.health().detail or agent.health().model}")
for question in [
    "what is this app for?",
    "how many models are live?",
    "which packages are blocked only by missing data?",
    "show me IWP-NI1-100-PIP-01-003 in 3d",
    "why did NI1-HVAC-U10 fail?",
    "colour the model by install status",
    "which components are missing a commissioning system?",
    "how complete are the handover attributes for KNS-NI1?",
    "list the valves",
    "what happened in job 3?",
]:
    turn = agent.ask(question)
    print(f"\n{BAR}\nQ: {question}\n   tool: {turn.calls[0]['name']}"
          f"({turn.calls[0]['arguments']})")
    print("   " + "\n   ".join(turn.reply.splitlines()[:4]))
    if turn.viewer:
        print(f"   -> viewer: {turn.viewer}")
