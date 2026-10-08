"""Render a Discovery run (reports/discovery-*.json) as one self-contained HTML page for sharing.

    python scripts/build_report.py                                   # newest discovery report
    python scripts/build_report.py reports/discovery-2026-10-08-run6.json --notes reports/discovery-notes.txt

The page embeds the run's data as JSON and renders it in the browser (no external requests except
Google Fonts). Findings come from the findings store (open + unverified, discovery and manual review,
with notes and what is new or resolved in this run); without a store, from the run JSON.
Optional --notes: a plain-text file, one key takeaway per line, shown at the top.
Output: reports/<input name>.html (gitignored). Publishing is a separate, manual step.
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


def build(report: dict, notes: list[str], run_date: str | None) -> str:
    data = dict(report, _notes=notes, _run_date=run_date)
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.replace("__TITLE__", f"{report['tenant']} Discovery").replace("__DATA__", payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", nargs="?", help="discovery JSON (default: newest in reports/)")
    ap.add_argument("--notes", help="text file, one key takeaway per line")
    ap.add_argument("--out", help="output HTML path")
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
    notes = []
    if args.notes:
        notes = [ln.strip().lstrip("-• ").strip() for ln in Path(args.notes).read_text(encoding="utf-8").splitlines()]
        notes = [n for n in notes if n]
    if Path(args.store).is_file():
        store = FindingsStore(args.store)
        run_ref = f"discovery-run{report['run_id']}"
        if not store.db.execute("SELECT 1 FROM runs WHERE ref = ?", (run_ref,)).fetchone():
            print(f"{run_ref} is not in the findings store; run scripts/findings.py import-discovery {src}",
                  file=sys.stderr)
            return 1
        report["findings"] = store.findings()
        report["_changes"] = store.changes(run_ref)
    else:
        print(f"No findings store at {args.store}; using the run's own findings.", file=sys.stderr)
    run_date = src.stem.split("-run")[0].removeprefix("discovery-") if "-run" in src.stem else None

    out = Path(args.out) if args.out else src.with_suffix(".html")
    out.write_text(build(report, notes, run_date), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB) from {src.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
