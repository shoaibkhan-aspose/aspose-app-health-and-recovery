"""SQLite store for audit runs: one row per run and one per fetched page."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, tenant TEXT, auditor TEXT, started_at TEXT, finished_at TEXT, params TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS pages (
  run_id INTEGER, url TEXT, host TEXT, section TEXT, lang TEXT, template TEXT, source TEXT, group_urls INTEGER,
  status INTEGER, final_url TEXT, redirects TEXT, redirect_statuses TEXT, content_type TEXT, bytes INTEGER,
  truncated INTEGER, blocked INTEGER, error TEXT, elapsed_ms INTEGER, facts TEXT, issues TEXT, ignored TEXT,
  listed_in TEXT,
  PRIMARY KEY (run_id, url)
);
"""

PAGE_COLS = (
    "url host section lang template source group_urls status final_url redirects redirect_statuses content_type "
    "bytes truncated blocked error elapsed_ms facts issues ignored listed_in"
).split()
_JSON = {"redirects", "redirect_statuses", "facts", "issues"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pages)")}
        for col in ("ignored", "listed_in"):  # stores created before these columns existed
            if col not in cols:
                self.db.execute(f"ALTER TABLE pages ADD COLUMN {col} TEXT")

    def start_run(self, tenant: str, auditor: str, params: dict) -> int:
        cur = self.db.execute("INSERT INTO runs (tenant, auditor, started_at, params) VALUES (?, ?, ?, ?)",
                              (tenant, auditor, _now(), json.dumps(params)))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.db.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                        (_now(), json.dumps(summary, default=str), run_id))
        self.db.commit()

    def add_page(self, run_id: int, rec: dict) -> None:
        values = [json.dumps(rec.get(c)) if c in _JSON else rec.get(c) for c in PAGE_COLS]
        cols = ", ".join(["run_id", *PAGE_COLS])
        self.db.execute(f"INSERT OR REPLACE INTO pages ({cols}) VALUES ({', '.join('?' * (len(PAGE_COLS) + 1))})",
                        [run_id, *values])
        self.db.commit()
