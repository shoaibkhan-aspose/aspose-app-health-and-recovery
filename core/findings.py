"""Findings: records in the schema confirmed in CLAUDE.md, and the SQLite store that tracks them across runs.

Every finding has a stable `key`. Code findings: `<dimension>/<check>/<subdomain>/<template>`; seed (manual)
findings: `seed/<id>`. The store gives each key one permanent id (crawl-0001, ...), records when it was first
and last seen, marks it resolved when a later run in the same scope no longer reports it, and reopens it if
it comes back.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

FINDING_TYPES = ("broken", "not_optimized", "incorrect", "improvable")
EFFORTS = ("S", "M", "L")
STATUSES = ("open", "unverified", "resolved", "superseded")
# How a fix would ship (Phase 2). None = unknown.
FIX_CHANNELS = ("git-pr", "wordpress-api", "wordpress-admin", "discourse-admin", "routing-cdn", "hosting")


class FindingIds:
    """Sequential ids per dimension within one run: crawl-0001, crawl-0002, intl-0001, ..."""

    def __init__(self):
        self._n: Counter = Counter()

    def next(self, dimension: str) -> str:
        self._n[dimension] += 1
        return f"{dimension}-{self._n[dimension]:04d}"


def finding_key(f: dict) -> str:
    if f.get("check"):
        return f"{f['dimension']}/{f['check']}/{f['subdomain']}/{f['template']}"
    return f"seed/{f['id']}"


def template_section(template: str) -> str | None:
    """'sitemap:pdf' -> 'pdf'; 'robots.txt' -> None."""
    return template.split(":", 1)[1] if ":" in template else None


def make_finding(
    *,
    ids: FindingIds,
    tenant: str,
    subdomain: str,
    template: str,
    dimension: str,
    type: str,  # noqa: A002 - schema field name
    title: str,
    urls: list[str],
    observed: str,
    expected: str,
    check: str | None = None,
    affected_pages: int = 0,
    effort: str = "S",
    fix_tier: int | None = None,
    fix_channel: str | None = None,
    detected_at: str | None = None,
) -> dict:
    if type not in FINDING_TYPES:
        raise ValueError(f"finding type must be one of {FINDING_TYPES}: {type}")
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}: {effort}")
    if fix_channel is not None and fix_channel not in FIX_CHANNELS:
        raise ValueError(f"fix_channel must be one of {FIX_CHANNELS} or None: {fix_channel}")
    if not urls or not observed:
        raise ValueError("every finding needs at least one URL and an observed value")
    return {
        "id": ids.next(dimension),
        "tenant": tenant,
        "subdomain": subdomain,
        "template": template,
        "dimension": dimension,
        "check": check,
        "type": type,
        "title": title,
        "evidence": {"urls": list(urls), "observed": observed, "expected": expected},
        "affected_pages": affected_pages,
        "impact": {"clicks_28d": 0, "impressions_28d": 0, "score": 0},  # filled in by the Prioritizer
        "effort": effort,
        "fix_tier": fix_tier,
        "fix_channel": fix_channel,
        "status": "open",
        "detected_at": detected_at or date.today().isoformat(),
    }


# ---- store ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, source TEXT, tenant TEXT, subdomain TEXT, template TEXT,
  dimension TEXT, check_name TEXT, type TEXT, title TEXT, evidence TEXT, affected_pages INTEGER, impact TEXT,
  effort TEXT, fix_tier INTEGER, fix_channel TEXT, status TEXT, verification TEXT, superseded_by TEXT,
  detected_at TEXT, first_seen_run TEXT, last_seen_run TEXT, resolved_run TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (ref TEXT PRIMARY KEY, source TEXT, imported_at TEXT, scope TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS observations (
  key TEXT, run_ref TEXT, affected_pages INTEGER, observed TEXT, PRIMARY KEY (key, run_ref)
);
CREATE TABLE IF NOT EXISTS events (key TEXT, run_ref TEXT, event TEXT, PRIMARY KEY (key, run_ref));
CREATE TABLE IF NOT EXISTS notes (key TEXT, note TEXT, source TEXT, PRIMARY KEY (key, source));
"""

