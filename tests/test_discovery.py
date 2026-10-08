"""Offline tests for discovery: sitemap parsing, URL classification, polite fetcher, and a full run."""

import gzip
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.discovery.classify import classify_url, compile_templates, normalize_lang  # noqa: E402
from core.discovery.fetch import FetchResult, PoliteFetcher  # noqa: E402
from core.discovery.run import discover, summarize_robots  # noqa: E402
from core.discovery.sitemap import parse_sitemap  # noqa: E402
from core.discovery.store import InventoryStore  # noqa: E402
from core.tenant import Tenant, load_tenant  # noqa: E402

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml"'


def urlset(*urls, lastmod=None, alternates=False) -> bytes:
    items = []
    for u in urls:
        lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        alt = f'<xhtml:link rel="alternate" hreflang="fr" href="{u}fr/"/>' if alternates else ""
        items.append(f"<url><loc>{u}</loc>{lm}{alt}</url>")
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset {NS}>{"".join(items)}</urlset>'.encode()


def index(*children) -> bytes:
    items = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{items}</sitemapindex>'.encode()


# ---- sitemap parser ----

def test_parse_urlset_with_hreflang_and_lastmod():
    p = parse_sitemap(urlset("https://h.example/a", lastmod="2026-09-01", alternates=True))
    assert p.kind == "urlset" and p.error is None
    assert p.entries[0].lastmod == "2026-09-01"
    assert p.entries[0].alternates == [("fr", "https://h.example/afr/")]


def test_parse_gzip_bom_and_index():
    body = b"\xef\xbb\xbf\n  " + index("https://h.example/s1.xml", "https://h.example/s2.xml")
    p = parse_sitemap(gzip.compress(body))
    assert p.kind == "index" and p.gzipped and p.had_bom
    assert [e.loc for e in p.entries] == ["https://h.example/s1.xml", "https://h.example/s2.xml"]


def test_parse_text_html_and_broken():
    assert parse_sitemap(b"https://h.example/a\nhttps://h.example/b\n").kind == "text"
    html = parse_sitemap(b"<html><body>Not found</body></html>")
    assert html.kind == "unknown" and "root element" in html.error
    broken = parse_sitemap(b"<urlset><url><loc>x</loc>")
    assert broken.error and broken.error.startswith("XML parse error")


# ---- classification ----

def test_lang_detection_is_strict():
    assert normalize_lang("fr") == "fr"
    assert normalize_lang("zh-CN") == "zh-cn"
    assert normalize_lang("zh_hant") == "zh-hant"
    assert normalize_lang("to-word") is None
    assert normalize_lang("pdf") is None


def test_classify_with_tenant_templates():
    t = load_tenant("aspose.app")
    templates = compile_templates(t.raw["url_templates"]["products.aspose.app"])
    c = classify_url("https://products.aspose.app/barcode/fa/recognize/qr", templates)
    assert (c.section, c.lang, c.template) == ("barcode", "fa", "tool-variant")
    c = classify_url("https://products.aspose.app/pdf/conversion/pdf-to-docx", templates)
    assert c.template == "conversion-pair" and c.params == {"family": "pdf", "src": "pdf", "dst": "docx"}
    c = classify_url("https://products.aspose.app/pdf/merger/jpg-to-jpg", templates)
    assert c.template == "tool-pair"
    c = classify_url("https://products.aspose.app/words/", templates)
    assert (c.section, c.lang, c.template) == ("words", None, "family-hub")
    c = classify_url("https://www.aspose.app/fr/")
    assert (c.section, c.lang, c.template) == ("", "fr", "generic:/")


# ---- polite fetcher ----

class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.url, self.history = "", []

    def iter_bytes(self):
        yield self._body


class FakeClient:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    @contextmanager
    def stream(self, method, url):
        assert method == "GET"
        self.calls.append(url)
        resp = self.routes[url].pop(0) if isinstance(self.routes[url], list) else self.routes[url]
        resp.url = url
        yield resp


def test_fetcher_respects_robots_and_retries():
    client = FakeClient({
        "https://h.example/robots.txt": FakeResponse(200, b"User-agent: *\nDisallow: /private/\n"),
        "https://h.example/private/x": FakeResponse(200, b"secret"),
        "https://h.example/s.xml": [FakeResponse(503, headers={"retry-after": "1"}), FakeResponse(200, b"ok")],
    })
    sleeps = []
    f = PoliteFetcher(client=client, min_interval=0, sleep=sleeps.append, clock=lambda: 0.0)
    blocked = f.get("https://h.example/private/x")
    assert blocked.blocked_by_robots and "https://h.example/private/x" not in client.calls
    res = f.get("https://h.example/s.xml")
    assert res.ok and res.body == b"ok" and 1.0 in sleeps


