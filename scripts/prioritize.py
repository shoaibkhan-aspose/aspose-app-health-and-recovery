"""Prioritizer: attach 28-day Search Console traffic to every open finding and score it (impact x severity / effort).

    set -a; source .env; set +a
    python scripts/prioritize.py                 # fetch GSC + GA4 (cached per window in data/), score, export
    python scripts/prioritize.py --offline       # reuse the newest cached traffic, no Google calls

Read-only: Search Console page rows per property (webmasters.readonly) and GA4 organic sessions per host
(analytics.readonly). Writes `impact` into the findings store and re-exports reports/findings.json.
Run it after every discover.py / audit_crawl.py import.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.audit.crawl import latest_inventory_run  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.gsc import API_MAX_ROWS, default_window  # noqa: E402
from core.prioritize import TrafficIndex, prioritize, snapshot_discovered  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.findings import DEFAULT_STORE, export  # noqa: E402

CACHE_DIR = REPO_ROOT / "data"
MAX_ROWS_PER_PROPERTY = 200_000


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"


def fetch_gsc_pages(gsc, site: str, start: str, end: str) -> list[dict]:
    """All page rows of one property for the window, paged 25k at a time."""
    rows, offset = [], 0
    while offset < MAX_ROWS_PER_PROPERTY:
        batch = gsc.search_analytics(site, start, end, dimensions=["page"], row_limit=API_MAX_ROWS, start_row=offset)
        rows += [{"page": r["keys"][0], "clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0))}
                 for r in batch]
        if len(batch) < API_MAX_ROWS:
            break
        offset += API_MAX_ROWS
    return rows


def fetch_ga4_organic(ga4, property_id: str) -> dict:
    rows = ga4.run_report(property_id, dimensions=["hostName", "sessionDefaultChannelGroup"], metrics=["sessions"])
    out: dict = defaultdict(float)
    for r in rows:
        if r["sessionDefaultChannelGroup"] == "Organic Search":
            out[r["hostName"]] += r["sessions"]
    return {h: round(v) for h, v in out.items()}


def load_traffic(tenant, offline: bool, days: int) -> dict:
    if offline:
        cached = sorted(CACHE_DIR.glob(f"traffic-{tenant.property}-*.json"))
        if not cached:
            raise SystemExit("No cached traffic in data/; run without --offline first.")
        return json.loads(cached[-1].read_text(encoding="utf-8"))
    start, end = default_window(days)
    path = CACHE_DIR / f"traffic-{tenant.property}-{start}_{end}.json"
    if path.is_file():
        print(f"Using cached traffic {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    from core.ga4 import GA4Client
    from core.google_auth import get_credentials
    from core.gsc import GSCClient

    credentials, identity = get_credentials()
    print(f"Identity: {identity}; window {start}..{end}")
    gsc = GSCClient(credentials)
    data = {"window": [start, end], "gsc": {}, "gsc_errors": {}, "ga4_organic": {}}
    for site in tenant.gsc_properties:
        try:
            data["gsc"][site] = fetch_gsc_pages(gsc, site, start, end)
            print(f"  {site}: {len(data['gsc'][site]):,} pages")
        except Exception as exc:  # noqa: BLE001
            data["gsc_errors"][site] = _err(exc)
            print(f"  {site}: {data['gsc_errors'][site]}")
    if tenant.ga4_property_id:
        try:
            data["ga4_organic"] = fetch_ga4_organic(GA4Client(credentials), tenant.ga4_property_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  GA4: {_err(exc)}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def section_urls(inventory_path: Path, tenant) -> dict:
    """(host, section) and (host, None) -> URL count in the latest full discovery run."""
    if not inventory_path.is_file():
        return {}
    db = sqlite3.connect(inventory_path)
    run = latest_inventory_run(db)
    sectioned = set((tenant.raw.get("discovery") or {}).get("section_sitemaps") or {})
    out: dict = defaultdict(int)
    for host, section, n in db.execute("SELECT host, section, COUNT(*) FROM urls WHERE run_id = ? GROUP BY 1, 2",
                                       (run,)):
        out[(host, section if host in sectioned else "")] += n
        out[(host, None)] += n
    return dict(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--offline", action="store_true", help="use the newest cached traffic in data/")
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--inventory", default=str(REPO_ROOT / "data" / "inventory.sqlite"))
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args(argv)

    tenant = load_tenant(args.tenant)
    data = load_traffic(tenant, args.offline, args.days)
    rows = [r for site_rows in data["gsc"].values() for r in site_rows]
    idx = TrafficIndex.build(tenant, rows, data.get("ga4_organic"), window=tuple(data["window"]))

    store = FindingsStore(args.store)
    findings = store.findings()
    impacts = prioritize(findings, idx, section_urls(Path(args.inventory), tenant), snapshot_discovered(tenant))
    print(f"Scored {store.set_impacts(impacts)} findings (window {data['window'][0]}..{data['window'][1]})")

    by_id = {f["id"]: f for f in findings}
    print(f"\n== Top {args.top} by score ==")
    for fid, imp in sorted(impacts.items(), key=lambda kv: -kv[1]["score"])[: args.top]:
        f = by_id[fid]
        clicks = "–" if imp["clicks_28d"] is None else f"{imp['clicks_28d']:,}"
        impr = "–" if imp["impressions_28d"] is None else f"{imp['impressions_28d']:,}"
        print(f"  {imp['score']:>3}  {fid:<16} {f['type']:<13} clicks={clicks:>9} impr={impr:>11} "
              f"[{imp['source']}]  {f['subdomain']}: {f['title']}")
    none = [fid for fid, imp in impacts.items() if imp["source"] == "none"]
    if none:
        print(f"\nNo traffic data for {len(none)} findings (host has no GSC property and no GA4 sessions).")
    print(f"Findings export: {export(store)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