_JSON_COLS = ("evidence", "impact", "verification")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FindingsStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- writes ----

    def _new_id(self, dimension: str) -> str:
        rows = self.db.execute("SELECT id FROM findings WHERE id LIKE ?", (f"{dimension}-%",)).fetchall()
        nums = [int(r["id"].rsplit("-", 1)[1]) for r in rows if r["id"].rsplit("-", 1)[1].isdigit()]
        return f"{dimension}-{max(nums, default=0) + 1:04d}"

    def _start_run(self, ref: str, source: str, scope: dict) -> None:
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM runs").fetchone()[0]
        self.db.execute(
            "INSERT OR REPLACE INTO runs (ref, source, imported_at, scope, seq) VALUES (?, ?, ?, ?, ?)",
            (ref, source, _now(), json.dumps(scope), seq),
        )

    def _upsert(self, f: dict, source: str, run_ref: str, status: str, finding_id: str | None = None) -> str:
        key = finding_key(f)
        row = self.db.execute("SELECT id, status FROM findings WHERE key = ?", (key,)).fetchone()
        values = {
            "source": source, "tenant": f["tenant"], "subdomain": f["subdomain"], "template": f["template"],
            "dimension": f["dimension"], "check_name": f.get("check"), "type": f["type"], "title": f["title"],
            "evidence": json.dumps(f["evidence"]), "affected_pages": f.get("affected_pages", 0),
            "impact": json.dumps(f.get("impact") or {}), "effort": f.get("effort"), "fix_tier": f.get("fix_tier"),
            "fix_channel": f.get("fix_channel"), "status": status,
            "verification": json.dumps(f.get("verification")) if f.get("verification") else None,
            "superseded_by": f.get("superseded_by"), "last_seen_run": run_ref,
            "resolved_run": None, "resolved_at": None,
        }
        if row is None:
            fid = finding_id or self._new_id(f["dimension"])
            values.update(id=fid, key=key, detected_at=f.get("detected_at") or date.today().isoformat(),
                          first_seen_run=run_ref)
            cols = ", ".join(values)
            self.db.execute(f"INSERT INTO findings ({cols}) VALUES ({', '.join('?' * len(values))})",
                            list(values.values()))
        else:
            fid = row["id"]
            if not (f.get("impact") or {}).get("source"):
                del values["impact"]  # keep the Prioritizer's impact; it is refreshed by `set_impacts`
            sets = ", ".join(f"{c} = ?" for c in values)
            self.db.execute(f"UPDATE findings SET {sets} WHERE key = ?", [*values.values(), key])
        self.db.execute(
            "INSERT OR REPLACE INTO observations (key, run_ref, affected_pages, observed) VALUES (?, ?, ?, ?)",
            (key, run_ref, f.get("affected_pages", 0), f["evidence"]["observed"]),
        )
        return fid

    def set_impacts(self, impacts: dict[str, dict]) -> int:
        """Store the Prioritizer's impact per finding id. Returns how many findings were updated."""
        n = 0
        for fid, impact in impacts.items():
            n += self.db.execute("UPDATE findings SET impact = ? WHERE id = ?", (json.dumps(impact), fid)).rowcount
        self.db.commit()
        return n

    def _event(self, key: str, run_ref: str, event: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO events (key, run_ref, event) VALUES (?, ?, ?)", (key, run_ref, event))

    def import_run(self, findings: list[dict], run_ref: str, source: str, hosts: list[str],
                   sections: list[str] | None = None, resolve_unsectioned: bool = False) -> dict:
        """Upsert one run's findings; resolve earlier findings of this source that the run covered but no
        longer reports. With a section filter, only section-scoped findings in those sections can resolve,
        plus findings without a section if `resolve_unsectioned` (the run checked those in full)."""
        self._start_run(run_ref, source, {"hosts": hosts, "sections": sections})
        seen, new, reopened = set(), [], []
        for f in findings:
            key = finding_key(f)
            before = self.db.execute("SELECT status FROM findings WHERE key = ?", (key,)).fetchone()
            fid = self._upsert(f, source, run_ref, "open")
            seen.add(key)
            if before is None:
                new.append(fid)
                self._event(key, run_ref, "new")
            elif before["status"] == "resolved":
                reopened.append(fid)
                self._event(key, run_ref, "reopened")

        resolved = []
        rows = self.db.execute(
            "SELECT id, key, subdomain, template FROM findings WHERE source = ? AND status = 'open'", (source,)
        ).fetchall()
        for r in rows:
            if r["key"] in seen or r["subdomain"] not in hosts:
                continue
            section = template_section(r["template"])
            if sections is not None and section not in sections and not (resolve_unsectioned and not section):
                continue
            self.db.execute("UPDATE findings SET status = 'resolved', resolved_run = ?, resolved_at = ? WHERE key = ?",
                            (run_ref, _now(), r["key"]))
            self._event(r["key"], run_ref, "resolved")
            resolved.append(r["id"])
        self.db.commit()
        return {"run": run_ref, "findings": len(findings), "new": new, "reopened": reopened, "resolved": resolved}

    def import_seed(self, doc: dict, run_ref: str) -> dict:
        """Load manual findings (tenants/<tenant>.seed-findings.yaml). The file is the source of truth for their
        status. Seeds keep their own ids. Notes attach diagnosis text to other findings' keys."""
        self._start_run(run_ref, "seed", {})
        keys = set()
        for f in doc.get("findings") or []:
            status = "superseded" if f.get("superseded_by") else f["status"]
            self._upsert(f, "seed", run_ref, status, finding_id=f["id"])
            keys.add(finding_key(f))
        # a seed removed from the file is no longer tracked as open
        for r in self.db.execute("SELECT key FROM findings WHERE source = 'seed' AND status != 'resolved'"):
            if r["key"] not in keys:
                self.db.execute("UPDATE findings SET status = 'resolved', resolved_run = ?, resolved_at = ? "
                                "WHERE key = ?", (run_ref, _now(), r["key"]))
        self.db.execute("DELETE FROM notes WHERE source = 'seed'")
        for n in doc.get("notes") or []:
            self.db.execute("INSERT OR REPLACE INTO notes (key, note, source) VALUES (?, ?, 'seed')",
                            (n["applies_to"], n["note"]))
        self.db.commit()
        return {"run": run_ref, "findings": len(keys), "notes": len(doc.get("notes") or [])}

    # ---- reads ----

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for c in _JSON_COLS:
            d[c] = json.loads(d[c]) if d[c] else None
        d["check"] = d.pop("check_name")
        return d

    def findings(self, statuses=("open", "unverified")) -> list[dict]:
        marks = ", ".join("?" * len(statuses))
        rows = self.db.execute(f"SELECT * FROM findings WHERE status IN ({marks}) ORDER BY id", list(statuses))
        notes: dict = {}
        for n in self.db.execute("SELECT key, note FROM notes"):
            notes.setdefault(n["key"], []).append(n["note"])
        out = []
        for r in rows:
            d = self._row(r)
            d["notes"] = notes.get(d["key"], [])
            out.append(d)
        return out

    def latest_run(self, source: str) -> str | None:
        r = self.db.execute("SELECT ref FROM runs WHERE source = ? ORDER BY seq DESC LIMIT 1", (source,)).fetchone()
        return r["ref"] if r else None

    def changes(self, run_ref: str) -> dict:
        """What a run changed: ids first seen or reopened, and findings it resolved."""
        sql = "SELECT f.* FROM events e JOIN findings f ON f.key = e.key WHERE e.run_ref = ? AND e.event = ?"
        new = [r["id"] for r in self.db.execute(sql, (run_ref, "new"))]
        new += [r["id"] for r in self.db.execute(sql, (run_ref, "reopened"))]
        resolved = [self._row(r) for r in self.db.execute(sql, (run_ref, "resolved"))]
        first = self.db.execute("SELECT seq FROM runs WHERE ref = ?", (run_ref,)).fetchone()
        earlier = self.db.execute("SELECT COUNT(*) FROM runs WHERE source = (SELECT source FROM runs WHERE ref = ?) "
                                  "AND seq < ?", (run_ref, first["seq"] if first else 0)).fetchone()[0]
        return {"run": run_ref, "new": new if earlier else [], "resolved": resolved, "first_run": not earlier}
