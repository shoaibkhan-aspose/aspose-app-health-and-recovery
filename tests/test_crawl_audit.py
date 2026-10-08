"""Offline tests for the crawl auditor: page parsing, per-page checks, sampling and a full run."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit.crawl import audit, import_report, latest_inventory_run, page_issues  # noqa: E402
from core.audit.page import parse_page  # noqa: E402
from core.audit.sample import sample_inventory  # noqa: E402
from core.audit.store import AuditStore  # noqa: E402
from core.discovery.fetch import FetchResult  # noqa: E402
from core.discovery.store import InventoryStore  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.tenant import Tenant  # noqa: E402

DESC = "A meta description that is long enough to pass the length check easily."


def html(title="Convert PDF files online", canonical=None, hreflang=(), lang="en", h1=("Convert PDF",),
         desc="auto", viewport="width=device-width, initial-scale=1", robots=None) -> bytes:
    head = [f"<title>{title}</title>" if title is not None else ""]
    desc = f"{title}: {DESC}" if desc == "auto" else desc
    if desc is not None:
        head.append(f'<meta name="description" content="{desc}">')
    if canonical:
        head.append(f'<link rel="canonical" href="{canonical}">')
    head += [f'<link rel="alternate" hreflang="{c}" href="{h}">' for c, h in hreflang]
    if viewport is not None:
        head.append(f'<meta name="viewport" content="{viewport}">')
    if robots:
        head.append(f'<meta name="robots" content="{robots}">')
    body = "".join(f"<h1>{h}</h1>" for h in h1)
    lang_attr = f' lang="{lang}"' if lang else ""
    return f"<!doctype html><html{lang_attr}><head>{''.join(head)}</head><body>{body}</body></html>".encode()


def rec(url, status=200, final_url=None, redirects=(), redirect_statuses=(), source="sitemap", lang=None):
    return {"url": url, "status": status, "final_url": final_url or url, "redirects": list(redirects),
            "redirect_statuses": list(redirect_statuses), "blocked": 0, "error": None, "source": source,
            "lang": lang, "content_type": "text/html"}


def checks(issues):
    return {c for c, _ in issues}


# ---- page parsing ----

def test_parse_page_reads_head_and_headers():
    body = (b'<html lang="FR"><head><title> A  b </title><link rel="Canonical" href="/x">'
            b'<link rel="alternate" hreflang="de" href="/de/x"><meta NAME="Robots" content="NoIndex"></head>'
            b'<body><h1> Hi <b>there</b></h1><link rel="canonical" href="/body-ignored"></body></html>')
    f = parse_page(body, "https://h.example/fr/x", {"link": '<https://h.example/x>; rel="canonical"',
                                                       "x-robots-tag": "noarchive"})
    assert f.titles == ["A b"] and f.html_lang == "FR" and f.h1 == ["Hi there"]
    assert f.canonicals == ["https://h.example/x", "https://h.example/x"]
    assert f.hreflang == [("de", "https://h.example/de/x")]
    assert f.robots == ["noindex", "noarchive"] and f.noindex


# ---- per-page checks ----

def test_clean_page_has_no_issues():
    url = "https://h.example/pdf/merge"
    facts = parse_page(html(canonical=url, hreflang=[("en", url), ("x-default", url)]), url)
    assert page_issues(rec(url), facts, localized=True) == []


def test_redirect_canonical_and_tag_issues():
    url = "https://h.example/pdf/split"
    final = "https://h.example/pdf/splitter"
    facts = parse_page(html(title="x", canonical="https://h.example/pdf/other", desc=None, h1=("A", "B"), lang=None,
                            viewport="width=device-width, user-scalable=no"), final)
    got = checks(page_issues(rec(url, final_url=final, redirects=[url, url + "/"], redirect_statuses=[302, 301]),
                             facts, localized=True))
    assert {"redirected", "redirect_chain", "redirect_temporary", "canonical_elsewhere", "title_length",
            "description_missing", "h1_multiple", "hreflang_missing", "html_lang_missing", "viewport_no_zoom"} <= got


def test_soft_404_errors_and_blocked():
    url = "https://h.example/email/sitemap.xml"
    final = "https://h.example/email/error?code=404"
    facts = parse_page(html(title="Error"), final)
    assert checks(page_issues(rec(url, final_url=final, redirects=[url], redirect_statuses=[302]), facts, False)) == {
        "redirected", "redirect_temporary", "soft_404"}
    assert checks(page_issues(rec(url, status=404), None, False)) == {"http_error"}
    blocked = {**rec(url, status=None), "blocked": 1}
    assert checks(page_issues(blocked, None, False)) == {"robots_blocked"}


def test_hreflang_and_lang_checks():
    url = "https://h.example/fr/pdf/merge"
    facts = parse_page(html(canonical=url, lang="de", hreflang=[("en_US", "https://h.example/pdf/merge")]), url)
    got = checks(page_issues(rec(url, lang="fr"), facts, localized=True))
    assert got == {"hreflang_invalid", "hreflang_no_self", "hreflang_no_xdefault", "html_lang_mismatch"}


# ---- sampling ----

def make_inventory():
    inv = InventoryStore(":memory:")
    run = inv.start_run("example.com", {"hosts": [], "sections": [], "max_sitemaps": None})
    rows = []
    for i in range(50):
        rows.append((run, f"https://app.example.com/pdf/tool{i}", "app.example.com", "pdf", None, "tool", None, "s", 0))
    for lang in ("fr", "de", "es"):
        for i in range(5):
            rows.append((run, f"https://app.example.com/pdf/{lang}/tool{i}", "app.example.com", "pdf", lang, "tool",
                         None, "s", 0))
    rows.append((run, "https://app.example.com/pdf/", "app.example.com", "pdf", None, "family-hub", None, "s", 0))
    rows.append((run, "https://app.example.com/omr/x", "app.example.com", "omr", None, "tool", None, "s", 0))
    rows.append((run, "https://www.example.com/", "www.example.com", "", None, "home", None, "s", 0))
    inv.add_urls(rows)
    inv.finish_run(run, {})
    return inv, run


def test_sample_is_stable_and_bounded():
    inv, run = make_inventory()
    s1 = sample_inventory(inv.db, run, ["app.example.com"], {"app.example.com"}, per_template=2, other_langs=2)
    s2 = sample_inventory(inv.db, run, ["app.example.com"], {"app.example.com"}, per_template=2, other_langs=2)
    assert s1 == s2
    tool = [s for s in s1 if s.section == "pdf" and s.template == "tool"]
    assert len(tool) == 4 and len([s for s in tool if s.lang is None]) == 2
    assert len({s.lang for s in tool if s.lang}) == 2 and tool[0].group_urls == 65
    capped = sample_inventory(inv.db, run, ["app.example.com"], {"app.example.com"}, max_per_host=3)
    assert {(s.section, s.template) for s in capped} == {("pdf", "tool"), ("pdf", "family-hub"), ("omr", "tool")}
    more = sample_inventory(inv.db, run, ["app.example.com"], {"app.example.com"}, other_langs=0,
                            per_template_by_host={"app.example.com": 10})
    assert len([s for s in more if (s.section, s.template) == ("pdf", "tool")]) == 10
    only = sample_inventory(inv.db, run, ["app.example.com"], {"app.example.com"}, sections=["omr"])
    assert [s.url for s in only] == ["https://app.example.com/omr/x"]
    assert latest_inventory_run(inv.db) == run


# ---- full run ----

class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.requests = pages, 0

    def get(self, url):
        self.requests += 1
        if url not in self.pages:
            return FetchResult(url, 404, final_url=url, content_type="text/html", body=b"<html></html>")
        body = self.pages[url]
        return FetchResult(url, 200, final_url=url, content_type="text/html; charset=utf-8", body=body)


def make_tenant(extra_urls=()):
    raw = {
        "property": "example.com", "excluded_host_patterns": ["*-qa.*"],
        "hosts": [{"host": "www.example.com"}, {"host": "app.example.com", "fix_channel": "git-pr"}],
        "discovery": {"section_sitemaps": {"app.example.com": {"sections": ["pdf", "omr"], "paths": []}}},
        "audit": {"extra_urls": list(extra_urls)},
    }
    return Tenant(property="example.com", gsc_properties=[], ga4_property_id=None, hosts=raw["hosts"],
                  excluded_host_patterns=raw["excluded_host_patterns"], raw=raw)


def test_audit_run_findings_and_resolution():
    inv, run = make_inventory()
    pages = {}
    for i in range(50):
        u = f"https://app.example.com/pdf/tool{i}"
        pages[u] = html(title=f"Tool {i} for PDF files", canonical=u)  # no hreflang on a localized section
    for lang in ("fr", "de", "es"):
        for i in range(5):
            u = f"https://app.example.com/pdf/{lang}/tool{i}"
            pages[u] = html(title=f"Tool {i} {lang} for PDF files", canonical=u, lang=lang)
    pages["https://app.example.com/pdf/"] = html(title="Same hub title", canonical="https://app.example.com/pdf/")
    pages["https://app.example.com/omr/"] = html(title="Same hub title", canonical="https://app.example.com/omr/")
    pages["https://www.example.com/"] = html(title="Home page title", canonical="https://www.example.com/")
    pages["https://app.example.com/"] = html(title="App home title", canonical="https://app.example.com/")
    tenant = make_tenant(extra_urls=["https://app.example.com/omr/", "https://x-qa.example.com/"])

    report = audit(tenant, FakeFetcher(pages), inv.db, run, AuditStore(":memory:"), log=None)
    by_check = {(f["check"], f["template"]): f for f in report["findings"]}
    assert set(by_check) == {("hreflang_missing", "page:pdf"), ("http_error", "page:omr"),
                             ("title_duplicate", "page:pdf"), ("title_duplicate", "page:omr"),
                             ("description_duplicate", "page:pdf"), ("description_duplicate", "page:omr")}
    hl = by_check[("hreflang_missing", "page:pdf")]
    assert hl["dimension"] == "intl" and hl["fix_channel"] == "git-pr"
    assert "tool 4/4" in hl["evidence"]["observed"] and hl["affected_pages"] == 66  # 65 tool + 1 hub
    assert not any("x-qa" in p["url"] for p in report["pages"])

    fstore = FindingsStore(":memory:")
    first = import_report(fstore, report)
    assert len(first["new"]) == 6
    for i in range(50):  # the template gains hreflang
        u = f"https://app.example.com/pdf/tool{i}"
        pages[u] = html(title=f"Tool {i} for PDF files", canonical=u, hreflang=[("en", u), ("x-default", u)])
    for lang in ("fr", "de", "es"):
        for i in range(5):
            u = f"https://app.example.com/pdf/{lang}/tool{i}"
            pages[u] = html(title=f"Tool {i} {lang} for PDF files", canonical=u, lang=lang,
                            hreflang=[(lang, u), ("x-default", u)])
    pages["https://app.example.com/pdf/"] = html(title="PDF hub page title", canonical="https://app.example.com/pdf/",
                                                 hreflang=[("en", "https://app.example.com/pdf/"),
                                                           ("x-default", "https://app.example.com/pdf/")])
    second = import_report(fstore, audit(tenant, FakeFetcher(pages), inv.db, run, AuditStore(":memory:"), log=None))
    assert len(second["resolved"]) == 5  # hreflang + duplicate titles/descriptions; the omr 404 stays open


def test_xdefault_is_not_a_self_reference_and_repeated_tags():
    url = "https://h.example/html/conversion/md"
    alts = [("x-default", url), ("de", "https://h.example/html/de/conversion/md")]
    body = html(canonical=url, hreflang=alts + alts).replace(b"</head>", f'<link rel="canonical" href="{url}"></head>'
                                                             .encode())
    facts = parse_page(body, url, {"link": f'<{url}>; rel="canonical"'})
    got = checks(page_issues(rec(url), facts, localized=True))
    assert got == {"hreflang_no_self", "head_tags_repeated"}
    facts = parse_page(html(canonical=url, hreflang=[("de", url), ("de", url + "x"), ("x-default", url)]), url)
    assert "hreflang_conflict" in checks(page_issues(rec(url), facts, localized=True))


def test_section_filter_keeps_other_hosts_and_homepages():
    inv, run = make_inventory()
    tenant = make_tenant(extra_urls=["https://app.example.com/pdf/"])
    tenant.raw["audit"]["section_urls"] = {"app.example.com": ["/{section}/family/"]}
    report = audit(tenant, FakeFetcher({}), inv.db, run, AuditStore(":memory:"), sections=["omr"], log=None)
    urls = {p["url"] for p in report["pages"]}
    assert urls == {"https://app.example.com/omr/x", "https://www.example.com/", "https://app.example.com/",
                    "https://app.example.com/omr/family/"}
    fstore = FindingsStore(":memory:")
    import_report(fstore, report)  # every page 404s: one http_error finding per (host, section)
    keys = {f["key"] for f in fstore.findings()}
    assert "crawl/http_error/www.example.com/page" in keys and "crawl/http_error/app.example.com/page" in keys
    report["findings"] = []
    assert len(import_report(fstore, report)["resolved"]) == 3  # omr section + both unsectioned findings


def test_meta_refresh_stub_and_cjk_width():
    url = "https://h.example/en/"
    body = (b'<html lang=en><head><title>x</title><meta name=robots content="noindex">'
            b'<meta http-equiv=refresh content="0; url=https://h.example/"></head></html>')
    assert checks(page_issues(rec(url), parse_page(body, url), localized=True)) == {"meta_refresh"}
    url = "https://h.example/zh-hant/with-logo"
    facts = parse_page(html(title="線上生成帶標誌", canonical=url, lang="zh-hant",
                            desc="免費產生帶有自訂徽標的二維碼。此外，還可額外付費使用徽標自動調整最大尺寸功能。"), url)
    assert page_issues(rec(url, lang="zh-hant"), facts, localized=False) == []
