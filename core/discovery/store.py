"""SQLite inventory: one row per run, robots.txt, sitemap and URL."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, tenant TEXT, started_at TEXT, finished_at TEXT, params TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS robots (
  run_id INTEGER, host TEXT, url TEXT, status INTEGER, error TEXT, bytes INTEGER, is_html INTEGER,
  sitemaps TEXT, user_agents TEXT, disallow_rules INTEGER, ai_bots_named TEXT,
  PRIMARY KEY (run_id, host)
);
CREATE TABLE IF NOT EXISTS sitemaps (
  run_id INTEGER, url TEXT, host TEXT, parent TEXT, root TEXT, depth INTEGER, origins TEXT,
  status INTEGER, final_url TEXT, redirected INTEGER, content_type TEXT, bytes INTEGER,
  uncompressed_bytes INTEGER, gzipped INTEGER, had_bom INTEGER, kind TEXT, entries INTEGER,
  child_count INTEGER, url_count INTEGER, lastmod_min TEXT, lastmod_max TEXT, lastmod_distinct INTEGER,
  lastmod_missing INTEGER, lastmod_invalid INTEGER, hreflang_urls INTEGER, off_host_urls INTEGER,
  http_urls INTEGER, excluded_host_urls INTEGER, dup_in_file INTEGER, error TEXT, examples TEXT,
  PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS urls (
  run_id INTEGER, url TEXT, host TEXT, section TEXT, lang TEXT, template TEXT, lastmod TEXT,
  sitemap TEXT, hreflang_count INTEGER, sitemap_count INTEGER DEFAULT 1,
  PRIMARY KEY (run_id, url)
);
CREATE INDEX IF NOT EXISTS urls_group ON urls (run_id, host, section, lang);
"""

SITEMAP_COLS = (
    "url host parent root depth origins status final_url redirected content_type bytes uncompressed_bytes "
    "gzipped had_bom kind entries child_count url_count lastmod_min lastmod_max lastmod_distinct "
    "lastmod_missing lastmod_invalid hreflang_urls off_host_urls http_urls excluded_host_urls dup_in_file "
    "error examples"
).split()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class InventoryStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def start_run(self, tenant: str, params: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (tenant, started_at, params) VALUES (?, ?, ?)", (tenant, _now(), json.dumps(params))
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
            (_now(), json.dumps(summary, default=str), run_id),
        )
        self.db.commit()

    def add_robots(self, run_id: int, host: str, info: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO robots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, host, info.get("url"), info.get("status"), info.get("error"), info.get("bytes"),
                int(bool(info.get("is_html"))), json.dumps(info.get("sitemaps", [])),
                json.dumps(info.get("user_agents", [])), info.get("disallow_rules", 0),
                json.dumps(info.get("ai_bots_named", [])),
            ),
        )
        self.db.commit()

    def add_sitemap(self, run_id: int, rec: dict) -> None:
        values = [json.dumps(rec[c]) if c == "examples" and c in rec else rec.get(c) for c in SITEMAP_COLS]
        cols = ", ".join(["run_id", *SITEMAP_COLS])
        marks = ", ".join("?" * (len(SITEMAP_COLS) + 1))
        self.db.execute(f"INSERT OR REPLACE INTO sitemaps ({cols}) VALUES ({marks})", [run_id, *values])
        self.db.commit()

    def add_urls(self, rows: list[tuple]) -> None:
        """rows: (run_id, url, host, section, lang, template, lastmod, sitemap, hreflang_count)."""
        self.db.executemany(
            "INSERT INTO urls (run_id, url, host, section, lang, template, lastmod, sitemap, hreflang_count) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (run_id, url) DO UPDATE SET sitemap_count = sitemap_count + 1, "
            "hreflang_count = MAX(hreflang_count, excluded.hreflang_count)",
            rows,
        )
        self.db.commit()

    # ---- queries used by the run summary and findings ----

    def section_stats(self, run_id: int) -> list[tuple]:
        """(host, section, lang, urls, urls_with_hreflang)."""
        return self.db.execute(
            "SELECT host, section, COALESCE(lang, ''), COUNT(*), SUM(hreflang_count > 0) FROM urls "
            "WHERE run_id = ? GROUP BY 1, 2, 3",
            (run_id,),
        ).fetchall()

    def template_stats(self, run_id: int) -> list[tuple]:
        """(host, section, template, urls)."""
        return self.db.execute(
            "SELECT host, section, template, COUNT(*) FROM urls WHERE run_id = ? GROUP BY 1, 2, 3", (run_id,)
        ).fetchall()

    def cross_sitemap_dups(self, run_id: int) -> list[tuple]:
        """(host, section, urls listed in 2+ sitemaps)."""
        return self.db.execute(
            "SELECT host, section, COUNT(*) FROM urls WHERE run_id = ? AND sitemap_count > 1 GROUP BY 1, 2",
            (run_id,),
        ).fetchall()

    def sample_urls(self, run_id: int, host: str, section: str | None, where: str = "1", limit: int = 5) -> list[str]:
        sql = f"SELECT url FROM urls WHERE run_id = ? AND host = ? AND ({where})"
        args: list = [run_id, host]
        if section is not None:
            sql += " AND section = ?"
            args.append(section)
        return [r[0] for r in self.db.execute(sql + " LIMIT ?", [*args, limit])]

    def sample_sitemaps(self, run_id: int, host: str, section: str | None, limit: int = 5) -> list[str]:
        sql = "SELECT DISTINCT sitemap FROM urls WHERE run_id = ? AND host = ?"
        args: list = [run_id, host]
        if section is not None:
            sql += " AND section = ?"
            args.append(section)
        return [r[0] for r in self.db.execute(sql + " LIMIT ?", [*args, limit])]
