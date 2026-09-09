"""SQLite data-access layer for the model catalog.

Design notes that matter for the POC being credible:

* One connection per thread. The orchestrator runs a worker pool, and
  sqlite3 connections are not safe to share across threads.
* WAL journalling plus a busy timeout, so concurrent workers writing job
  progress do not trip over each other with "database is locked".
* All SQL is parameterised. The dashboard's ad-hoc query page is the one
  place free text reaches the engine, and it is gated to read-only.

The catalog is a single file on disk (data/catalog.db). Nothing here
opens a network socket.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

_local = threading.local()
_write_lock = threading.Lock()


def utcnow() -> str:
    """Timestamps are stored as UTC 'YYYY-MM-DD HH:MM:SS' to match SQLite's
    own datetime('now'), so mixed application/DDL defaults stay comparable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def data_version(db: "Database") -> tuple:
    """Cheap token that changes whenever the live model set changes.

    Everything the packaging and handover pages derive is a function of
    the current publications, so a page can cache its summary against
    this and be certain it is never showing a superseded model.
    """
    row = db.query("SELECT COUNT(*), COALESCE(MAX(publication_id), 0) "
                   "FROM publications WHERE is_current = 1")
    return tuple(row[0]) if row else (0, 0)


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # -- connection handling ----------------------------------------------
    def connect(self) -> sqlite3.Connection:
        key = f"conn_{id(self)}"
        conn = getattr(_local, key, None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            setattr(_local, key, conn)
        return conn

    def close(self) -> None:
        key = f"conn_{id(self)}"
        conn = getattr(_local, key, None)
        if conn is not None:
            conn.close()
            setattr(_local, key, None)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction."""
        conn = self.connect()
        with _write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # -- primitives --------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self.tx() as conn:
            conn.executemany(sql, rows)

    # -- schema ------------------------------------------------------------
    def initialise(self, schema_sql_path: str | Path) -> None:
        script = Path(schema_sql_path).read_text(encoding="utf-8")
        conn = self.connect()
        with _write_lock:
            conn.executescript(script)

    def reset(self, schema_sql_path: str | Path) -> None:
        """Drop everything and rebuild. Used by `modelops init --force`."""
        conn = self.connect()
        with _write_lock:
            # Tables reference each other, so any drop order violates some
            # constraint. Constraints are re-enabled by initialise().
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                for kind in ("view", "table"):
                    names = [
                        r[0] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = ? "
                            "AND name NOT LIKE 'sqlite_%'", (kind,)
                        ).fetchall()
                    ]
                    for name in names:
                        conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        self.initialise(schema_sql_path)

    # -- audit -------------------------------------------------------------
    def audit(self, action: str, *, actor: str = "system", entity_type: str | None = None,
              entity_id: Any = None, detail: Any = None) -> None:
        self.execute(
            "INSERT INTO audit_log (ts, actor, action, entity_type, entity_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), actor, action, entity_type,
             None if entity_id is None else str(entity_id),
             detail if isinstance(detail, str) or detail is None else json.dumps(detail)),
        )


# =====================================================================
# Read-only guard for the dashboard's ad-hoc SQL page.
# =====================================================================

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    pass


def assert_read_only(sql: str) -> str:
    """Allow a single SELECT / WITH statement and nothing else.

    The dashboard exposes a query box because 'strong SQL' is a core part
    of this role and it is worth demonstrating live. It is still user
    input reaching a database, so it is restricted rather than trusted.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise UnsafeQuery("Empty query.")
    if ";" in stripped:
        raise UnsafeQuery("Only one statement may be run at a time.")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise UnsafeQuery("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(stripped):
        raise UnsafeQuery("Only read-only queries are allowed.")
    return stripped


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Second line of defence: the OS-level file mode is read-only too."""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn
