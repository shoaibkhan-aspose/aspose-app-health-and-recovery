"""Discovery (inventory) for a tenant: robots.txt and sitemaps per host -> SQLite inventory + findings.

Run on the Mac (needs network; no Google credentials):

    python scripts/discover.py --tenant aspose.app                                   # everything
    python scripts/discover.py --hosts products.aspose.app --sections pdf,words      # sample

Read-only and polite: GET requests only, robots.txt respected, one request per host per
--interval seconds, identifiable user agent, excluded (QA) hosts never touched.

Writes the inventory to data/inventory.sqlite (one run per invocation) and the summary plus
findings to reports/discovery-<date>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.discovery.fetch import DEFAULT_USER_AGENT, PoliteFetcher  # noqa: E402
from core.discovery.run import discover, import_report  # noqa: E402
from core.discovery.store import InventoryStore  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.findings import DEFAULT_STORE, import_seed, print_changes  # noqa: E402


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_summary(report: dict) -> None:
    print(f"\n== Hosts ({report['sitemaps_fetched']} sitemaps, {report['requests']} requests, "
          f"{report['duration_s']}s) ==")
    for host, h in report["hosts"].items():
        rb = h["robots"]
        robots = f"robots={rb['status'] or rb['error']}"
        if rb["sitemaps"]:
            robots += f" sitemap-lines={len(rb['sitemaps'])}"
        print(f"  {host:28} {robots:28} sitemaps ok={h['sitemaps_ok']:<4} failed={len(h['sitemaps_failed']):<3} "
              f"urls={h['urls']:,}")

    print("\n== Sections (URLs, languages, % with hreflang) ==")
    for host, sections in report["sections"].items():
        rows = sorted(sections.items(), key=lambda kv: -kv[1]["urls"])
        shown = rows[:40]
        for section, s in shown:
            pct = 100 * s["hreflang_urls"] / s["urls"] if s["urls"] else 0
            print(f"  {host:28} /{section:<14} urls={s['urls']:>8,}  langs={len(s['langs']):>3}  hreflang={pct:5.1f}%")
        if len(rows) > len(shown):
            print(f"  {host:28} ... {len(rows) - len(shown)} more sections")

    if report["gsc_comparison"]:
        print("\n== GSC sitemaps snapshot vs live ==")
        for g in report["gsc_comparison"]:
            print(f"  {g['path']:42} GSC {g['gsc_status']:<15} {g['gsc_discovered'] or 0:>8,}   "
                  f"live {str(g['live_status']):<6} {g['live_urls']:>8,}")

    if report["new_hosts"]:
        print("\n== Hosts found in sitemaps but not in the tenant config ==")
        for host, n in report["new_hosts"].items():
            print(f"  {host}  ({n:,} URLs)")

    findings = report["findings"]
    print(f"\n== Findings ({len(findings)}) ==")
    for f in findings:
        print(f"  {f['id']:<10} {f['type']:<13} {f['subdomain']:<24} {f['title']}  [pages={f['affected_pages']:,}]")
    if report["not_fetched"]:
        print(f"\n{len(report['not_fetched'])} sitemaps queued but not fetched (--max-sitemaps reached).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    ap.add_argument("--hosts", help="comma-separated hosts to include (default: all tenant hosts)")
    ap.add_argument("--sections", help="comma-separated sections (e.g. product families) to probe")
    ap.add_argument("--max-sitemaps", type=int, help="stop after fetching this many sitemaps")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between requests to one host")
    ap.add_argument("--user-agent", default=os.environ.get("AUDIT_USER_AGENT", DEFAULT_USER_AGENT))
    ap.add_argument("--db", default=str(REPO_ROOT / "data" / "inventory.sqlite"))
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    ap.add_argument("--findings-store", default=str(DEFAULT_STORE), help="'' to skip the findings store")
    ap.add_argument("--quiet", action="store_true", help="no per-sitemap progress lines")
    args = ap.parse_args(argv)

    tenant = load_tenant(args.tenant)
    hosts = _csv(args.hosts)
    unknown = [h for h in hosts or [] if h not in tenant.host_names]
    if unknown:
        print(f"Not tenant hosts (or excluded): {', '.join(unknown)}", file=sys.stderr)
        return 2

    fetcher = PoliteFetcher(user_agent=args.user_agent, min_interval=args.interval)
    store = InventoryStore(args.db)
    report = discover(tenant, fetcher, store, hosts=hosts, sections=_csv(args.sections),
                      max_sitemaps=args.max_sitemaps, log=None if args.quiet else print)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"discovery-{date.today().isoformat()}-run{report['run_id']}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print_summary(report)
    print(f"\nInventory: {args.db} (run {report['run_id']})\nFull report: {out_file}")
    if args.findings_store:
        fstore = FindingsStore(args.findings_store)
        import_seed(fstore, tenant.property)
        print(f"\nFindings store: {args.findings_store}")
        print_changes(import_report(fstore, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
