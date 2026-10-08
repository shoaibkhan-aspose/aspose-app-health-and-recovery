"""Findings store CLI: import discovery runs and seed findings, list what is open.

    python scripts/findings.py import-seed                      # tenants/<tenant>.seed-findings.yaml
    python scripts/findings.py import-discovery reports/discovery-2026-10-08-run6.json
    python scripts/findings.py list --dimension intl --host products.aspose.app
    python scripts/findings.py summary
    python scripts/findings.py export                           # reports/findings.json (committed)

discover.py imports its run automatically; import-discovery is for older run files.
The store is data/findings.sqlite (gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.discovery.run import import_report  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.tenant import TENANTS_DIR  # noqa: E402

DEFAULT_STORE = REPO_ROOT / "data" / "findings.sqlite"
DEFAULT_EXPORT = REPO_ROOT / "reports" / "findings.json"
ALL_STATUSES = ("open", "unverified", "resolved", "superseded")


def seed_path(tenant: str) -> Path:
    return TENANTS_DIR / f"{tenant}.seed-findings.yaml"


def import_seed(store: FindingsStore, tenant: str) -> dict | None:
    path = seed_path(tenant)
    if not path.is_file():
        return None
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return store.import_seed(doc, f"seed-{date.today().isoformat()}")


def export(store: FindingsStore, path: Path = DEFAULT_EXPORT) -> Path:
    """Every finding (all statuses) as readable JSON, sorted by id, so git diffs show what changed."""
    rows = sorted(store.findings(ALL_STATUSES), key=lambda f: f["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def print_changes(result: dict) -> None:
    print(f"{result['run']}: {result['findings']} findings, {len(result['new'])} new, "
          f"{len(result['reopened'])} reopened, {len(result['resolved'])} resolved")
    for label in ("new", "reopened", "resolved"):
        if result[label]:
            print(f"  {label}: {', '.join(result[label])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--tenant", default=os.environ.get("TENANT", "aspose.app"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-seed", help="load the tenant's seed findings file")
    p = sub.add_parser("import-discovery", help="load a discovery report JSON")
    p.add_argument("report")
    p = sub.add_parser("list", help="list findings")
    p.add_argument("--status", default="open,unverified")
    p.add_argument("--dimension")
    p.add_argument("--host")
    p.add_argument("--json", action="store_true")
    sub.add_parser("summary", help="counts by status, dimension and type")
    p = sub.add_parser("export", help="write all findings to reports/findings.json")
    p.add_argument("--out", default=str(DEFAULT_EXPORT))
    args = ap.parse_args(argv)

    store = FindingsStore(args.store)
    if args.cmd == "import-seed":
        res = import_seed(store, args.tenant)
        if res is None:
            print(f"No seed file: {seed_path(args.tenant)}", file=sys.stderr)
            return 1
        print(f"{res['run']}: {res['findings']} seed findings, {res['notes']} notes")
    elif args.cmd == "import-discovery":
        print_changes(import_report(store, json.loads(Path(args.report).read_text(encoding="utf-8"))))
    elif args.cmd == "list":
        rows = [f for f in store.findings(tuple(args.status.split(",")))
                if (not args.dimension or f["dimension"] == args.dimension)
                and (not args.host or f["subdomain"] == args.host)]
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            for f in rows:
                print(f"{f['id']:<18} {f['status']:<10} {f['type']:<13} {f['subdomain']:<24} {f['title']}")
            print(f"{len(rows)} findings")
    elif args.cmd == "export":
        print(f"Wrote {export(store, Path(args.out))}")
    elif args.cmd == "summary":
        rows = store.findings(ALL_STATUSES)
        for field in ("status", "source", "dimension", "type"):
            counts = Counter(f[field] for f in rows)
            print(f"{field:>10}: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
