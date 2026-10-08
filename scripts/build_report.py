"""Render the site health report (discovery run + latest crawl audit) as one self-contained HTML page.

    python scripts/build_report.py                                   # newest discovery report + newest crawl audit
    python scripts/build_report.py reports/discovery-2026-10-08-run7.json --notes reports/discovery-notes.txt \
        --audit-notes reports/crawl-audit-notes.txt --out reports/discovery-latest.html

The page embeds the run's data as JSON and renders it in the browser (no external requests except
Google Fonts). Findings come from the findings store (open + unverified, discovery and manual review,
with notes and what is new or resolved in this run); without a store, from the run JSON.
Optional --notes / --audit-notes: plain-text files, one key takeaway per line, shown at the top and in the
crawl audit section. --audit '' leaves the crawl audit out.
Output: reports/<input name>.html, or --out. GitHub Pages serves docs/index.html (built with --standalone).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.findings import FindingsStore  # noqa: E402

TEMPLATE = REPO_ROOT / "scripts" / "templates" / "discovery_report.html"


# For hosting the file on its own (GitHub Pages): the artifact host adds this skeleton itself.
STANDALONE_HEAD = (
    '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="robots" content="noindex, nofollow">\n'
)


def build(report: dict, notes: list[str], run_date: str | None, standalone: bool = False) -> str:
    data = dict(report, _notes=notes, _run_date=run_date)
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    page = template.replace("__TITLE__", f"{report['tenant']} Site Health").replace("__DATA__", payload)
    return STANDALONE_HEAD + page if standalone else page


def read_notes(path: str | None) -> list[str]:
    if not path:
        return []
    lines = [ln.strip().lstrip("-• ").strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()]
    return [n for n in lines if n]


def run_date_of(path: Path, prefix: str) -> str | None:
    return path.stem.split("-run")[0].removeprefix(prefix) if "-run" in path.stem else None


def audit_summary(path: Path, notes: list[str]) -> dict:
    """The parts of a crawl-audit report the page shows (not the per-page facts)."""
    a = json.loads(path.read_text(encoding="utf-8"))
    sections: dict = {}
    for p in a["pages"]:
        if p["section"]:
            sections.setdefault(p["host"], set()).add(p["section"])
    return {"run_id": a["run_id"], "date": run_date_of(path, "crawl-audit-"), "params": a["params"],
            "summary": a["summary"], "pages": len(a["pages"]), "requests": a["requests"],
            "duration_s": a["duration_s"], "stopped_early": a["stopped_early"],
            "sections": {h: sorted(v) for h, v in sections.items()}, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", nargs="?", help="discovery JSON (default: newest in reports/)")
    ap.add_argument("--notes", help="text file, one key takeaway per line")
    ap.add_argument("--audit", help="crawl-audit JSON (default: newest in reports/; '' to leave out)")
    ap.add_argument("--audit-notes", help="text file, one crawl-audit takeaway per line")
    ap.add_argument("--out", help="output HTML path")
    ap.add_argument("--standalone", action="store_true",
                    help="add doctype, charset, viewport and noindex (for GitHub Pages: --out docs/index.html)")
    ap.add_argument("--store", default=str(REPO_ROOT / "data" / "findings.sqlite"), help="findings store")
    args = ap.parse_args(argv)

    if args.report:
        src = Path(args.report)
    else:
        candidates = sorted((REPO_ROOT / "reports").glob("discovery-*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print("No reports/discovery-*.json found. Run scripts/discover.py first.", file=sys.stderr)
            return 1
        src = candidates[-1]

    report = json.loads(src.read_text(encoding="utf-8"))
    notes = read_notes(args.notes)
    audit_src = None
    if args.audit:
        audit_src = Path(args.audit)
    elif args.audit is None:
        found = sorted((REPO_ROOT / "reports").glob("crawl-audit-*-run*.json"), key=lambda p: p.stat().st_mtime)
        audit_src = found[-1] if found else None
    if audit_src:
        report["_audit"] = audit_summary(audit_src, read_notes(args.audit_notes))
    if Path(args.store).is_file():
        store = FindingsStore(args.store)
        run_ref = f"discovery-run{report['run_id']}"
        if not store.db.execute("SELECT 1 FROM runs WHERE ref = ?", (run_ref,)).fetchone():
            print(f"{run_ref} is not in the findings store; run scripts/findings.py import-discovery {src}",
                  file=sys.stderr)
            return 1
        report["findings"] = store.findings()
        report["_changes"] = store.changes(run_ref)
        if not audit_src:
            report["findings"] = [f for f in report["findings"] if f["source"] != "crawl-audit"]
    else:
        print(f"No findings store at {args.store}; using the run's own findings.", file=sys.stderr)
    run_date = run_date_of(src, "discovery-")

    out = Path(args.out) if args.out else src.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(report, notes, run_date, args.standalone), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB) from {src.name}"
          + (f" and {audit_src.name}" if audit_src else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
