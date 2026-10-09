"""Prioritizer: attach real traffic to each finding and score it (impact x severity / effort). Deterministic.

Traffic comes from Search Console page rows (clicks, impressions; 28 days), classified into the same
(host, section, template) buckets the findings use. Hosts without a Search Console property fall back to
GA4 organic sessions per host (counted as clicks). What share of that traffic a finding puts at stake:

- crawl audit: per template, the template's traffic x the failing share of its sample (`evidence.sample`)
- discovery:   the section's (or host's) traffic x affected_pages / URLs in that section. Checks that cover a
               whole host or section (robots.txt, no working sitemap) with no page count take all of it; a
               failed sitemap file takes the URLs Search Console discovered from it (tenant snapshot), else 0
- seed:        the exact pages in `evidence.urls`

score = min(100, 20 * log10(1 + click-equivalents)) * severity / effort, with impressions counted as
IMPRESSION_WEIGHT click-equivalents each. 100k click-equivalents saturate the traffic factor.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from core.discovery.classify import LANG_CODES, classify_url, compile_templates
from core.findings import template_section

SEVERITY = {"broken": 1.0, "incorrect": 0.7, "not_optimized": 0.4, "improvable": 0.2}
EFFORT = {"S": 1.0, "M": 1.5, "L": 2.5}
# Discovery checks whose scope is the whole host/section even when no page count is attached.
WHOLE_SCOPE_CHECKS = {"robots_missing", "robots_html", "robots_no_sitemap", "robots_unreachable",
                      "host_no_sitemap", "section_no_sitemap"}
IMPRESSION_WEIGHT = 0.01  # ~1% CTR: 100 impressions count like one click
TRAFFIC_SCALE = 20  # score points per decade of click-equivalents


def page_key(url: str) -> str:
    """Join key for GSC pages and finding URLs: lower-case host, no fragment, no trailing slash."""
    p = urlsplit(url.strip())
    return urlunsplit(("https", p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))


def score(clicks: float, impressions: float, ftype: str, effort: str | None) -> int:
    value = clicks + IMPRESSION_WEIGHT * impressions
    traffic = min(100.0, TRAFFIC_SCALE * math.log10(1 + value))
    return round(traffic * SEVERITY.get(ftype, 0.2) / EFFORT.get(effort or "S", 1.0))


@dataclass
class Traffic:
    clicks: float = 0.0
    impressions: float = 0.0
    pages: int = 0

    def add(self, clicks: float, impressions: float) -> None:
        self.clicks += clicks
        self.impressions += impressions
        self.pages += 1


@dataclass
class TrafficIndex:
    """Traffic per bucket. Keys: (host, section, template), (host, section, None), (host, None, None)."""

    buckets: dict = field(default_factory=lambda: defaultdict(Traffic))
    pages: dict = field(default_factory=dict)  # page_key -> (clicks, impressions)
    gsc_hosts: set = field(default_factory=set)  # hosts covered by a Search Console property
    ga4_organic: dict = field(default_factory=dict)  # host -> organic sessions (fallback)
    window: tuple | None = None

    @classmethod
    def build(cls, tenant, gsc_rows: list[dict], ga4_hosts: dict | None = None, window=None) -> TrafficIndex:
        """gsc_rows: [{'page': url, 'clicks': n, 'impressions': n}] across the tenant's properties."""
        raw = tenant.raw
        sectioned = set((raw.get("discovery") or {}).get("section_sitemaps") or {})
        templates = {h: compile_templates(s) for h, s in (raw.get("url_templates") or {}).items()}
        langs = frozenset(raw["languages"]) if raw.get("languages") else LANG_CODES
        idx = cls(window=window, ga4_organic=dict(ga4_hosts or {}))
        for site in tenant.gsc_properties:
            if not site.startswith("sc-domain:"):
                idx.gsc_hosts.add(urlsplit(site).netloc.lower())
            else:
                domain = site.removeprefix("sc-domain:")
                idx.gsc_hosts.update(h for h in tenant.host_names if h == domain or h.endswith("." + domain))
        for r in gsc_rows:
            url, clicks, impr = r["page"], r.get("clicks", 0), r.get("impressions", 0)
            host = urlsplit(url).netloc.lower()
            c = classify_url(url, templates.get(host, ()), langs)
            sec = c.section if host in sectioned else ""
            for key in ((host, sec, c.template), (host, sec, None), (host, None, None)):
                idx.buckets[key].add(clicks, impr)
            k = page_key(url)
            old = idx.pages.get(k, (0, 0))
            idx.pages[k] = (old[0] + clicks, old[1] + impr)
        return idx

    def get(self, host: str, section: str | None = None, template: str | None = None) -> Traffic:
        return self.buckets.get((host, section, template), Traffic())


