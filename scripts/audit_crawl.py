"""Crawl audit: fetch a sample of pages per template and check status, redirects, canonical, hreflang, head tags.

Run on the Mac after a discovery run (needs network; no Google credentials):

    python scripts/audit_crawl.py                                              # all hosts, latest full inventory
    python scripts/audit_crawl.py --hosts products.aspose.app --sections pdf   # sample
    python scripts/audit_crawl.py --max-pages 20                               # smoke test (resolves nothing)

Read-only and polite: GET only, robots.txt respected, one request per host per --interval seconds,
identifiable user agent, excluded (QA) hosts never touched. Hosts are fetched round-robin.

Writes pages to data/audit.sqlite, the report to reports/crawl-audit-<date>-run<id>.json, and the findings
to the findings store (data/findings.sqlite, exported to reports/findings.json).
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

from core.audit.crawl import audit, import_report, latest_inventory_run  # noqa: E402
from core.audit.store import AuditStore  # noqa: E402
from core.discovery.fetch import DEFAULT_USER_AGENT, PoliteFetcher  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.findings import DEFAULT_STORE, export, import_seed, print_changes  # noqa: E402

MAX_PAGE_BYTES = 5 * 1024 * 1024


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_summary(report: dict) -> None:
    print(f"\n== Hosts ({len(report['pages'])} pages, {report['requests']} requests, {report['duration_s']}s) ==")
    for host, h in report["summary"].items():
        print(f"  {host:28} pages={h['pages']:<4} html={h['ok_html']:<4} errors={h['errors']:<3} "
              f"redirected={h['redirected']:<3} blocked={h['blocked']}")
    findings = report["findings"]
    print(f"\n== Findings ({len(findings)}) ==")
    for f in findings:
        print(f"  {f['id']:<12} {f['type']:<13} {f['subdomain']:<24} {f['title']}  "
              f"[est. pages={f['affected_pages']:,}]")
    if report["stopped_early"]:
        print(f"\nStopped at --max-pages ({len(report['pages'])} of {report['pages_planned']} planned).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    ap.add_argument("--hosts", help="comma-separated hosts (default: all tenant hosts)")
    ap.add_argument("--sections", help="comma-separated sections (e.g. product families)")
    ap.add_argument("--inventory-run", type=int, help="discovery run to sample (default: latest full run)")
    ap.add_argument("--per-template", type=int, default=2, help="default-language pages per template")
    ap.add_argument("--other-langs", type=int, default=2, help="other languages per template (1 page each)")
    ap.add_argument("--max-per-host", type=int, help="cap on sampled pages per host")
    ap.add_argument("--max-pages", type=int, help="stop after this many pages (findings then resolve nothing)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between requests to one host")
    ap.add_argument("--user-agent", default=os.environ.get("AUDIT_USER_AGENT", DEFAULT_USER_AGENT))
    ap.add_argument("--inventory", default=str(REPO_ROOT / "data" / "inventory.sqlite"))
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
    if not Path(args.inventory).is_file():
        print(f"No inventory at {args.inventory}; run scripts/discover.py first", file=sys.stderr)
        return 2
    inventory = sqlite3.connect(args.inventory)
    run = args.inventory_run or latest_inventory_run(inventory)
    if run is None:
        print("No finished full discovery run in the inventory", file=sys.stderr)
        return 2

    fetcher = PoliteFetcher(user_agent=args.user_agent, min_interval=args.interval, max_bytes=MAX_PAGE_BYTES)
    report = audit(tenant, fetcher, inventory, run, AuditStore(args.db), hosts=hosts, sections=_csv(args.sections),
                   per_template=args.per_template, other_langs=args.other_langs, max_per_host=args.max_per_host,
                   max_pages=args.max_pages, log=None if args.quiet else print)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"crawl-audit-{date.today().isoformat()}-run{report['run_id']}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    print_summary(report)
    print(f"\nInventory run {run}; pages: {args.db} (run {report['run_id']})\nFull report: {out_file}")
    if args.findings_store:
        fstore = FindingsStore(args.findings_store)
        import_seed(fstore, tenant.property)
        print(f"\nFindings store: {args.findings_store}")
        print_changes(import_report(fstore, report))
        print(f"Findings export: {export(fstore)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
