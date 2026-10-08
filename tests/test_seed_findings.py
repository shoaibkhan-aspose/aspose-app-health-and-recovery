"""Seed findings (manual review, tenants/<tenant>.seed-findings.yaml) must follow the finding schema."""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.findings import EFFORTS, FINDING_TYPES, FIX_CHANNELS  # noqa: E402
from core.tenant import load_tenant  # noqa: E402

DIMENSIONS = {
    "crawl", "perf", "onpage", "schema", "intl", "content", "ai",
    "functional", "ux", "authority", "competitive", "cross", "platform",
}
CODE_KEY = re.compile(r"^[a-z]+/[a-z_]+/[a-z0-9.-]+/[^/]+$")  # <dimension>/<check>/<subdomain>/<template>


def _load():
    return yaml.safe_load((ROOT / "tenants" / "aspose.app.seed-findings.yaml").read_text(encoding="utf-8"))


def test_seed_findings_follow_schema():
    doc = _load()
    hosts = {h["host"] for h in load_tenant(doc["tenant"]).hosts}
    ids = set()
    assert doc["findings"]
    for f in doc["findings"]:
        assert f["id"] not in ids, f"duplicate id {f['id']}"
        ids.add(f["id"])
        assert f["id"].startswith(f"seed-{f['dimension']}-"), f["id"]
        assert f["tenant"] == doc["tenant"]
        assert f["subdomain"] in hosts, f"{f['id']}: {f['subdomain']} is not a tenant host"
        assert f["type"] in FINDING_TYPES, f["id"]
        assert f["effort"] in EFFORTS, f["id"]
        assert f["dimension"] in DIMENSIONS, f["id"]
        assert f["fix_channel"] is None or f["fix_channel"] in FIX_CHANNELS, f["id"]
        ev = f["evidence"]
        assert ev["urls"] and ev["observed"] and ev["expected"], f["id"]
        assert all(u.startswith("https://") for u in ev["urls"]), f["id"]
        assert not any("-qa." in u for u in ev["urls"]), f"{f['id']}: QA hosts are out of scope"
        verified = f["verification"]["verified"]
        assert f["status"] == ("open" if verified else "unverified"), f["id"]
        assert f["verification"]["recheck"], f"{f['id']}: name the auditor check that re-detects it"


def test_seed_notes_point_at_code_findings():
    for n in _load().get("notes") or []:
        assert CODE_KEY.match(n["applies_to"]), n["applies_to"]
        assert n["note"].strip()