def finding_traffic(f: dict, idx: TrafficIndex, section_urls: dict | None = None,
                    sitemap_discovered: dict | None = None) -> dict:
    """Traffic a finding puts at stake: {'clicks_28d', 'impressions_28d', 'source', 'share'}."""
    host = f["subdomain"]
    section = template_section(f["template"]) or ""
    source_kind = f.get("source") or ("seed" if str(f["id"]).startswith("seed-") else "code")
    section_urls = section_urls or {}

    if host not in idx.gsc_hosts:
        sessions = idx.ga4_organic.get(host)
        if sessions is None:
            return {"clicks_28d": None, "impressions_28d": None, "source": "none", "share": None}
        share = _share(f, section_urls.get((host, None)), sitemap_discovered)
        return {"clicks_28d": round(sessions * share), "impressions_28d": None, "source": "ga4-host",
                "share": round(share, 3)}

    if source_kind == "seed":
        clicks = impr = 0.0
        for u in f["evidence"]["urls"]:
            c, i = idx.pages.get(page_key(u), (0, 0))
            clicks, impr = clicks + c, impr + i
        return {"clicks_28d": round(clicks), "impressions_28d": round(impr), "source": "gsc-pages", "share": None}

    sample = (f.get("evidence") or {}).get("sample")
    if sample:  # crawl audit: {template: [failed, sampled]}
        clicks = impr = 0.0
        for template, (failed, sampled) in sample.items():
            t = idx.get(host, section, template)
            share = failed / sampled if sampled else 0
            clicks, impr = clicks + t.clicks * share, impr + t.impressions * share
        return {"clicks_28d": round(clicks), "impressions_28d": round(impr), "source": "gsc-template",
                "share": None}

    # discovery: the section's traffic (or the host's, for host-level findings) x affected share
    sec = template_section(f["template"])
    if sec is None:
        t, kind, total = idx.get(host), "gsc-host", section_urls.get((host, None))
    else:
        t, kind, total = idx.get(host, sec), "gsc-section", section_urls.get((host, sec))
    share = _share(f, total, sitemap_discovered)
    return {"clicks_28d": round(t.clicks * share), "impressions_28d": round(t.impressions * share),
            "source": kind, "share": round(share, 3)}


def _share(f: dict, total_urls: int | None, sitemap_discovered: dict | None = None) -> float:
    pages = f.get("affected_pages") or 0
    if not pages and f.get("check") not in WHOLE_SCOPE_CHECKS and f.get("source") != "crawl-audit":
        # a failed sitemap file: the URLs Search Console discovered from it, if known
        pages = sum((sitemap_discovered or {}).get(u, 0) for u in f["evidence"]["urls"])
        if not pages:
            return 0.0
    if not pages or not total_urls:
        return 1.0
    return min(1.0, pages / total_urls)


def snapshot_discovered(tenant) -> dict:
    """Sitemap URL -> URLs Search Console discovered from it, from the tenant's GSC sitemaps snapshot."""
    snap = tenant.raw.get("gsc_sitemaps_snapshot") or {}
    base = (snap.get("property") or "").rstrip("/")
    return {base + s["path"]: s.get("discovered") or 0 for s in snap.get("sitemaps") or []}


def prioritize(findings: list[dict], idx: TrafficIndex, section_urls: dict | None = None,
               sitemap_discovered: dict | None = None) -> dict:
    """finding id -> impact dict (clicks_28d, impressions_28d, score, source, share, window)."""
    out = {}
    for f in findings:
        t = finding_traffic(f, idx, section_urls, sitemap_discovered)
        s = score(t["clicks_28d"] or 0, t["impressions_28d"] or 0, f["type"], f.get("effort"))
        out[f["id"]] = {**t, "score": s, "window": list(idx.window) if idx.window else None}
    return out
