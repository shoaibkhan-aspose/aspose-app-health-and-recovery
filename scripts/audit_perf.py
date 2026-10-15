"""Speed audit: Core Web Vitals (real users) and a Lighthouse lab run per sampled page, via PageSpeed Insights.

Run on the Mac after a crawl audit and prioritize.py (it reads data/audit.sqlite and the traffic cache):

    python scripts/audit_perf.py --plan                    # list the pages it would measure; no API calls
    python scripts/audit_perf.py --quiet                   # all hosts, 2 pages per template, mobile
    python scripts/audit_perf.py --sections pdf --max-pages 10   # smoke test (resolves nothing)

Auth: the service account from GOOGLE_APPLICATION_CREDENTIALS (PageSpeed Insights API enabled in its Cloud
project), or PAGESPEED_API_KEY. Google loads each page once from its own servers; nothing is written anywhere.

Writes the report to reports/perf-audit-<date>-run<id>.json and the findings to the findings store (exported to
reports/findings.json). Run prioritize.py afterwards to score the new findings.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.audit.perf import audit, fmt, import_report, select_pages  # noqa: E402
from core.audit.store import AuditStore  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.pagespeed import PageSpeedClient, PageSpeedError, make_session  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.findings import DEFAULT_STORE, export, import_seed, print_changes  # noqa: E402


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_summary(report: dict) -> None:
    print(f"\n== Hosts ({len(report['pages'])} pages, {report['requests']} API calls, {report['duration_s']}s) ==")
    for host, h in report["summary"].items():
        print(f"  {host:28} pages={h['pages']:<4} errors={h['errors']:<3} not loaded={h['not_loaded']:<3} "
              f"real-user data={h['field']:<4} "
              f"median lab score={h['median_score']}")
    print("\n== Whole host, real users (p75) ==")
    for host, o in report["origins"].items():
        print(f"  {host:28} " + "  ".join(f"{k.upper()} {fmt(k, v)}" for k, v in o["metrics"].items()))
    findings = report["findings"]
    print(f"\n== Findings ({len(findings)}) ==")
    for f in findings:
        print(f"  {f['id']:<12} {f['type']:<13} {f['subdomain']:<24} {f['title']}  "
              f"[est. pages={f['affected_pages']:,}]")
    if report["stopped_early"]:
        print(f"\nStopped at --max-pages ({len(report['pages'])} of {report['pages_planned']} planned).")
    if report["incomplete"]:
        print("\nToo many API errors: findings are recorded but nothing is marked resolved.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    ap.add_argument("--hosts", help="comma-separated hosts (default: all tenant hosts)")
    ap.add_argument("--sections", help="comma-separated sections (narrows sectioned hosts only)")
    ap.add_argument("--per-template", type=int, default=2, help="pages per template (most search clicks first)")
    ap.add_argument("--strategy", choices=("mobile", "desktop"), default="mobile")
    ap.add_argument("--workers", type=int, default=4, help="PageSpeed calls in parallel")
    ap.add_argument("--max-pages", type=int, help="measure at most this many pages (findings then resolve nothing)")
    ap.add_argument("--plan", action="store_true", help="only list the pages that would be measured")
    ap.add_argument("--traffic", help="cached traffic JSON from prioritize.py (default: newest in data/)")
    ap.add_argument("--db", default=str(REPO_ROOT / "data" / "audit.sqlite"))
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    ap.add_argument("--findings-store", default=str(DEFAULT_STORE), help="'' to skip the findings store")
    ap.add_argument("--quiet", action="store_true", help="no per-page progress lines")
    args = ap.parse_args(argv)

    tenant = load_tenant(args.tenant)
    hosts = _csv(args.hosts)
    unknown = [h for h in hosts or [] if h not in tenant.host_names]
    if unknown:
        print(f"Not tenant hosts (or excluded): {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not Path(args.db).is_file():
        print(f"No crawl audit at {args.db}; run scripts/audit_crawl.py first", file=sys.stderr)
        return 2
    traffic = args.traffic
    if traffic is None:
        cached = sorted((REPO_ROOT / "data").glob(f"traffic-{tenant.property}-*.json"))
        traffic = str(cached[-1]) if cached else ""
    search_rows = []
    if traffic:
        data = json.loads(Path(traffic).read_text(encoding="utf-8"))
        search_rows = [r for rows in data.get("gsc", {}).values() for r in rows]
    else:
        print("No traffic cache: pages are picked without search clicks (run prioritize.py first)", file=sys.stderr)

    audit_db = sqlite3.connect(args.db)
    if args.plan:
        targets = select_pages(audit_db, tenant, search_rows, hosts or tenant.host_names, _csv(args.sections),
                               args.per_template)
        for t in targets:
            print(f"  {t.host:24} {t.section or '-':10} {t.template:28} clicks={t.clicks:>8,.0f} [{t.source}] {t.url}")
        groups = {(t.host, t.section, t.template) for t in targets}
        print(f"\n{len(targets)} pages in {len(groups)} template groups on {len({t.host for t in targets})} hosts"
              f" (~{len(targets) * 25 / max(1, args.workers) / 60:.0f} min at {args.workers} in parallel)")
        return 0

    try:
        client = PageSpeedClient(make_session(), strategy=args.strategy)
        report = audit(tenant, client, audit_db, AuditStore(args.db), search_rows=search_rows, hosts=hosts,
                       sections=_csv(args.sections), per_template=args.per_template, workers=args.workers,
                       max_pages=args.max_pages, log=None if args.quiet else print)
    except PageSpeedError as e:
        print(f"PageSpeed Insights: {e}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"perf-audit-{date.today().isoformat()}-run{report['run_id']}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print_summary(report)
    print(f"\nPages: {args.db} (run {report['run_id']})\nFull report: {out_file}")
    if args.findings_store:
        fstore = FindingsStore(args.findings_store)
        import_seed(fstore, tenant.property)
        print(f"\nFindings store: {args.findings_store}")
        print_changes(import_report(fstore, report))
        print(f"Findings export: {export(fstore)}  (run prioritize.py to score them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
