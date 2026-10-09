"""Render the site health report (discovery run + latest crawl audit) as one self-contained HTML page.

    python scripts/build_report.py                                   # newest discovery report + newest crawl audit
    python scripts/build_report.py --findings reports/findings.json  # without the store (GitHub Actions)
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
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.findings import FindingsStore  # noqa: E402
from core.scorecard import family_of, family_scorecards  # noqa: E402

TEMPLATE = REPO_ROOT / "scripts" / "templates" / "discovery_report.html"
TRAFFIC_SUMMARY = REPO_ROOT / "reports" / "traffic-summary.json"


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


def runs_by_name(pattern: str) -> list[Path]:
    """Report files ordered by the date and run number in their names (file times are meaningless after a
    git checkout)."""
    def key(p: Path):
        m = re.search(r"(\d{4}-\d{2}-\d{2})-run(\d+)", p.name)
        return (m.group(1), int(m.group(2))) if m else ("", 0)
    return sorted((REPO_ROOT / "reports").glob(pattern), key=key)


def newest(pattern: str) -> Path | None:
    found = runs_by_name(pattern)
    return found[-1] if found else None


def from_export(rows: list[dict], run_ref: str) -> tuple[list[dict], dict]:
    """Open findings and what `run_ref` changed, from reports/findings.json (for CI, where the store is absent).
    Same shape as FindingsStore.findings() and .changes()."""
    findings = sorted((f for f in rows if f["status"] in ("open", "unverified")), key=lambda f: f["id"])
    def seq(ref: str | None) -> int:  # discovery-run7 -> 7
        m = re.search(r"(\d+)$", ref or "")
        return int(m.group(1)) if m else 0

    prefix = run_ref.rstrip("0123456789")
    first_run = not any((f.get("first_seen_run") or "").startswith(prefix) and seq(f["first_seen_run"]) < seq(run_ref)
                        for f in rows)
    new = [f["id"] for f in findings if f.get("first_seen_run") == run_ref]
    resolved = [f for f in rows if f["status"] == "resolved" and f.get("resolved_run") == run_ref]
    return findings, {"run": run_ref, "new": [] if first_run else new, "resolved": resolved, "first_run": first_run}


def audit_summary(paths: list[Path], notes: list[str]) -> dict:
    """Merge crawl-audit runs (oldest first): the newest result wins for a page checked more than once, so
    section-scoped batches add up to one picture. Only what the page shows (not per-page facts)."""
    from core.audit.crawl import summarize_pages

    pages: dict = {}
    runs = []
    for path in paths:
        a = json.loads(path.read_text(encoding="utf-8"))
        runs.append({"run_id": a["run_id"], "date": run_date_of(path, "crawl-audit-"), "pages": len(a["pages"]),
                     "sections": a["params"].get("sections"), "requests": a["requests"],
                     "duration_s": a["duration_s"], "stopped_early": a["stopped_early"]})
        for p in a["pages"]:
            pages[p["url"]] = p
    last = json.loads(paths[-1].read_text(encoding="utf-8"))
    sections: dict = {}
    for p in pages.values():
        if p["section"]:
            sections.setdefault(p["host"], set()).add(p["section"])
    return {"run_id": runs[-1]["run_id"], "date": runs[-1]["date"], "runs": runs, "params": last["params"],
            "summary": summarize_pages(list(pages.values())), "pages": len(pages),
            "requests": sum(r["requests"] for r in runs), "duration_s": sum(r["duration_s"] for r in runs),
            "stopped_early": any(r["stopped_early"] for r in runs),
            "sections": {h: sorted(v) for h, v in sections.items()}, "notes": notes}


def add_families(report: dict, summary_path: Path = TRAFFIC_SUMMARY) -> None:
    """Family scorecards per sectioned host (from reports/traffic-summary.json, written by prioritize.py), and
    each finding's family for the report's Family filter."""
    if not summary_path.is_file():
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    report["_families"] = {"windows": summary["windows"], "hosts": {}}
    for host, traffic in summary["hosts"].items():
        families = list(traffic)
        report["_families"]["hosts"][host] = family_scorecards(
            report["findings"], host, families, (report.get("sections") or {}).get(host) or {}, traffic)
        for f in report["findings"]:
            fam = family_of(f, host, set(families))
            if fam:
                f["_family"] = fam


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", nargs="?", help="discovery JSON (default: newest in reports/)")
    ap.add_argument("--notes", help="text file, one key takeaway per line")
    ap.add_argument("--audit", help="comma-separated crawl-audit JSONs, merged oldest first "
                    "(default: every crawl-audit run in reports/; '' to leave out)")
    ap.add_argument("--audit-notes", help="text file, one crawl-audit takeaway per line")
    ap.add_argument("--out", help="output HTML path")
    ap.add_argument("--standalone", action="store_true",
                    help="add doctype, charset, viewport and noindex (for GitHub Pages: --out docs/index.html)")
    ap.add_argument("--store", default=str(REPO_ROOT / "data" / "findings.sqlite"), help="findings store")
    ap.add_argument("--findings", help="read findings from this export (reports/findings.json) instead of the store")
    args = ap.parse_args(argv)

    if args.report:
        src = Path(args.report)
    else:
        src = newest("discovery-*-run*.json")
        if src is None:
            print("No reports/discovery-*.json found. Run scripts/discover.py first.", file=sys.stderr)
            return 1

    report = json.loads(src.read_text(encoding="utf-8"))
    notes = read_notes(args.notes)
    audit_srcs: list[Path] = []
    if args.audit:
        audit_srcs = [Path(a) for a in args.audit.split(",")]
    elif args.audit is None:
        audit_srcs = runs_by_name("crawl-audit-*-run*.json")
    audit_src = audit_srcs[-1] if audit_srcs else None
    if audit_srcs:
        report["_audit"] = audit_summary(audit_srcs, read_notes(args.audit_notes))
    if args.findings:
        rows = json.loads(Path(args.findings).read_text(encoding="utf-8"))
        report["findings"], report["_changes"] = from_export(rows, f"discovery-run{report['run_id']}")
        if not audit_src:
            report["findings"] = [f for f in report["findings"] if f["source"] != "crawl-audit"]
    elif Path(args.store).is_file():
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
    add_families(report)
    run_date = run_date_of(src, "discovery-")

    out = Path(args.out) if args.out else src.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(report, notes, run_date, args.standalone), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB) from {src.name}"
          + (f" and {len(audit_srcs)} crawl-audit runs (newest {audit_src.name})" if audit_src else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
