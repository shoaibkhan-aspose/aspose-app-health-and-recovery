"""Speed auditor: PSI parsing, thresholds, page selection, findings and resolution. Offline (fake API)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit.perf import (  # noqa: E402
    PerfAudit,
    import_report,
    page_issues,
    rating,
    select_pages,
)
from core.audit.store import AuditStore  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.google_auth import CredentialsError, assert_readonly  # noqa: E402
from core.pagespeed import PageLoadError, PageSpeedClient, PageSpeedError, extract  # noqa: E402
from core.tenant import Tenant  # noqa: E402


def make_tenant():
    raw = {
        "property": "example.com", "excluded_host_patterns": ["*-qa.*"],
        "hosts": [{"host": "www.example.com"}, {"host": "app.example.com", "fix_channel": "git-pr"}],
        "discovery": {"section_sitemaps": {"app.example.com": {"sections": ["pdf", "omr"], "paths": []}}},
    }
    return Tenant(property="example.com", gsc_properties=[], ga4_property_id=None, hosts=raw["hosts"],
                  excluded_host_patterns=raw["excluded_host_patterns"], raw=raw)


def psi(lcp=None, inp=None, cls=None, ttfb=None, score=0.8, origin=None, fallback=False, url="u"):
    """A trimmed PageSpeed Insights response."""
    def block(m, id_):
        names = {"lcp": "LARGEST_CONTENTFUL_PAINT_MS", "inp": "INTERACTION_TO_NEXT_PAINT",
                 "cls": "CUMULATIVE_LAYOUT_SHIFT_SCORE", "ttfb": "EXPERIMENTAL_TIME_TO_FIRST_BYTE"}
        return {"id": id_, "metrics": {names[k]: {"percentile": round(v * 100) if k == "cls" else v}
                                       for k, v in m.items() if v is not None}}
    le = block({"lcp": lcp, "inp": inp, "cls": cls, "ttfb": ttfb}, url)
    if fallback:
        le["origin_fallback"] = True
    return {
        "loadingExperience": le,
        "originLoadingExperience": block(origin or {"lcp": 2000, "inp": 150, "cls": 0.05}, "origin"),
        "lighthouseResult": {
            "finalDisplayedUrl": url, "lighthouseVersion": "12.0.0",
            "categories": {"performance": {"score": score}},
            "audits": {
                "largest-contentful-paint": {"numericValue": 6100.0},
                "total-blocking-time": {"numericValue": 900.0},
                "cumulative-layout-shift": {"numericValue": 0.02},
                "unused-javascript": {"title": "Reduce unused JavaScript", "score": 0.3,
                                      "details": {"overallSavingsMs": 1200}},
                "render-blocking-resources": {"title": "Eliminate render-blocking resources", "score": 0.5,
                                              "metricSavings": {"LCP": 450, "FCP": 300}},
                "uses-text-compression": {"title": "Enable text compression", "score": 0.5,
                                          "details": {"overallSavingsMs": 40}},  # below the noise floor
                "font-display": {"title": "Font display", "score": 1, "details": {"overallSavingsMs": 500}},
            },
        },
    }


def test_extract_field_lab_and_savings():
    r = extract(psi(lcp=4800, inp=120, cls=0.31, ttfb=2000, score=0.42, url="https://app.example.com/pdf/a"))
    assert r["field"]["metrics"] == {"lcp": 4800, "inp": 120, "cls": 0.31, "ttfb": 2000}
    assert r["origin"]["metrics"]["cls"] == 0.05
    assert r["lab_score"] == 42 and r["lab"]["tbt"] == 900.0
    assert r["savings"] == [["Reduce unused JavaScript", 1200], ["Eliminate render-blocking resources", 450]]
    assert extract(psi(lcp=4800, fallback=True))["field"] is None  # origin data standing in for the URL


def test_ratings_and_page_issues():
    assert rating("lcp", 2500) == "good" and rating("lcp", 2501) == "needs_work" and rating("lcp", 4001) == "poor"
    assert rating("cls", 0.1) == "good" and rating("cls", 0.26) == "poor"
    page = extract(psi(lcp=4800, inp=300, cls=0.05, ttfb=1000, score=0.42))
    checks = dict(page_issues(page))
    assert set(checks) == {"lcp_poor", "inp_needs_work"}  # TTFB only counts when poor; lab score is no finding
    assert "LCP 4.8 s (real users, p75); lab 6.1 s, lab score 42; biggest savings: Reduce unused JavaScript " \
        "(1.2 s)" in checks["lcp_poor"]
    no_field = extract(psi(fallback=True, lcp=9000, score=0.9))
    assert page_issues(no_field) == []


def audit_db(rows):
    store = AuditStore(":memory:")
    run = store.start_run("example.com", "crawl-audit", {})
    for r in rows:
        store.add_page(run, {"status": 200, "facts": {"titles": ["t"]}, "issues": [], "redirects": [],
                             "group_urls": 100, "section": "", **r})
    return store.db


TOOL = "generic:/{section}/*"  # what the classifier gives /pdf/<tool> without tenant templates
CRAWL = [
    {"url": "https://app.example.com/pdf/merger", "host": "app.example.com", "section": "pdf", "template": TOOL},
    {"url": "https://app.example.com/pdf/split", "host": "app.example.com", "section": "pdf", "template": TOOL},
    {"url": "https://app.example.com/pdf/viewer", "host": "app.example.com", "section": "pdf", "template": TOOL},
    {"url": "https://app.example.com/pdf/old", "host": "app.example.com", "section": "pdf", "template": TOOL,
     "redirects": ["https://app.example.com/pdf/new"]},
    {"url": "https://app.example.com/omr/", "host": "app.example.com", "section": "omr",
     "template": "generic:/{section}", "issues": [["soft_404", "HTTP 200 at error page"]]},
    {"url": "https://www.example.com/", "host": "www.example.com", "template": "generic:/", "group_urls": 1},
]
SEARCH = [{"page": "https://app.example.com/pdf/viewer", "clicks": 900},
          {"page": "https://app.example.com/pdf/compress", "clicks": 5000},  # not crawled: same template
          {"page": "https://app.example.com/pdf/merger", "clicks": 10}]


def test_select_pages_prefers_traffic_and_skips_unclean():
    db = audit_db(CRAWL)
    targets = select_pages(db, make_tenant(), SEARCH, ["www.example.com", "app.example.com"], per_template=2)
    urls = [t.url for t in targets]
    assert set(urls) == {"https://app.example.com/pdf/compress", "https://app.example.com/pdf/viewer",
                         "https://www.example.com/"}
    assert urls[:2] == ["https://app.example.com/pdf/compress", "https://www.example.com/"]  # hosts alternate
    t = next(t for t in targets if t.url.endswith("compress"))
    assert t.source == "search" and t.group_urls == 100 and t.section == "pdf" and t.template == TOOL
    only_omr = select_pages(db, make_tenant(), SEARCH, ["app.example.com"], sections=["omr"])
    assert only_omr == []  # the only omr page is a soft 404


class FakeClient:
    strategy = "mobile"

    def __init__(self, responses):
        self.responses, self.calls = responses, 0

    def run(self, url):
        self.calls += 1
        r = self.responses.get(url)
        if isinstance(r, list):  # one reply per call
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r if r is not None else psi(lcp=1800, inp=100, cls=0.01, score=0.95, url=url)


def run_audit(responses, **kw):
    db = audit_db(CRAWL)
    return PerfAudit(make_tenant(), FakeClient(responses), db, AuditStore(":memory:"), search_rows=SEARCH,
                     log=None, **kw).run()


def test_audit_findings_sample_and_origin():
    slow = {"lcp": 2000, "inp": 600, "cls": 0.05}
    report = run_audit({
        "https://app.example.com/pdf/compress": psi(lcp=5200, inp=150, cls=0.02, score=0.3, origin=slow),
        "https://app.example.com/pdf/viewer": psi(fallback=True, lcp=5200, score=0.9, origin=slow),  # no URL data
    })
    by = {(f["check"], f["subdomain"], f["template"]): f for f in report["findings"]}
    assert set(by) == {("lcp_poor", "app.example.com", "page:pdf"), ("origin_cwv_poor", "app.example.com", "origin")}
    lcp = by[("lcp_poor", "app.example.com", "page:pdf")]
    assert lcp["evidence"]["sample"] == {TOOL: [1, 1]}  # the page without URL data is not in the sample
    assert lcp["affected_pages"] == 100 and lcp["dimension"] == "perf" and lcp["fix_channel"] == "git-pr"
    assert "1 of 1 pages with real-user data" in lcp["evidence"]["observed"]
    origin = by[("origin_cwv_poor", "app.example.com", "origin")]
    assert "INP 600 ms (poor)" in origin["evidence"]["observed"] and origin["effort"] == "L"
    assert report["summary"]["app.example.com"] == {"pages": 2, "errors": 0, "not_loaded": 0, "field": 1,
                                                   "median_score": 90,
                                                   "issues": {"lcp_poor": 1}}
    assert not report["incomplete"]


def test_resolution_and_incomplete_runs():
    fstore = FindingsStore(":memory:")
    bad = {"https://app.example.com/pdf/compress": psi(lcp=5200, inp=150, cls=0.02)}
    first = import_report(fstore, run_audit(bad))
    assert len(first["new"]) == 1
    # API errors on most pages: nothing resolves even though the finding is gone
    errors = {u: PageSpeedError("HTTP 500: boom") for u in ("https://app.example.com/pdf/compress",
                                                            "https://app.example.com/pdf/viewer")}
    flaky = run_audit(errors)
    assert flaky["incomplete"] and import_report(fstore, flaky)["resolved"] == []
    assert import_report(fstore, run_audit({}))["resolved"] == first["new"]  # fixed: resolves


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, replies):
        self.replies, self.params = list(replies), []

    def get(self, url, params=None, timeout=None):
        self.params.append(params)
        return self.replies.pop(0)


def test_client_retries_and_reports_disabled_api():
    ok = FakeResponse(200, {"lighthouseResult": {}})
    s = FakeSession([FakeResponse(429, {"error": {"message": "quota"}}), ok])
    client = PageSpeedClient(s, sleep=lambda _: None)
    assert client.run("https://a/") == {"lighthouseResult": {}} and client.calls == 2
    assert s.params[0] == {"url": "https://a/", "strategy": "mobile", "category": "performance"}
    disabled = FakeSession([FakeResponse(403, {"error": {"message": "PageSpeed Insights API has not been used in "
                                                                    "project 1 before or it is disabled."}})])
    with pytest.raises(PageSpeedError, match="not enabled"):
        PageSpeedClient(disabled, sleep=lambda _: None).run("https://a/")
    with pytest.raises(PageSpeedError, match="HTTP 400"):
        PageSpeedClient(FakeSession([FakeResponse(400, {"error": {"message": "bad url"}})])).run("x")


def test_identity_scope_allowed_write_scopes_refused():
    assert_readonly(["openid"])
    with pytest.raises(CredentialsError):
        assert_readonly(["https://www.googleapis.com/auth/cloud-platform"])


def test_report_json_round_trip():
    report = run_audit({"https://app.example.com/pdf/compress": psi(lcp=5200)})
    json.dumps(report, default=str)  # the CLI writes it as is


def test_retry_and_pages_that_do_not_load():
    load_err = PageLoadError("FAILED_DOCUMENT_REQUEST. Lighthouse was unable to reliably load the page")
    report = run_audit({
        "https://app.example.com/pdf/compress": [load_err],  # fails twice: a finding
        "https://app.example.com/pdf/viewer": [load_err, None],  # fails once, fine on retry: no finding
    })
    by = {f["check"]: f for f in report["findings"]}
    assert set(by) == {"lab_load_failed"}
    f = by["lab_load_failed"]
    assert f["type"] == "broken" and f["evidence"]["sample"] == {TOOL: [1, 2]}
    assert "FAILED_DOCUMENT_REQUEST" in f["evidence"]["observed"]
    pages = {p["url"]: p for p in report["pages"]}
    assert pages["https://app.example.com/pdf/viewer"]["retried"] and not pages["https://app.example.com/pdf/viewer"]["lab_error"]
    assert report["summary"]["app.example.com"]["not_loaded"] == 1
    assert not report["incomplete"]  # a page that does not load is a result, not an API failure


def test_nothing_painted_is_its_own_check():
    report = run_audit({"https://app.example.com/pdf/compress": [PageLoadError("NO_FCP. The page did not paint")]})
    f = next(f for f in report["findings"])
    assert f["check"] == "lab_no_paint" and f["type"] == "incorrect"


def test_client_turns_lighthouse_errors_into_page_load_errors():
    s = FakeSession([FakeResponse(500, {"error": {"message": "Lighthouse returned error: NO_FCP. The page did not "
                                                              "paint any content."}})])
    with pytest.raises(PageLoadError, match="^NO_FCP"):
        PageSpeedClient(s, sleep=lambda _: None).run("https://a/")
