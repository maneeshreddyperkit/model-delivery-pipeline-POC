"""WSGI entrypoint for running the dashboard behind a real web server.

    gunicorn wsgi:app

Local development does not need this - `python -m modelops serve` runs the
same app on Flask's built-in server.

The one thing this adds is self-seeding. A hosted container starts with an
empty disk, so if there is no catalog yet it runs the full demo pipeline
(generate models -> scan -> process -> publish) before serving the first
request. That takes a few seconds and it means the deployed instance is
never showing a blank dashboard. It is also an honest reflection of the
POC: there is no external data source, the models are synthesised on the
machine that serves them.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from modelops import awp, cli, handover       # noqa: E402
from modelops.config import load_config       # noqa: E402
from modelops.db import Database              # noqa: E402
from modelops.web.app import create_app       # noqa: E402

_seed_lock = threading.Lock()


def _demo_args() -> argparse.Namespace:
    """The defaults `modelops demo` would have parsed from an empty argv."""
    return argparse.Namespace(
        config=None, seed=20260830, max_seconds=600.0, days=14,
        force=True, quiet=True,
    )


def _catalog_is_populated(db: Database) -> bool:
    try:
        return bool(db.scalar("SELECT COUNT(*) FROM jobs", default=0))
    except Exception:
        return False  # schema not created yet


def bootstrap() -> tuple:
    config = load_config()
    config.ensure_directories()
    db = Database(config.path("catalog_db"))

    with _seed_lock:
        if not _catalog_is_populated(db):
            print("No catalog found - running the demo pipeline to seed one.",
                  flush=True)
            cli.cmd_demo(_demo_args())

    # Work packages and Handover both derive their pages from a pivot over
    # every component attribute, which is slow enough to notice on a small
    # instance. Build them here, for every project filter, so the cost lands
    # on startup rather than on a visitor's first click.
    try:
        scopes = [None] + [r["project_code"] for r in awp.project_options(db)]
        for scope in scopes:
            awp.cached_board(db, scope)
            handover.page_summary(db, scope)
        print(f"Packages and handover warmed for {len(scopes)} scopes.",
              flush=True)
    except Exception as exc:                       # never block serving
        print(f"Warm skipped: {exc}", flush=True)

    return config, db


config, db = bootstrap()
app = create_app(config, db)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765)
