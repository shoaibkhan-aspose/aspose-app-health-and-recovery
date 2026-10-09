"""Crawl auditor: fetch a sample of pages per template and check status, redirects, indexability, canonical,
hreflang and the basic head tags (title, meta description, H1, html lang, viewport).

All checks are deterministic code. Pages come from a discovery inventory run (see core.audit.sample), plus
each host's homepage and the tenant's `audit.extra_urls`. Findings are grouped like discovery findings: per
host, or per section on hosts with configured sections, so one template fix = one finding. Each finding says
how many sampled pages failed per template; `affected_pages` extrapolates that share to the inventory.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit

from core.audit.page import parse_page
from core.audit.sample import SampledUrl, group_langs, sample_inventory
from core.discovery.classify import LANG_CODES, classify_url, compile_templates
from core.findings import FindingIds, make_finding

AUDITOR = "crawl-audit"
EXAMPLES = 5
MAX_CANONICAL_TARGETS = 50  # distinct canonical targets fetched per run, to show whether they exist
TITLE_LEN = (10, 70)  # display width (CJK characters count 2); outside this range is a finding
DESCRIPTION_LEN = (50, 170)
REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)
HREFLANG_RE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)
SOFT_404_URL_RE = re.compile(r"(^|[/?&=_.-])(404|not-?found|notfound|error)([/?&=_.-]|$)", re.I)
SOFT_404_TITLE_RE = re.compile(r"\b(404|page not found|not found)\b", re.I)
NO_ZOOM_RE = re.compile(r"user-scalable\s*=\s*(no|0)\b|maximum-scale\s*=\s*1(\.0*)?\b", re.I)

# check -> (dimension, type, title, expected, fix_tier, effort)
CHECKS = {
    "fetch_error": ("crawl", "broken", "Pages fail to load (network error or timeout)", "HTTP 200", 1, "S"),
    "http_error": ("crawl", "broken", "Pages listed in sitemaps return an HTTP error", "HTTP 200", 1, "S"),
    "robots_blocked": ("crawl", "incorrect", "Sitemaps list pages that robots.txt blocks",
                       "sitemaps list only crawlable pages", 3, "S"),
    "redirected": ("crawl", "not_optimized", "Sitemaps list URLs that redirect",
                   "sitemaps list final URLs that answer HTTP 200", 1, "S"),
    "meta_refresh": ("crawl", "not_optimized", "Sitemaps list meta-refresh redirect pages",
                     "sitemaps list the target URL; moved pages use an HTTP 301", 1, "S"),
    "redirect_chain": ("crawl", "not_optimized", "Redirect chains (2+ hops)", "a single redirect hop", 2, "S"),
    "redirect_temporary": ("crawl", "not_optimized", "Pages redirect with a temporary status (302/307)",
                           "permanent redirects (301/308) for moved URLs", 2, "S"),
    "soft_404": ("crawl", "broken", "Error page served with HTTP 200 (soft 404)",
                 "HTTP 404/410 for missing pages", 2, "M"),
    "noindex": ("crawl", "incorrect", "Pages listed in sitemaps (or homepages) are noindex",
                "only indexable pages in sitemaps; homepages indexable", 3, "S"),
    "canonical_missing": ("crawl", "improvable", "Pages have no canonical tag",
                          "a self-referencing absolute canonical", 2, "S"),
    "canonical_multiple": ("crawl", "incorrect", "Pages declare more than one canonical URL",
                           "exactly one canonical", 2, "S"),
    "canonical_elsewhere": ("crawl", "incorrect", "Sitemap URLs canonicalize to a different URL",
                            "sitemaps list the canonical URL; canonical points to the page itself", 2, "S"),
    "title_missing": ("onpage", "broken", "Pages have no <title>", "a unique, descriptive title", 1, "S"),
    "title_multiple": ("onpage", "incorrect", "Pages have more than one <title>", "one <title> in <head>", 1, "S"),
    "title_length": ("onpage", "improvable", "Titles are too long or too short",
                     f"{TITLE_LEN[0]}-{TITLE_LEN[1]} characters (CJK count double)", 1, "S"),
    "title_duplicate": ("onpage", "not_optimized", "Different pages share the same title",
                        "a unique title per page and language", 1, "M"),
    "description_missing": ("onpage", "improvable", "Pages have no meta description",
                            "a unique meta description", 1, "S"),
    "description_length": ("onpage", "improvable", "Meta descriptions are too long or too short",
                           f"{DESCRIPTION_LEN[0]}-{DESCRIPTION_LEN[1]} characters (CJK count double)", 1, "S"),
    "description_duplicate": ("onpage", "not_optimized", "Different pages share the same meta description",
                              "a unique meta description per page and language", 1, "M"),
    "h1_missing": ("onpage", "improvable", "Pages have no H1", "one H1 that states the page topic", 1, "S"),
    "h1_multiple": ("onpage", "improvable", "Pages have more than one H1", "one H1 per page", 1, "S"),
    "hreflang_missing": ("intl", "not_optimized", "Localized pages have no hreflang tags in page HTML",
                         "hreflang alternates for every language version, incl. a self-reference and x-default",
                         2, "M"),
    "hreflang_no_self": ("intl", "incorrect", "hreflang set does not include the page itself",
                         "a self-referencing hreflang entry", 2, "S"),
    "hreflang_invalid": ("intl", "incorrect", "Invalid hreflang codes",
                         "ISO 639-1 language (+ optional ISO 3166-1 region) or x-default", 2, "S"),
    "hreflang_conflict": ("intl", "incorrect", "hreflang lists one language code with different URLs",
                          "one URL per language code", 2, "S"),
    "head_tags_repeated": ("crawl", "improvable", "Canonical/hreflang tags are emitted more than once",
                           "each head tag emitted once", 2, "S"),
    "hreflang_no_xdefault":("intl", "improvable", "hreflang set has no x-default", "an x-default entry", 2, "S"),
    "html_lang_missing": ("intl", "improvable", "Pages have no <html lang> attribute",
                          "<html lang> matching the page language", 1, "S"),
    "html_lang_mismatch": ("intl", "incorrect", "<html lang> does not match the language in the URL",
                           "<html lang> = the URL's language", 1, "S"),
    "viewport_missing": ("ux", "not_optimized", "Pages have no meta viewport",
                         "<meta name=viewport content='width=device-width, initial-scale=1'>", 1, "S"),
    "viewport_no_zoom": ("ux", "not_optimized", "Viewport disables zoom (user-scalable=no or maximum-scale=1)",
                         "zoom allowed (WCAG 1.4.4)", 1, "S"),
}


def norm_url(url: str) -> str:
    """Lower-case scheme and host, drop the fragment. Path and query are compared exactly."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def display_len(text: str) -> int:
    """Approximate rendered width: East Asian wide/full-width characters count 2, as SERP pixel limits do."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _is_html(rec: dict) -> bool:
    return "html" in (rec.get("content_type") or "").lower()


def page_issues(rec: dict, facts, localized: bool) -> list[tuple[str, str]]:
    """Per-page checks -> [(check, note)]. Duplicate titles/descriptions are checked across pages later."""
    issues: list[tuple[str, str]] = []
    listed = rec["source"] == "sitemap"
    if rec["blocked"]:
        return [("robots_blocked", "disallowed by robots.txt")] if listed else []
    if rec["status"] is None:
        return [("fetch_error", rec["error"] or "no response")]

    hops = list(zip(rec["redirect_statuses"], rec["redirects"], strict=False))
    if hops:
        chain = " -> ".join(f"{s}" for s, _ in hops) + f" -> {rec['final_url']}"
        if listed:
            issues.append(("redirected", f"redirects {chain}"))
        if len(hops) >= 2:
            issues.append(("redirect_chain", f"{len(hops)} hops: {chain}"))
        temporary = [s for s in rec["redirect_statuses"] if s in (302, 303, 307)]
        if temporary:
            issues.append(("redirect_temporary", f"HTTP {temporary[0]} -> {rec['final_url']}"))
    if rec["status"] >= 400:
        return [*issues, ("http_error", f"HTTP {rec['status']}" + (f" at {rec['final_url']}" if hops else ""))]
    if rec["status"] != 200 or facts is None:
        return issues

    title = facts.titles[0] if facts.titles else ""
    final_path = urlsplit(rec["final_url"] or rec["url"])
    if (hops and SOFT_404_URL_RE.search(final_path.path + "?" + final_path.query)) or SOFT_404_TITLE_RE.search(title):
        issues.append(("soft_404", f"HTTP 200 at {rec['final_url']}, title '{title[:80]}'"))
        return issues  # an error page's other tags say nothing about the template

    if facts.refresh and REFRESH_URL_RE.search(facts.refresh):
        target = REFRESH_URL_RE.search(facts.refresh).group(1).strip()
        if listed:
            issues.append(("meta_refresh", f"meta refresh to {target}"))
        return issues  # a redirect stub's own tags say nothing about the template
    if facts.noindex:
        issues.append(("noindex", f"robots: {'; '.join(facts.robots)}"))
    here = norm_url(rec["final_url"] or rec["url"])
    canon = list(dict.fromkeys(norm_url(c) for c in facts.canonicals))
    if not canon:
        issues.append(("canonical_missing", "no <link rel=canonical> and no Link header"))
    elif len(canon) > 1:
        issues.append(("canonical_multiple", ", ".join(canon[:3])))
    elif canon[0] != here and not facts.noindex:
        issues.append(("canonical_elsewhere", f"canonical {canon[0]}"))

    if not facts.titles:
        issues.append(("title_missing", "no <title> in <head>"))
    else:
        if len(facts.titles) > 1:
            issues.append(("title_multiple", " | ".join(facts.titles[:3])))
        if not TITLE_LEN[0] <= display_len(title) <= TITLE_LEN[1]:
            issues.append(("title_length", f"width {display_len(title)}: '{title[:90]}'"))
    desc = facts.descriptions[0] if facts.descriptions else ""
    if not desc:
        issues.append(("description_missing", "no meta description" if not facts.descriptions else "empty"))
    elif not DESCRIPTION_LEN[0] <= display_len(desc) <= DESCRIPTION_LEN[1]:
        issues.append(("description_length", f"width {display_len(desc)}: '{desc[:90]}'"))
    h1 = [h for h in facts.h1 if h]
    if not h1:
        issues.append(("h1_missing", "no non-empty <h1>"))
    elif len(h1) > 1:
        issues.append(("h1_multiple", f"{len(h1)} H1s: " + " | ".join(h[:40] for h in h1[:3])))

    if facts.hreflang:
        codes = [c for c, _ in facts.hreflang]
        bad = sorted({c for c in codes if not HREFLANG_RE.match(c)})
        if bad:
            issues.append(("hreflang_invalid", ", ".join(bad[:5])))
        # x-default does not count as the self-reference: the page needs an entry for its own language
        targets = {norm_url(h) for c, h in facts.hreflang if c.lower() != "x-default"}
        if here not in targets and not (canon and canon[0] in targets):
            own = sorted({c for c in set(codes) if c.lower() != "x-default"})
            issues.append(("hreflang_no_self", f"{len(own)} languages ({', '.join(own[:8])}"
                                               f"{', ...' if len(own) > 8 else ''}), none points to {here}"))
        by_code: dict = defaultdict(set)
        for c, h in facts.hreflang:
            by_code[c.lower()].add(norm_url(h))
        conflicts = sorted(c for c, hs in by_code.items() if len(hs) > 1)
        if conflicts:
            issues.append(("hreflang_conflict", f"codes with 2+ URLs: {', '.join(conflicts[:5])}"))
    if facts.head_canonicals > 1 or len(facts.hreflang) > len(set(facts.hreflang)):
        issues.append(("head_tags_repeated", f"{facts.head_canonicals} canonical tags, {len(facts.hreflang)} "
                                             f"hreflang tags for {len(set(facts.hreflang))} distinct"))
    if facts.hreflang:
        if not any(c.lower() == "x-default" for c in codes):
            issues.append(("hreflang_no_xdefault", f"{len(codes)} entries: {', '.join(codes[:6])}"))
    elif localized and not facts.noindex:
        issues.append(("hreflang_missing", "no <link rel=alternate hreflang> in <head>"))

    if not facts.html_lang:
        issues.append(("html_lang_missing", "no lang attribute on <html>"))
    elif rec["lang"] and facts.html_lang.lower().split("-")[0] != rec["lang"].split("-")[0]:
        issues.append(("html_lang_mismatch", f"URL language {rec['lang']}, <html lang='{facts.html_lang}'>"))

    if facts.viewport is None:
        issues.append(("viewport_missing", "no meta viewport"))
    elif NO_ZOOM_RE.search(facts.viewport):
        issues.append(("viewport_no_zoom", facts.viewport))
    return issues


def duplicate_issues(pages: list[dict], field: str, check: str) -> None:
    """Add `check` to pages that share a title/description with another page of the same host and language.
    Pages that canonicalize to the same URL are not duplicates of each other."""
    seen: dict = defaultdict(list)
    for p in pages:
        f = p.get("facts")
        if not f or p["status"] != 200 or any(c == "soft_404" for c, _ in p["issues"]) or not f[field]:
            continue
        value = f[field][0]
        target = norm_url(f["canonicals"][0]) if f["canonicals"] else norm_url(p["final_url"] or p["url"])
        seen[(p["host"], p["lang"] or "", value)].append((p, target))
    for (_, _, value), items in seen.items():
        if len({t for _, t in items}) < 2:
            continue
        for p, target in items:
            others = [q["url"] for q, t in items if t != target][:2]
            if others:
                p["issues"].append((check, f"'{value[:60]}' also on {', '.join(others)}"))


class CrawlAudit:
    def __init__(self, tenant, fetcher, inventory_db, inventory_run: int, store, hosts=None, sections=None,
                 per_template=2, other_langs=2, max_per_host=None, max_pages=None, search_pages=None, log=print):
        self.tenant = tenant
        self.fetcher = fetcher
        self.inventory_db = inventory_db
        self.inventory_run = inventory_run
        self.store = store
        self.hosts = [h for h in tenant.host_names if not hosts or h in hosts]
        self.sections = sections
        self.per_template, self.other_langs, self.max_per_host = per_template, other_langs, max_per_host
        self.max_pages = max_pages
        self.search_pages = search_pages or []  # [{'page', 'clicks'}]: fills sections that no sitemap covers
        self.log = log or (lambda *_: None)
        raw = tenant.raw
        self.sectioned = set((raw.get("discovery") or {}).get("section_sitemaps") or {})
        self.templates = {h: compile_templates(specs) for h, specs in (raw.get("url_templates") or {}).items()}
        self.lang_codes = frozenset(raw["languages"]) if raw.get("languages") else LANG_CODES
        self.fix_channels = {h["host"]: h.get("fix_channel") for h in tenant.hosts}
        self.audit_cfg = raw.get("audit") or {}
        self.pages: list[dict] = []
        self.run_id: int | None = None
        self.stopped_early = False
        self.target_status: dict = {}  # canonical target -> 'HTTP 404' / 'HTTP 200' / error
        self._parents: dict = {}

    def _section(self, host: str, section: str) -> str:
        return section if host in self.sectioned else ""

    def _sections_for(self, host: str) -> list[str]:
        cfg = ((self.tenant.raw.get("discovery") or {}).get("section_sitemaps") or {}).get(host) or {}
        src = cfg.get("sections") or []
        return [str(x) for x in (self.tenant.raw.get(src, []) if isinstance(src, str) else src)]

    def _classified(self, url: str, source: str) -> SampledUrl:
        host = urlsplit(url).netloc.lower()
        c = classify_url(url, self.templates.get(host, ()), self.lang_codes)
        return SampledUrl(url, host, self._section(host, c.section), c.lang, c.template, 1, source)

    def targets(self) -> list[SampledUrl]:
        sample = sample_inventory(self.inventory_db, self.inventory_run, self.hosts, self.sectioned, self.sections,
                                  self.per_template, self.other_langs, self.audit_cfg.get("default_lang", "en"),
                                  self.max_per_host, self.audit_cfg.get("per_template_by_host"))
        seen = {s.url for s in sample}
        extra = [f"https://{h}/" for h in self.hosts]  # homepages always, also with a section filter
        for host, paths in (self.audit_cfg.get("section_urls") or {}).items():
            if host in self.hosts:  # pages per section that no sitemap lists (e.g. hub pages)
                extra += [f"https://{host}{path.format(section=sec)}" for sec in self._sections_for(host)
                          for path in paths]
        extra += [u for u in self.audit_cfg.get("extra_urls") or []
                  if urlsplit(u).netloc.lower() in self.hosts and not self.tenant.is_excluded(urlsplit(u).netloc)]
        for url in extra:
            if url not in seen:
                seen.add(url)
                c = self._classified(url, "homepage" if urlsplit(url).path in ("", "/") else "extra")
                if self.sections is None or not c.section or c.section in self.sections:
                    sample.append(c)
        for t in self._search_fill({(s.host, s.section) for s in sample if s.source == "sitemap"}):
            if t.url not in seen:
                seen.add(t.url)
                sample.append(t)
        return sample

    def _search_fill(self, covered: set) -> list[SampledUrl]:
        """Sections with no sitemap URLs: sample their top search pages per template instead (source 'search')."""
        missing = {(h, sec) for h in self.hosts if h in self.sectioned for sec in self._sections_for(h)
                   if (self.sections is None or sec in self.sections) and (h, sec) not in covered}
        if not missing or not self.search_pages:
            return []
        groups: dict = defaultdict(list)
        for r in self.search_pages:
            c = self._classified(r["page"], "search")
            if (c.host, c.section) in missing:
                groups[(c.host, c.section, c.template)].append((r.get("clicks", 0), c))
        out = []
        for (_host, _sec, _tmpl), items in sorted(groups.items()):
            items.sort(key=lambda x: (-x[0], x[1].url))
            out += [SampledUrl(c.url, c.host, c.section, c.lang, c.template, len(items), "search")
                    for _, c in items[: self.per_template + self.other_langs]]
        return out

    def _fetch(self, t: SampledUrl) -> dict:
        res = self.fetcher.get(t.url)
        rec = {
            "url": t.url, "host": t.host, "section": t.section, "lang": t.lang, "template": t.template,
            "source": t.source, "group_urls": t.group_urls, "listed_in": t.sitemap,
            "status": res.status, "final_url": res.final_url,
            "redirects": res.redirects, "redirect_statuses": res.redirect_statuses, "content_type": res.content_type,
            "bytes": len(res.body), "truncated": int(res.truncated), "blocked": int(res.blocked_by_robots),
            "error": res.error, "elapsed_ms": res.elapsed_ms, "facts": None,
        }
        facts = None
        if res.status == 200 and _is_html(rec):
            facts = parse_page(res.body, res.final_url or t.url, res.headers)
            rec["facts"] = facts.to_dict()
        rec["ignored"] = self._ignored(rec)
        rec["issues"] = [] if rec["ignored"] else page_issues(rec, facts, self._localized(t.host, t.section))
        return rec

    def _ignored(self, rec: dict) -> str | None:
        """Reason if the page matches a tenant `audit.ignore_pages` rule (host, status, path_prefix), else None."""
        path = urlsplit(rec["url"]).path
        for rule in self.audit_cfg.get("ignore_pages") or []:
            if (rule.get("host") in (None, rec["host"]) and rule.get("status") in (None, rec["status"])
                    and path.startswith(rule.get("path_prefix", "/"))):
                return rule.get("reason") or "ignored by tenant rule"
        return None

    def _check_canonical_targets(self) -> None:
        """Fetch each distinct foreign canonical target once and add its status to the issue notes."""
        targets = []
        for p in self.pages:
            if any(c == "canonical_elsewhere" for c, _ in p["issues"]):
                t = norm_url(p["facts"]["canonicals"][0])
                if t not in targets:
                    targets.append(t)
        for t in targets[:MAX_CANONICAL_TARGETS]:
            res = self.fetcher.get(t)
            status = f"HTTP {res.status}" if res.status else (res.error or "no response")
            if res.redirects and res.final_url:
                status += f" via redirect to {res.final_url}"
            self.target_status[t] = status
        for p in self.pages:
            p["issues"] = [(c, f"{n} ({self.target_status[norm_url(p['facts']['canonicals'][0])]})")
                           if c == "canonical_elsewhere" and norm_url(p["facts"]["canonicals"][0]) in self.target_status
                           else (c, n) for c, n in p["issues"]]

    def _parent(self, sitemap: str) -> str | None:
        """The sitemap index that lists `sitemap`, from the inventory."""
        if sitemap not in self._parents:
            row = self.inventory_db.execute("SELECT parent FROM sitemaps WHERE run_id = ? AND url = ?",
                                            (self.inventory_run, sitemap)).fetchone()
            self._parents[sitemap] = row[0] if row else None
        return self._parents[sitemap]

    def locations(self, check: str, items: list) -> list[dict]:
        """Clickable evidence: the pages, the sitemap files that list them and the index above those, and
        for a foreign canonical the target URL with its status."""
        locs: list[dict] = []
        seen: set = set()

        def add(role, url, note=None):
            if url and (role, url) not in seen:
                seen.add((role, url))
                locs.append({"role": role, "url": url, **({"note": note} if note else {})})

        examples = items[:EXAMPLES]
        for p, note in examples:
            add("page", p["url"], note[:200])
        for p, _ in examples:
            if p.get("listed_in"):
                add("listed in", p["listed_in"])
                add("sitemap index", self._parent(p["listed_in"]))
        if check == "canonical_elsewhere":
            for p, _ in examples:
                t = norm_url(p["facts"]["canonicals"][0])
                add("canonical target", t, self.target_status.get(t))
        return locs

    def _localized(self, host: str, section: str) -> bool:
        return len(self.langs.get((host, section), set())) > 1

    def run(self) -> dict:
        started = time.monotonic()
        params = {"hosts": self.hosts, "sections": self.sections, "inventory_run": self.inventory_run,
                  "per_template": self.per_template, "other_langs": self.other_langs,
                  "max_per_host": self.max_per_host, "max_pages": self.max_pages}
        self.run_id = self.store.start_run(self.tenant.property, AUDITOR, params)
        self.langs = group_langs(self.inventory_db, self.inventory_run, self.sectioned)
        targets = self.targets()
        self.log(f"{len(targets)} pages to fetch on {len({t.host for t in targets})} hosts")

        # Round-robin across hosts: the per-host rate limit then overlaps instead of adding up.
        queues: dict = defaultdict(deque)
        for t in targets:
            queues[t.host].append(t)
        while any(queues.values()):
            for host in list(queues):
                if not queues[host]:
                    continue
                if self.max_pages is not None and len(self.pages) >= self.max_pages:
                    self.stopped_early = True
                    break
                rec = self._fetch(queues[host].popleft())
                self.pages.append(rec)
                self.log(f"[{len(self.pages)}/{len(targets)}] {rec['status'] or rec['error']}  {rec['url']}  "
                         f"issues={len(rec['issues'])}")
            if self.stopped_early:
                break

        self._check_canonical_targets()
        duplicate_issues(self.pages, "titles", "title_duplicate")
        duplicate_issues(self.pages, "descriptions", "description_duplicate")
        for rec in self.pages:
            self.store.add_page(self.run_id, rec)

        report = {"tenant": self.tenant.property, "auditor": AUDITOR, "run_id": self.run_id, "params": params,
                  "canonical_targets": self.target_status,
                  "requests": self.fetcher.requests, "duration_s": round(time.monotonic() - started, 1),
                  "pages_planned": len(targets), "stopped_early": self.stopped_early, "summary": self.summary()}
        report["findings"] = self.findings()
        self.store.finish_run(self.run_id, {k: v for k, v in report.items() if k != "findings"})
        report["pages"] = self.pages
        return report

    def summary(self) -> dict:
        return summarize_pages(self.pages)

    def findings(self) -> list[dict]:
        ids = FindingIds()
        sampled: dict = defaultdict(int)  # (host, section, template) -> sampled pages
        group_size: dict = {}
        for p in self.pages:
            if p.get("ignored"):
                continue  # excluded from the sample: neither a failure nor a pass
            g = (p["host"], p["section"], p["template"])
            sampled[g] += 1
            group_size[g] = max(group_size.get(g, 0), p["group_urls"])

        hits: dict = defaultdict(list)  # (check, host, section) -> [(page, note)]
        for p in self.pages:
            for check, note in p["issues"]:
                hits[(check, p["host"], p["section"])].append((p, note))

        out = []
        for (check, host, section), items in sorted(hits.items()):
            dimension, ftype, title, expected, tier, effort = CHECKS[check]
            per_template: dict = defaultdict(int)
            for p, _ in items:
                per_template[p["template"]] += 1
            in_scope = sum(n for (h, s, _), n in sampled.items() if h == host and s == section)
            estimate = sum(round(group_size[(host, section, t)] * n / sampled[(host, section, t)])
                           for t, n in per_template.items())
            tmpl = ", ".join(f"{t} {n}/{sampled[(host, section, t)]}" for t, n in sorted(per_template.items()))
            examples = "; ".join(f"{p['url']}: {note}" for p, note in items[:EXAMPLES])
            observed = (f"{len(items)} of {in_scope} sampled pages (by template: {tmpl}). e.g. {examples}")
            where = f" (/{section}/)" if section else ""
            finding = make_finding(
                ids=ids, tenant=self.tenant.property, subdomain=host, template=f"page:{section}" if section else "page",
                dimension=dimension, type=ftype, check=check, title=title + where,
                urls=[p["url"] for p, _ in items[:10]], observed=observed, expected=expected,
                affected_pages=estimate, effort=effort, fix_tier=tier, fix_channel=self.fix_channels.get(host))
            # structured sample per template, used by the Prioritizer: {template: [failed, sampled]}
            finding["evidence"]["locations"] = self.locations(check, items)
            finding["evidence"]["sample"] = {t: [n, sampled[(host, section, t)]]
                                             for t, n in sorted(per_template.items())}
            out.append(finding)
        return out


def summarize_pages(pages: list[dict]) -> dict:
    """Per-host counts (pages, HTML 200, errors, redirected, blocked, ignored) and issue counts per check."""
    out: dict = {}
    for p in pages:
        h = out.setdefault(p["host"], {"pages": 0, "ok_html": 0, "errors": 0, "redirected": 0, "blocked": 0,
                                       "ignored": 0, "issues": defaultdict(int)})
        h["pages"] += 1
        if p.get("ignored"):
            h["ignored"] += 1
            continue
        h["ok_html"] += int(p["facts"] is not None)
        h["errors"] += int(p["status"] is None and not p["blocked"] or (p["status"] or 0) >= 400)
        h["redirected"] += int(bool(p["redirects"]))
        h["blocked"] += p["blocked"]
        for check, _ in p["issues"]:
            h["issues"][check] += 1
    return {host: {**h, "issues": dict(sorted(h["issues"].items()))} for host, h in out.items()}


def latest_inventory_run(db) -> int | None:
    """Latest finished full discovery run: no section filter and not stopped early."""
    for run_id, params in db.execute("SELECT id, params FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC"):
        p = json.loads(params or "{}")
        if not p.get("sections") and p.get("max_sitemaps") is None:
            return run_id
    return None


def audit(tenant, fetcher, inventory_db, inventory_run, store, **kw) -> dict:
    return CrawlAudit(tenant, fetcher, inventory_db, inventory_run, store, **kw).run()


def import_report(findings_store, report: dict) -> dict:
    """Record a crawl-audit report in the findings store. A run stopped early resolves nothing."""
    params = report["params"]
    hosts = [] if report.get("stopped_early") else params["hosts"]
    # A section filter narrows only sectioned hosts: findings without a section (other hosts, homepages)
    # were fully re-checked, so they may resolve too.
    return findings_store.import_run(report["findings"], f"{AUDITOR}-run{report['run_id']}", AUDITOR,
                                     hosts=hosts, sections=params.get("sections"), resolve_unsectioned=True)
