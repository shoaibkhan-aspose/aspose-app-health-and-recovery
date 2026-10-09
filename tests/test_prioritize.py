"""Offline tests for the Prioritizer: traffic buckets, share of traffic per finding source, scores."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.findings import FindingsStore  # noqa: E402
from core.prioritize import TrafficIndex, page_key, prioritize, score  # noqa: E402
from core.tenant import Tenant  # noqa: E402


def make_tenant():
    raw = {
        "property": "example.com", "hosts": [{"host": "app.example.com"}, {"host": "forum.example.com"}],
        "gsc_properties": ["https://app.example.com/"],
        "discovery": {"section_sitemaps": {"app.example.com": {"sections": ["pdf", "omr"], "paths": []}}},
        "url_templates": {"app.example.com": [
            {"name": "family-hub", "pattern": "^/(?P<family>[^/]+)/?$"},
            {"name": "tool", "pattern": "^/(?P<family>[^/]+)/(?P<tool>[^/]+)/?$"}]},
    }
    return Tenant(property="example.com", gsc_properties=raw["gsc_properties"], ga4_property_id=None,
                  hosts=raw["hosts"], raw=raw)


ROWS = [
    {"page": "https://app.example.com/pdf/merge", "clicks": 1000, "impressions": 50000},
    {"page": "https://app.example.com/pdf/fr/merge", "clicks": 500, "impressions": 20000},
    {"page": "https://app.example.com/pdf/", "clicks": 100, "impressions": 3000},
    {"page": "https://app.example.com/omr/scan", "clicks": 10, "impressions": 400},
]


def finding(fid, template, ftype="broken", source="discovery", effort="S", pages=0, urls=None, sample=None,
            host="app.example.com"):
    ev = {"urls": urls or [f"https://{host}/x"], "observed": "o", "expected": "e"}
    if sample:
        ev["sample"] = sample
    return {"id": fid, "subdomain": host, "template": template, "type": ftype, "effort": effort,
            "affected_pages": pages, "evidence": ev, "source": source}


def test_index_buckets_by_section_template_and_language_free_template():
    idx = TrafficIndex.build(make_tenant(), ROWS, {"forum.example.com": 400}, window=("2026-09-01", "2026-09-28"))
    assert idx.get("app.example.com", "pdf", "tool").clicks == 1500  # fr page joins its template
    assert idx.get("app.example.com", "pdf").clicks == 1600
    assert idx.get("app.example.com").impressions == 73400
    assert idx.pages[page_key("https://APP.example.com/pdf")] == (100, 3000)
    assert idx.gsc_hosts == {"app.example.com"}


def test_share_per_source_and_scores():
    idx = TrafficIndex.build(make_tenant(), ROWS, {"forum.example.com": 400})
    sections = {("app.example.com", "pdf"): 100, ("app.example.com", None): 120, ("forum.example.com", None): 50}
    fs = [
        finding("crawl-1", "sitemap:pdf", pages=25),  # discovery: 25% of pdf
        {**finding("crawl-2", "robots.txt", ftype="improvable"), "check": "robots_missing"},  # whole host
        {**finding("crawl-4", "sitemap", urls=["https://app.example.com/sitemap.xml"]), "check": "fetch_failed"},
        {**finding("crawl-5", "sitemap:omr", urls=["https://app.example.com/omr/s.xml"]), "check": "fetch_failed"},
        finding("intl-1", "page:pdf", ftype="not_optimized", source="crawl-audit", effort="M",
                sample={"tool": [2, 4], "family-hub": [1, 1]}),
        finding("seed-onpage-01", "family-hub", ftype="incorrect", source="seed", urls=["https://app.example.com/pdf/"]),
        finding("crawl-3", "page", source="crawl-audit", host="forum.example.com", pages=25,
                sample={"generic:/{section}/*/*": [20, 20]}),
    ]
    known = {"https://app.example.com/sitemap.xml": 12}  # GSC discovered 12 URLs from the root sitemap
    imp = prioritize(fs, idx, sections, known)
    assert imp["crawl-4"]["share"] == 0.1 and imp["crawl-5"]["clicks_28d"] == 0  # unknown failed sitemap: 0
    c1 = imp["crawl-1"]
    assert (c1["clicks_28d"], c1["share"], c1["source"]) == (400, 0.25, "gsc-section")
    assert imp["crawl-2"]["clicks_28d"] == 1610 and imp["crawl-2"]["source"] == "gsc-host"
    assert imp["intl-1"]["clicks_28d"] == 750 + 100  # half the tool template + the whole hub
    assert imp["seed-onpage-01"]["impressions_28d"] == 3000 and imp["seed-onpage-01"]["source"] == "gsc-pages"
    assert (imp["crawl-3"]["clicks_28d"], imp["crawl-3"]["source"]) == (200, "ga4-host")
    assert imp["crawl-1"]["score"] > imp["crawl-2"]["score"]  # broken beats improvable despite less traffic
    assert score(0, 0, "broken", "S") == 0 and score(10**9, 0, "broken", "S") == 100
    assert score(1000, 0, "broken", "M") < score(1000, 0, "broken", "S")


def test_store_keeps_impact_across_reimports():
    s = FindingsStore(":memory:")
    f = {"id": "x", "tenant": "example.com", "subdomain": "app.example.com", "template": "sitemap:pdf",
         "dimension": "crawl", "check": "empty", "type": "broken", "title": "t", "affected_pages": 1,
         "evidence": {"urls": ["https://app.example.com/a"], "observed": "o", "expected": "e"},
         "impact": {"clicks_28d": 0, "impressions_28d": 0, "score": 0}, "effort": "S"}
    s.import_run([f], "discovery-run1", "discovery", hosts=["app.example.com"])
    fid = s.findings()[0]["id"]
    s.set_impacts({fid: {"clicks_28d": 5, "impressions_28d": 9, "score": 40, "source": "gsc-section"}})
    s.import_run([f], "discovery-run2", "discovery", hosts=["app.example.com"])
    assert s.findings()[0]["impact"]["score"] == 40
