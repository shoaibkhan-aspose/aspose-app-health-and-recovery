"""Read-only access check for a tenant's Google Search Console and GA4 data.

Run on the Mac (needs network + the service-account key):

    python scripts/check_access.py --tenant aspose.app

It answers:
  1. Which identity are we using?
  2. Which of the tenant's GSC properties can that identity read, at what permission?
  3. Per readable property: 28-day clicks/impressions, top pages, submitted sitemaps + errors.
  4. GA4: which hostnames the property tracks, total vs organic sessions per host.

Writes reports/access-check-<date>.json. Nothing is changed anywhere; only read scopes are used.
Exit code 0 = every check passed, 1 = at least one failed (details in the report).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.google_auth import CredentialsError, get_credentials  # noqa: E402
from core.gsc import default_window  # noqa: E402
from core.tenant import load_tenant  # noqa: E402

TOP_N = 5


def _err(exc: Exception) -> str:
    """Short, secret-free error description."""
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"


def check_gsc(gsc, tenant, start: str, end: str) -> dict:
    out: dict = {"window": [start, end], "properties": {}, "ok": True}
    try:
        visible = {s["siteUrl"]: s.get("permissionLevel") for s in gsc.list_sites()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _err(exc)}

    out["visible_to_identity"] = visible
    for site in tenant.gsc_properties:
        entry: dict = {"permission": visible.get(site)}
        if site not in visible:
            entry["error"] = "not visible to this identity (add the service account in GSC)"
            out["ok"] = False
            out["properties"][site] = entry
            continue
        try:
            totals = gsc.search_analytics(site, start, end)
            entry["clicks"] = totals[0]["clicks"] if totals else 0
            entry["impressions"] = totals[0]["impressions"] if totals else 0
            top = gsc.search_analytics(site, start, end, dimensions=["page"], row_limit=TOP_N)
            entry["top_pages"] = [
                {"page": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"]}
                for r in top
            ]
            sitemaps = gsc.list_sitemaps(site)
            entry["sitemaps"] = {
                "count": len(sitemaps),
                "with_errors": [
                    {"path": s.get("path"), "errors": s.get("errors"), "warnings": s.get("warnings")}
                    for s in sitemaps
                    if int(s.get("errors", 0) or 0) > 0
                ],
                "pending": [s.get("path") for s in sitemaps if s.get("isPending")],
            }
        except Exception as exc:  # noqa: BLE001
            entry["error"] = _err(exc)
            out["ok"] = False
        out["properties"][site] = entry
    return out


def check_ga4(ga4, tenant) -> dict:
    if not tenant.ga4_property_id:
        return {"ok": False, "error": "no ga4_property_id in tenant config"}
    try:
        rows = ga4.run_report(
            tenant.ga4_property_id,
            dimensions=["hostName", "sessionDefaultChannelGroup"],
            metrics=["sessions"],
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "property_id": tenant.ga4_property_id, "error": _err(exc)}

    per_host: dict = defaultdict(lambda: {"sessions": 0.0, "organic_sessions": 0.0})
    for r in rows:
        host = r["hostName"]
        per_host[host]["sessions"] += r["sessions"]
        if r["sessionDefaultChannelGroup"] == "Organic Search":
            per_host[host]["organic_sessions"] += r["sessions"]

    hosts = dict(sorted(per_host.items(), key=lambda kv: -kv[1]["sessions"]))
    tracked = set(hosts)
    return {
        "ok": True,
        "property_id": tenant.ga4_property_id,
        "window": ["28daysAgo", "yesterday"],
        "hosts": hosts,
        "tenant_hosts_not_seen": [h for h in tenant.host_names if h not in tracked],
        "excluded_hosts_seen": [h for h in tracked if tenant.is_excluded(h)],
    }


def print_summary(report: dict) -> None:
    print(f"\nIdentity: {report.get('identity', '?')}")
    gsc = report.get("gsc", {})
    print("\n== Search Console ==")
    if "error" in gsc:
        print(f"  FAILED: {gsc['error']}")
    for site, e in gsc.get("properties", {}).items():
        if "error" in e:
            print(f"  ✗ {site}  {e['error']}")
        else:
            sm = e.get("sitemaps", {})
            print(
                f"  ✓ {site}  [{e['permission']}]  clicks={e['clicks']:,.0f}  "
                f"impr={e['impressions']:,.0f}  sitemaps={sm.get('count')}  "
                f"with_errors={len(sm.get('with_errors', []))}"
            )
    ga = report.get("ga4", {})
    print("\n== GA4 ==")
    if not ga.get("ok"):
        print(f"  FAILED: {ga.get('error')}")
    else:
        for host, v in list(ga["hosts"].items())[:20]:
            print(f"  {host:35} sessions={v['sessions']:>10,.0f}  organic={v['organic_sessions']:>10,.0f}")
        if ga["tenant_hosts_not_seen"]:
            print(f"  Tenant hosts with no GA4 sessions: {', '.join(ga['tenant_hosts_not_seen'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default="aspose.app")
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    args = ap.parse_args(argv)

    tenant = load_tenant(args.tenant)
    report: dict = {"tenant": tenant.property, "run_date": date.today().isoformat()}

    try:
        credentials, identity = get_credentials()
    except CredentialsError as exc:
        print(f"Credentials problem: {exc}", file=sys.stderr)
        return 1
    report["identity"] = identity

    from core.ga4 import GA4Client
    from core.gsc import GSCClient

    start, end = default_window(args.days)
    try:
        report["gsc"] = check_gsc(GSCClient(credentials), tenant, start, end)
    except Exception as exc:  # noqa: BLE001
        report["gsc"] = {"ok": False, "error": _err(exc)}
    try:
        report["ga4"] = check_ga4(GA4Client(credentials), tenant)
    except Exception as exc:  # noqa: BLE001
        report["ga4"] = {"ok": False, "error": _err(exc)}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"access-check-{report['run_date']}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print_summary(report)
    print(f"\nFull report: {out_file}")
    ok = report["gsc"].get("ok") and report["ga4"].get("ok")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
