"""AI-readiness audit, host level: AI crawler access (robots.txt, firewall/CDN) and llms.txt per host and section.

Run on the Mac (needs network; no Google credentials). About 2 minutes for all hosts:

    python scripts/audit_ai.py                           # all tenant hosts
    python scripts/audit_ai.py --hosts about.aspose.app  # one host

Page-level AI checks (text without JavaScript, structured data) run in scripts/audit_crawl.py.

Read-only and polite: GET only, robots.txt respected, one request per host per --interval seconds. The firewall
probe fetches each homepage once per AI crawler user agent, with the audit's name appended to stay identifiable.

Writes the report to reports/ai-audit-<date>-run<id>.json and the findings to the findings store (exported to
reports/findings.json). Run prioritize.py afterwards to score the new findings.
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

from core.audit.ai import audit, import_report  # noqa: E402
from core.audit.store import AuditStore  # noqa: E402
from core.discovery.fetch import DEFAULT_USER_AGENT, PoliteFetcher  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.findings import DEFAULT_STORE, export, import_seed, print_changes  # noqa: E402

MAX_BYTES = 5 * 1024 * 1024


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_summary(report: dict) -> None:
    print(f"\n== Hosts ({report['requests']} requests, {report['duration_s']}s) ==")
    for host, h in report["summary"].items():
        secs = f" sections covered={h['sections_covered']}/{h['sections']}" if h["sections"] else ""
        print(f"  {host:28} robots={h['robots']!s:<4} answer bots blocked={len(h['answer_blocked'])} "
              f"training bots blocked={len(h['training_blocked'])} firewall={len(h['firewall_blocked'])} "
              f"llms.txt={h['llms']['state']}{secs}")
    findings = report["findings"]
    print(f"\n== Findings ({len(findings)}) ==")
    for f in findings:
        print(f"  {f['id']:<12} {f['type']:<13} {f['subdomain']:<24} {f['title']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    ap.add_argument("--hosts", help="comma-separated hosts (default: all tenant hosts)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between requests to one host")
    ap.add_argument("--user-agent", default=os.environ.get("AUDIT_USER_AGENT", DEFAULT_USER_AGENT))
    ap.add_argument("--db", default=str(REPO_ROOT / "data" / "audit.sqlite"))
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    ap.add_argument("--findings-store", default=str(DEFAULT_STORE), help="'' to skip the findings store")
    ap.add_argument("--quiet", action="store_true", help="no per-host progress lines")
    args = ap.parse_args(argv)

    tenant = load_tenant(args.tenant)
    hosts = _csv(args.hosts)
    unknown = [h for h in hosts or [] if h not in tenant.host_names]
    if unknown:
        print(f"Not tenant hosts (or excluded): {', '.join(unknown)}", file=sys.stderr)
        return 2

    fetcher = PoliteFetcher(user_agent=args.user_agent, min_interval=args.interval, max_bytes=MAX_BYTES)
    report = audit(tenant, fetcher, AuditStore(args.db), hosts=hosts, log=None if args.quiet else print)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"ai-audit-{date.today().isoformat()}-run{report['run_id']}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print_summary(report)
    print(f"\nFull report: {out_file}")
    if args.findings_store:
        fstore = FindingsStore(args.findings_store)
        import_seed(fstore, tenant.property)
        print(f"\nFindings store: {args.findings_store}")
        print_changes(import_report(fstore, report))
        print(f"Findings export: {export(fstore)}  (run prioritize.py to score them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