def test_fetcher_robots_404_allows_and_5xx_blocks():
    client = FakeClient({
        "https://a.example/robots.txt": FakeResponse(404),
        "https://b.example/robots.txt": FakeResponse(500),
    })
    f = PoliteFetcher(client=client, min_interval=0, retries=0, sleep=lambda s: None, clock=lambda: 0.0)
    assert f.allowed("https://a.example/x")
    assert not f.allowed("https://b.example/x")


def test_summarize_robots():
    res = FetchResult("https://h.example/robots.txt", 200,
                      body=b"User-agent: GPTBot\nDisallow: /\nUser-agent: *\nAllow: /\n# c\nSitemap: https://h.example/s.xml\n")
    info = summarize_robots(res)
    assert info["sitemaps"] == ["https://h.example/s.xml"]
    assert info["ai_bots_named"] == ["GPTBot"] and info["disallow_rules"] == 1


# ---- full run with a fake fetcher ----

class FakeFetcher:
    max_bytes = 10**8

    def __init__(self, routes):
        self.routes, self.requests = routes, 0

    def _res(self, url):
        self.requests += 1
        status, body = self.routes.get(url, (404, b""))
        return FetchResult(url, status, final_url=url, body=body)

    def robots(self, origin):
        return self._res(origin + "/robots.txt")

    def get(self, url):
        return self._res(url)


def make_tenant() -> Tenant:
    raw = {
        "property": "example.com",
        "excluded_host_patterns": ["*-qa.*"],
        "hosts": [{"host": "www.example.com"}, {"host": "app.example.com", "fix_channel": "git-pr"}],
        "sections": ["pdf", "omr"],
        "discovery": {"section_sitemaps": {"app.example.com": {"sections": "sections",
                                                                "paths": ["/{section}/sitemap.xml"]}}},
        "gsc_sitemaps_snapshot": {"property": "https://app.example.com/", "sitemaps": [
            {"path": "/omr/sitemap.xml", "status": "Couldn't fetch", "last_read": "2025-05-30", "discovered": 544},
        ]},
    }
    return Tenant(property="example.com", gsc_properties=[], ga4_property_id=None, hosts=raw["hosts"],
                  excluded_host_patterns=raw["excluded_host_patterns"], raw=raw)


def test_discovery_run_builds_inventory_and_findings():
    pdf = [f"https://app.example.com/pdf/tool{i}" for i in range(120)]
    pdf_fr = [f"https://app.example.com/pdf/fr/tool{i}" for i in range(3)]
    routes = {
        "https://www.example.com/robots.txt": (200, b"User-agent: *\nAllow: /\n"),
        "https://www.example.com/sitemap.xml": (200, urlset("https://www.example.com/", "https://www.example.com/fr/",
                                                           alternates=True)),
        "https://app.example.com/pdf/sitemap.xml": (200, index("https://app.example.com/pdf/en.xml",
                                                               "https://app.example.com/pdf/fr.xml",
                                                               "https://app-qa.example.com/pdf/x.xml")),
        "https://app.example.com/pdf/en.xml": (200, urlset(*pdf, lastmod="2026-04-17")),
        "https://app.example.com/pdf/fr.xml": (200, urlset(*pdf_fr, pdf[0], "http://app.example.com/pdf/old")),
    }
    store = InventoryStore(":memory:")
    report = discover(make_tenant(), FakeFetcher(routes), store, log=None)

    assert report["hosts"]["app.example.com"]["urls"] == 124
    pdf_section = report["sections"]["app.example.com"]["pdf"]
    assert set(pdf_section["langs"]) == {"default", "fr"}
    assert report["gsc_comparison"][0]["live_status"] == 404

    titles = {f["title"]: f for f in report["findings"]}
    assert "robots.txt has no Sitemap: line" in titles
    assert "No robots.txt on this host" in titles                       # app host robots 404
    assert "Sitemap cannot be fetched (omr)" in titles                  # GSC-submitted, live 404
    assert "Couldn't fetch" in titles["Sitemap cannot be fetched (omr)"]["evidence"]["observed"]
    assert "No working sitemap lists any /omr/ URL" in titles
    assert "All lastmod dates are identical (bulk-stamped) (pdf)" in titles
    assert "Sitemap lists http:// URLs (pdf)" in titles
    assert "Sitemap index points outside the property or to an excluded host (pdf)" in titles
    assert "Localized /pdf/ pages have no hreflang in sitemaps" in titles
    assert titles["URLs listed in more than one sitemap (/pdf/)"]["affected_pages"] == 1
    assert all(f["evidence"]["urls"] and f["evidence"]["observed"] for f in report["findings"])
    assert titles["No robots.txt on this host"]["fix_channel"] == "git-pr"
    # www has hreflang and a working sitemap: no intl or coverage finding for it
    assert not any(f["subdomain"] == "www.example.com" and f["dimension"] == "intl" for f in report["findings"])


