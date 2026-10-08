"""Offline tests for the findings store: stable ids, resolve/reopen, scoped runs, seeds and notes."""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.discovery.run import import_report  # noqa: E402
from core.findings import FindingIds, FindingsStore, finding_key, make_finding  # noqa: E402

IDS = FindingIds()


def f(check, host="app.example.com", template="sitemap:pdf", dimension="crawl", pages=1):
    return make_finding(ids=IDS, tenant="example.com", subdomain=host, template=template, dimension=dimension,
                        type="broken", check=check, title=f"{check} on {template}", urls=[f"https://{host}/x"],
                        observed="seen", expected="fine", affected_pages=pages)


def ids_by_key(store):
    return {r["key"]: r for r in store.findings(("open", "unverified", "resolved", "superseded"))}


def test_ids_are_stable_and_findings_resolve_and_reopen():
    s = FindingsStore(":memory:")
    a, b = f("fetch_failed"), f("empty")
    r1 = s.import_run([a, b], "discovery-run1", "discovery", hosts=["app.example.com"])
    assert sorted(r1["new"]) == ["crawl-0001", "crawl-0002"]
    rows = ids_by_key(s)
    id_a, id_b = rows[finding_key(a)]["id"], rows[finding_key(b)]["id"]

    r2 = s.import_run([f("fetch_failed", pages=5)], "discovery-run2", "discovery", hosts=["app.example.com"])
    assert r2["new"] == [] and r2["resolved"] == [id_b]
    rows = ids_by_key(s)
    assert rows[finding_key(a)]["id"] == id_a and rows[finding_key(a)]["affected_pages"] == 5
    assert rows[finding_key(b)]["status"] == "resolved"

    r3 = s.import_run([a, b], "discovery-run3", "discovery", hosts=["app.example.com"])
    assert r3["reopened"] == [id_b] and ids_by_key(s)[finding_key(b)]["status"] == "open"
    assert s.changes("discovery-run2")["resolved"][0]["id"] == id_b
    assert s.changes("discovery-run1")["first_run"] is True


def test_scoped_runs_only_resolve_what_they_covered():
    s = FindingsStore(":memory:")
    pdf, words = f("empty", template="sitemap:pdf"), f("empty", template="sitemap:words")
    robots = f("robots_missing", template="robots.txt")
    www = f("robots_missing", host="www.example.com", template="robots.txt")
    s.import_run([pdf, words, robots, www], "discovery-run1", "discovery",
                 hosts=["app.example.com", "www.example.com"])
    # sample run: app host, pdf section only, reports nothing
    r = s.import_run([], "discovery-run2", "discovery", hosts=["app.example.com"], sections=["pdf"])
    rows = ids_by_key(s)
    assert r["resolved"] == [rows[finding_key(pdf)]["id"]]
    assert all(rows[finding_key(x)]["status"] == "open" for x in (words, robots, www))


def test_stopped_run_resolves_nothing():
    s = FindingsStore(":memory:")
    s.import_run([f("empty")], "discovery-run1", "discovery", hosts=["app.example.com"])
    report = {"run_id": 2, "hosts": {"app.example.com": {}}, "findings": [], "not_fetched": ["https://x/s.xml"],
              "params": {"hosts": ["app.example.com"], "sections": None, "max_sitemaps": 5}}
    assert import_report(s, report)["resolved"] == []


def test_seed_import_keeps_ids_status_and_notes():
    s = FindingsStore(":memory:")
    s.import_run([f("robots_missing", template="robots.txt")], "discovery-run1", "discovery",
                 hosts=["app.example.com"])
    seed = {
        "id": "seed-onpage-01", "tenant": "example.com", "subdomain": "app.example.com", "template": "hub",
        "dimension": "onpage", "type": "incorrect", "title": "Typo", "effort": "S", "fix_tier": 1,
        "fix_channel": "git-pr", "status": "unverified", "affected_pages": 1,
        "evidence": {"urls": ["https://app.example.com/"], "observed": "tset", "expected": "test"},
        "verification": {"verified": False, "how": "fetch", "recheck": "onpage auditor"},
    }
    gone = dict(seed, id="seed-onpage-02", title="Old")
    note = {"applies_to": "crawl/robots_missing/app.example.com/robots.txt", "note": "Routing never serves it."}
    s.import_seed({"findings": [seed, gone], "notes": [note]}, "seed-1")
    s.import_seed({"findings": [seed], "notes": [note]}, "seed-2")

    rows = {r["id"]: r for r in s.findings(("open", "unverified", "resolved"))}
    assert rows["seed-onpage-01"]["status"] == "unverified" and rows["seed-onpage-01"]["source"] == "seed"
    assert rows["seed-onpage-01"]["verification"]["recheck"] == "onpage auditor"
    assert rows["seed-onpage-02"]["status"] == "resolved"
    robots = next(r for r in rows.values() if r["key"] == note["applies_to"])
    assert robots["notes"] == ["Routing never serves it."]
    # discovery ids are not disturbed by seeds
    assert robots["id"] == "crawl-0001"


def test_real_seed_file_imports():
    doc = yaml.safe_load((ROOT / "tenants" / "aspose.app.seed-findings.yaml").read_text(encoding="utf-8"))
    s = FindingsStore(":memory:")
    res = s.import_seed(doc, "seed-test")
    assert res["findings"] == len(doc["findings"]) and res["notes"] == len(doc.get("notes") or [])


def test_report_from_export_matches_store():
    from scripts.build_report import from_export
    from scripts.findings import ALL_STATUSES

    s = FindingsStore(":memory:")
    s.import_run([f("fetch_failed"), f("empty")], "discovery-run1", "discovery", hosts=["app.example.com"])
    s.import_run([f("fetch_failed"), f("redirected")], "discovery-run2", "discovery", hosts=["app.example.com"])
    rows = s.findings(ALL_STATUSES)  # what scripts/findings.py export writes
    for run in ("discovery-run1", "discovery-run2"):
        findings, changes = from_export(rows, run)
        want = s.changes(run)
        assert findings == s.findings()
        assert changes["new"] == want["new"] and changes["first_run"] == want["first_run"]
        assert [r["id"] for r in changes["resolved"]] == [r["id"] for r in want["resolved"]]
