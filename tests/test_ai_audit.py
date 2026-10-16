"""AI-readiness auditor: robots.txt rules (RFC 9309), firewall probe, llms.txt coverage, page checks. Offline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit.ai import AiAudit, import_report, llms_state, parse_robots, robots_allows  # noqa: E402
from core.audit.crawl import page_issues  # noqa: E402
from core.audit.page import parse_page  # noqa: E402
from core.audit.store import AuditStore  # noqa: E402
from core.discovery.fetch import FetchResult  # noqa: E402
from core.findings import FindingsStore  # noqa: E402
from core.prioritize import TrafficIndex, finding_traffic  # noqa: E402
from core.scorecard import family_of, group_of  # noqa: E402
from core.tenant import Tenant  # noqa: E402

LLMS = b"# Example\n\n> Free apps.\n\n## Apps\n\n- [PDF](https://app.example.com/pdf/): PDF tools\n"


def make_tenant(skip=()):
    raw = {
        "property": "example.com", "excluded_host_patterns": ["*-qa.*"],
        "hosts": [{"host": "www.example.com"}, {"host": "app.example.com", "fix_channel": "git-pr"}],
        "discovery": {"section_sitemaps": {"app.example.com": {"sections": ["pdf", "omr", "zip"], "paths": []}}},
        "audit": {"ai": {"llms_txt_skip_hosts": list(skip)}},
    }
    return Tenant(property="example.com", gsc_properties=["https://app.example.com/"], ga4_property_id=None,
                  hosts=raw["hosts"], excluded_host_patterns=raw["excluded_host_patterns"], raw=raw)


class FakeFetcher:
    """url -> (status, content type, body); `bots` maps a user-agent fragment to a status the CDN returns."""

    def __init__(self, pages, robots=None, bots=None):
        self.pages, self.robots_txt, self.bots, self.requests = pages, robots or {}, bots or {}, 0

    def robots(self, origin):
        host = origin.split("//")[1]
        if host in self.robots_txt:
            return FetchResult(f"{origin}/robots.txt", 200, content_type="text/plain",
                               body=self.robots_txt[host].encode())
        return FetchResult(f"{origin}/robots.txt", 404, content_type="text/html")

    def get(self, url, headers=None):
        self.requests += 1
        ua = (headers or {}).get("User-Agent", "")
        for frag, status in self.bots.items():
            if frag in ua:
                return FetchResult(url, status, final_url=url, content_type="text/html", body=b"denied")
        status, ctype, body = self.pages.get(url, (404, "text/html", b"<html>not found</html>"))
        return FetchResult(url, status, final_url=url, content_type=ctype, body=body)


HOME = (200, "text/html", b"<html><body>" + b"welcome " * 200 + b"</body></html>")


def run(fetcher, tenant=None):
    return AiAudit(tenant or make_tenant(), fetcher, AuditStore(":memory:"), log=None).run()


def by_check(report):
    return {(f["check"], f["subdomain"], f["template"]): f for f in report["findings"]}


# ---- robots.txt ----

def test_robots_rfc9309_groups_and_longest_match():
    groups = parse_robots("User-agent: *\nDisallow: /private/\n\n"
                          "User-agent: GPTBot\nUser-agent: OAI-SearchBot\nDisallow: /\nAllow: /pdf/\n\n"
                          "User-agent: PerplexityBot\nDisallow: /*.pdf$\nDisallow:\n")
    assert robots_allows(groups, "Claude-SearchBot", "/")
    assert not robots_allows(groups, "Claude-SearchBot", "/private/x")
    assert not robots_allows(groups, "oai-searchbot", "/") and robots_allows(groups, "OAI-SearchBot", "/pdf/")
    assert robots_allows(groups, "PerplexityBot", "/private/")  # a named group replaces '*' entirely
    assert not robots_allows(groups, "PerplexityBot", "/a/file.pdf")
    assert robots_allows(groups, "PerplexityBot", "/a/file.pdfx")
    assert robots_allows(parse_robots("User-agent: *\nDisallow: /x\nAllow: /x\n"), "Bingbot", "/x")  # allow wins tie


def test_answer_bot_block_is_a_finding_training_bot_block_is_not():
    pages = {"https://www.example.com/": HOME, "https://app.example.com/": HOME}
    robots = {"www.example.com": "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n",
              "app.example.com": "User-agent: PerplexityBot\nDisallow: /omr/\n"}
    report = run(FakeFetcher(pages, robots))
    f = by_check(report)
    assert ("ai_bot_blocked", "www.example.com", "robots.txt") not in f
    assert report["summary"]["www.example.com"]["training_blocked"] == ["CCBot", "GPTBot"]
    blocked = f[("ai_bot_blocked", "app.example.com", "robots.txt")]
    assert blocked["type"] == "broken" and "PerplexityBot on /omr/" in blocked["evidence"]["observed"]
    assert family_of(blocked, "app.example.com", {"pdf", "omr", "zip"}) is None  # host-level, not a family's


# ---- firewall ----

def test_firewall_probe_flags_answer_bots_only():
    pages = {"https://www.example.com/": HOME, "https://app.example.com/": HOME}
    report = run(FakeFetcher(pages, bots={"PerplexityBot": 403, "GPTBot": 403}))
    f = by_check(report)
    fw = f[("ai_bot_firewalled", "www.example.com", "host")]
    assert "PerplexityBot: HTTP 403" in fw["evidence"]["observed"] and "GPTBot" not in fw["evidence"]["observed"]
    assert report["summary"]["www.example.com"]["firewall_training"] == {"GPTBot": "HTTP 403"}  # information
    assert not run(FakeFetcher(pages))["summary"]["app.example.com"]["firewall_blocked"]


# ---- llms.txt ----

def test_llms_state():
    def res(status, ctype="text/plain", body=b"", final=None):
        return FetchResult("https://h/llms.txt", status, final_url=final or "https://h/llms.txt", content_type=ctype,
                           body=body)
    assert llms_state(res(404), "https://h/llms.txt")[0] == "missing"
    assert llms_state(res(403), "https://h/llms.txt") == ("missing", "HTTP 403")  # no file, whatever the reason
    assert llms_state(res(200, "text/html", b"<!DOCTYPE html><p>"), "https://h/llms.txt")[0] == "missing"
    assert llms_state(res(200, body=b"  \n"), "https://h/llms.txt") == ("invalid", "empty file")
    assert llms_state(res(200, body=b"\xef\xbb\xbfjust words"), "https://h/llms.txt")[1].startswith("no H1")
    assert llms_state(res(200, body=LLMS, final="https://h/"), "https://h/llms.txt") == ("missing", "redirects to https://h/")
    assert llms_state(res(200, "text/markdown", LLMS), "https://h/llms.txt")[0] == "ok"
    assert llms_state(FetchResult("u", None, error="Timeout"), "https://h/llms.txt")[0] == "error"


def test_no_host_file_gives_one_host_finding_listing_sections():
    pages = {"https://www.example.com/": HOME, "https://app.example.com/": HOME,
             "https://app.example.com/pdf/llms.txt": (200, "text/plain", LLMS),
             "https://app.example.com/zip/llms.txt": (None, None, b""),  # timeout
             "https://app.example.com/pdf/": HOME,
             "https://www.example.com/llms.txt": (200, "text/plain",
                                                    LLMS.replace(b"app.example.com/pdf/", b"www.example.com/gone"))}
    report = run(FakeFetcher(pages))
    f = by_check(report)
    host = f[("llms_txt_missing", "app.example.com", "llms.txt")]
    assert host["evidence"]["observed"].endswith(
        "1 of 3 sections have no llms.txt of their own (omr); own file: pdf; not checked (no response): zip")
    assert family_of(host, "app.example.com", {"pdf", "omr", "zip"}) is None
    assert not [k for k in f if k[2].startswith("llms.txt:")]  # no per-family findings while the host has none
    www = f[("llms_txt_invalid", "www.example.com", "llms.txt")]  # its only link fails
    assert "1 of 1 links checked fail: https://www.example.com/gone (HTTP 404)" in www["evidence"]["observed"]
    assert report["summary"]["app.example.com"]["sections_covered"] == 1


def test_host_file_then_uncovered_families_get_their_own_finding():
    root = LLMS + b"- [OMR](/omr/recognize): OMR\n"
    pages = {"https://app.example.com/": HOME, "https://www.example.com/": HOME,
             "https://app.example.com/llms.txt": (200, "text/plain", root),
             "https://app.example.com/pdf/": HOME, "https://app.example.com/omr/recognize": HOME,
             "https://app.example.com/zip/llms.txt": (200, "text/html", b"<html>"),
             "https://app.example.com/pdf/llms.txt": (200, "text/plain", b"PDF tools, no heading")}
    report = run(FakeFetcher(pages), make_tenant(skip=["www.example.com"]))
    f = by_check(report)
    assert ("llms_txt_missing", "app.example.com", "llms.txt") not in f
    assert ("llms_txt_missing", "app.example.com", "llms.txt:pdf") not in f  # linked from the host file
    assert ("llms_txt_missing", "app.example.com", "llms.txt:omr") not in f
    assert "no H1 line" in f[("llms_txt_invalid", "app.example.com", "llms.txt:pdf")]["evidence"]["observed"]
    zip_ = f[("llms_txt_missing", "app.example.com", "llms.txt:zip")]  # an app page at the URL: no file
    assert family_of(zip_, "app.example.com", {"pdf", "omr", "zip"}) == "zip" and group_of(zip_) == "ai"
    assert report["summary"]["www.example.com"]["llms"]["state"] == "skipped"
    assert not [k for k in f if k[1] == "www.example.com"]


def test_findings_resolve_and_get_host_traffic():
    pages = {"https://www.example.com/": HOME, "https://app.example.com/": HOME}
    store = FindingsStore(":memory:")
    first = run(FakeFetcher(pages))
    import_report(store, first)
    assert {f["check"] for f in store.findings()} == {"llms_txt_missing"}
    pages["https://www.example.com/llms.txt"] = (200, "text/plain", b"# Ex\n\n- [Home](https://www.example.com/)\n")
    changes = import_report(store, run(FakeFetcher(pages)))
    assert len(changes["resolved"]) == 1
    f = next(x for x in first["findings"] if x["subdomain"] == "app.example.com")
    rows = [{"page": "https://app.example.com/pdf/a", "clicks": 90, "impressions": 900}]
    idx = TrafficIndex.build(make_tenant(), rows)
    assert finding_traffic({**f, "source": "ai-audit"}, idx)["clicks_28d"] == 90  # whole host at stake


# ---- page checks (crawl auditor) ----

def page_rec(url="https://app.example.com/pdf/merge"):
    return {"url": url, "status": 200, "final_url": url, "redirects": [], "redirect_statuses": [], "blocked": 0,
            "error": None, "source": "sitemap", "lang": None, "content_type": "text/html"}


def test_page_text_without_js_and_structured_data():
    shell = (b'<html lang="en"><head><title>Merge PDF files online free</title>'
             b'<script type="application/ld+json">{"@context":"https://schema.org","@graph":'
             b'[{"@type":"WebApplication"},{"@type":["HowTo","Thing"]}]}</script>'
             b'<script type="application/ld+json">{broken</script></head><body><nav>' + b"menu " * 100
             + b'</nav><div id="app"></div><script>' + b"x" * 5000 + b"</script><footer>(c) Example</footer>"
             b"</body></html>")
    facts = parse_page(shell, "https://app.example.com/pdf/merge")
    assert facts.text_chars == 0 and facts.jsonld_types == ["WebApplication", "HowTo", "Thing"]
    assert facts.jsonld_invalid == 1
    issues = dict(page_issues(page_rec(), facts, localized=False))
    assert issues["content_needs_js"] == "0 characters of main text in the HTML"
    assert "no_structured_data" not in issues

    full = (b"<html><head><title>t</title></head><body><main>" + b"Merge your PDF files. " * 30
            + b"</main></body></html>")
    issues = dict(page_issues(page_rec(), parse_page(full, "https://app.example.com/pdf/merge"), localized=False))
    assert "content_needs_js" not in issues and issues["no_structured_data"] == "no JSON-LD"
    noindex = full.replace(b"<title>", b'<meta name="robots" content="noindex"><title>')
    issues = dict(page_issues(page_rec(), parse_page(noindex, "https://app.example.com/pdf/merge"), localized=False))
    assert "no_structured_data" not in issues