def test_duplicate_url_keeps_hreflang_and_findings_group_per_host():
    # www-style: one index, per-language sitemaps; the same URL appears with and without alternates.
    routes = {
        "https://www.example.com/robots.txt": (404, b""),
        "https://www.example.com/sitemap.xml": (200, index(*[f"https://www.example.com/{lg}/sitemap.xml"
                                                             for lg in ("en", "fr", "de")])),
        "https://www.example.com/en/sitemap.xml": (200, urlset("https://www.example.com/fr/",
                                                               "https://www.example.com/de/")),
        "https://www.example.com/fr/sitemap.xml": (200, urlset("https://www.example.com/fr/", alternates=True)),
        "https://www.example.com/de/sitemap.xml": (200, urlset("https://www.example.com/de/")),
    }
    store = InventoryStore(":memory:")
    report = discover(make_tenant(), FakeFetcher(routes), store, hosts=["www.example.com"], log=None)
    rows = dict(store.db.execute("SELECT url, hreflang_count FROM urls"))
    assert rows["https://www.example.com/fr/"] == 1 and rows["https://www.example.com/de/"] == 0
    lastmod = [f for f in report["findings"] if f["title"].startswith("Sitemap has no lastmod dates")]
    assert len(lastmod) == 1 and len(lastmod[0]["evidence"]["urls"]) == 4   # one finding, not one per folder
    partial = [f for f in report["findings"] if f["title"].startswith("hreflang in sitemaps covers only some")]
    assert partial and partial[0]["evidence"]["urls"] == ["https://www.example.com/de/"]


def test_double_slash_child_and_guessed_html_probe():
    routes = {
        # GSC-submitted index lists children with "//": listed URL fails, corrected URL works
        "https://app.example.com/pdf/sitemaps/pdf.xml": (200, index("https://app.example.com//pdf/sitemaps/p0.xml")),
        "https://app.example.com//pdf/sitemaps/p0.xml": (503, b""),
        "https://app.example.com/pdf/sitemaps/p0.xml": (200, urlset("https://app.example.com/pdf/a")),
        # guessed probe answers with an HTML error page: no finding
        "https://app.example.com/omr/sitemap.xml": (200, b"<html><body>error</body></html>"),
    }
    t = make_tenant()
    t.raw["gsc_sitemaps_snapshot"]["sitemaps"] = [{"path": "/pdf/sitemaps/pdf.xml", "status": "Success"}]
    report = discover(t, FakeFetcher(routes), InventoryStore(":memory:"), hosts=["app.example.com"], log=None)
    titles = {f["title"]: f for f in report["findings"]}
    bad = titles["Sitemap index lists child sitemaps with a double slash (pdf)"]
    assert bad["evidence"]["urls"] == ["https://app.example.com//pdf/sitemaps/p0.xml"] and bad["affected_pages"] == 1
    assert "HTTP 503" in bad["evidence"]["observed"]
    assert not any(k.startswith("Sitemap cannot be fetched") for k in titles)
    assert not any(k.startswith("Sitemap is not a valid sitemap") for k in titles)
    assert report["hosts"]["app.example.com"]["urls"] == 1


def test_section_filter_limits_probes():
    store = InventoryStore(":memory:")
    fetcher = FakeFetcher({})
    discover(make_tenant(), fetcher, store, hosts=["app.example.com"], sections=["pdf"], log=None)
    fetched = [r[0] for r in store.db.execute("SELECT url FROM sitemaps")]
    assert "https://app.example.com/pdf/sitemap.xml" in fetched
    assert not any("/omr/" in u for u in fetched)
