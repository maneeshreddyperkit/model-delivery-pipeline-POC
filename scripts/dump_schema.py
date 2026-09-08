"""Print the DDL for the tables and views the handover feeds read from."""
import sqlite3
import sys

WANTED = ("v_component_packaging", "v_work_package", "components",
          "component_attributes", "publications", "deliverables",
          "v_model_currency", "models", "model_revisions", "projects")

db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "data/catalog.db")
for name in WANTED:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    print(row[0] if row else f"-- {name}: not found", "\n")
