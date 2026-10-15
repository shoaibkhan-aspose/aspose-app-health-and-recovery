"""Speed auditor: Core Web Vitals from real users (CrUX) and a Lighthouse lab run per sampled page, through the
PageSpeed Insights API (mobile by default: Google ranks on mobile).

Pages: per (host, section, template) group of the crawl audit, the pages with the most search clicks (real-user
data exists only for pages with enough visits), taken from the crawl audit's clean HTML 200 pages and the
Search Console pages of the same group. Findings are grouped like crawl-audit findings (per host, or per section
on hosts with sections) with a sample per template, so the Prioritizer and the family scorecards use them as is.

Checks from real-user data use the 75th percentile over 28 days and Google's thresholds. A page without
URL-level data counts neither as a pass nor as a failure for those checks. Origin data (the whole host) gives
one host-level finding. The Lighthouse lab score moves by up to ~20 points between runs for the same page, so
it is information only (in the notes and the report), never a finding (SK, 2026-10-15). Pages Google's test
cannot load, or that paint nothing, twice in a row, are findings.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.discovery.classify import LANG_CODES, classify_url, compile_templates
from core.findings import FindingIds, make_finding
from core.pagespeed import PageLoadError, PageSpeedError, extract
from core.prioritize import page_key

AUDITOR = "perf-audit"
EXAMPLES = 5
# metric -> (good up to, poor above); Google's Core Web Vitals thresholds (TTFB: web.dev guidance)
THRESHOLDS = {"lcp": (2500, 4000), "inp": (200, 500), "cls": (0.1, 0.25), "ttfb": (800, 1800)}
CWV = ("lcp", "inp", "cls")
CLEAN_ISSUES = {"soft_404", "meta_refresh"}  # crawl pages with these say nothing about the template's speed
MAX_ERROR_SHARE = 0.1  # more API errors than this: the run is incomplete and resolves nothing

# check -> (dimension, type, title, expected, fix_tier, effort)
CHECKS = {
    "lcp_poor": ("perf", "not_optimized", "Main content takes over 4 s to appear for real users (LCP)",
                 "LCP at most 2.5 s for 75% of visits", 2, "M"),
    "lcp_needs_work": ("perf", "improvable", "Main content appears slowly for real users (LCP 2.5-4 s)",
                       "LCP at most 2.5 s for 75% of visits", 2, "M"),
    "inp_poor": ("perf", "not_optimized", "Pages react slowly to taps and clicks (INP over 500 ms)",
                 "INP at most 200 ms for 75% of visits", 2, "M"),
    "inp_needs_work": ("perf", "improvable", "Pages react sluggishly to taps and clicks (INP 200-500 ms)",
                       "INP at most 200 ms for 75% of visits", 2, "M"),
    "cls_poor": ("perf", "not_optimized", "Layout jumps while the page loads (CLS over 0.25)",
                 "CLS at most 0.1 for 75% of visits", 2, "S"),
    "cls_needs_work": ("perf", "improvable", "Layout shifts while the page loads (CLS 0.1-0.25)",
                       "CLS at most 0.1 for 75% of visits", 2, "S"),
    "ttfb_poor": ("perf", "not_optimized", "Server takes over 1.8 s to answer real users (TTFB)",
                  "TTFB at most 0.8 s for 75% of visits", 2, "M"),
    "lab_load_failed": ("perf", "broken", "Google's page test cannot load the page (server error or very slow)",
                        "the page loads within a few seconds with HTTP 200", 1, "M"),
    "lab_no_paint": ("perf", "incorrect", "Page shows no content in Google's mobile test (nothing painted)",
                     "visible content within a few seconds, without user action", 2, "M"),
    "origin_cwv_poor": ("perf", "not_optimized", "Host fails Core Web Vitals for real users (whole host)",
                        "LCP, INP and CLS good for 75% of visits across the host", 2, "L"),
    "origin_cwv_needs_work": ("perf", "improvable", "Host needs work on Core Web Vitals (whole host)",
                              "LCP, INP and CLS good for 75% of visits across the host", 2, "L"),
}
# per-page check -> the measurement a page needs to count in that check's sample
NEEDS = {"lcp_poor": "lcp", "lcp_needs_work": "lcp", "inp_poor": "inp", "inp_needs_work": "inp",
         "cls_poor": "cls", "cls_needs_work": "cls", "ttfb_poor": "ttfb",
         "lab_load_failed": "any", "lab_no_paint": "any"}


def rating(metric: str, p75: float) -> str:
    good, poor = THRESHOLDS[metric]
    return "good" if p75 <= good else "poor" if p75 > poor else "needs_work"


def fmt(metric: str, v: float | None) -> str:
    if v is None:
        return "–"
    if metric == "cls":
        return f"{v:.2f}"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{round(v)} ms"


def measured(page: dict, need: str) -> bool:
    if need == "any":
        return True
    return need in ((page.get("field") or {}).get("metrics") or {})


def page_issues(page: dict) -> list[tuple[str, str]]:
    """Per-page checks -> [(check, note)], from real-user data only. Notes add the lab value, the lab score
    and the biggest savings Lighthouse suggests, so a team sees where to start."""
    issues = []
    field = (page.get("field") or {}).get("metrics") or {}
    lab = page.get("lab") or {}
    hint = "; ".join(f"{t} ({fmt('lcp', ms)})" for t, ms in page.get("savings") or [])
    for metric in (*CWV, "ttfb"):
        if metric not in field:
            continue
        r = rating(metric, field[metric])
        if r == "good" or (metric == "ttfb" and r != "poor"):
            continue
        note = f"{metric.upper()} {fmt(metric, field[metric])} (real users, p75)"
        if metric in lab:
            note += f"; lab {fmt(metric, lab[metric])}"
        if page.get("lab_score") is not None:
            note += f", lab score {page['lab_score']}"
        if hint and metric in ("lcp", "ttfb", "inp"):
            note += f"; biggest savings: {hint}"
        issues.append((f"{metric}_{'poor' if r == 'poor' else 'needs_work'}", note))
    return issues


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    section: str
    template: str
    group_urls: int
    clicks: float
    source: str  # crawl | search


def select_pages(audit_db, tenant, search_rows: list[dict], hosts: list[str], sections: list[str] | None = None,
                 per_template: int = 2) -> list[Target]:
    """Per crawl-audit group, the `per_template` pages with the most search clicks. Candidates: the crawl
    audit's clean HTML 200 pages (newest result per URL) and Search Console pages classified into the same
    groups. Sorted so that groups alternate between hosts."""
    raw = tenant.raw
    sectioned = set((raw.get("discovery") or {}).get("section_sitemaps") or {})
    templates = {h: compile_templates(s) for h, s in (raw.get("url_templates") or {}).items()}
    langs = frozenset(raw["languages"]) if raw.get("languages") else LANG_CODES
    clicks: dict = defaultdict(float)
    for r in search_rows:
        clicks[page_key(r["page"])] += r.get("clicks", 0) or 0

    def in_scope(host: str, section: str) -> bool:
        return host in hosts and not tenant.is_excluded(host) and (
            sections is None or host not in sectioned or section in sections)

    groups: dict = defaultdict(dict)  # (host, section, template) -> {url: Target}
    sizes: dict = {}
    rows = audit_db.execute(
        "SELECT url, host, section, template, group_urls, redirects, issues FROM pages p "
        "WHERE run_id = (SELECT MAX(run_id) FROM pages q WHERE q.url = p.url) AND status = 200 "
        "AND facts IS NOT NULL AND facts != 'null' AND (ignored IS NULL OR ignored = '')")
    for url, host, section, template, group_urls, redirects, issues in rows:
        g = (host, section or "", template)
        sizes[g] = max(sizes.get(g, 0), group_urls or 0)
        if not in_scope(host, section or "") or json.loads(redirects or "[]"):
            continue
        if any(c in CLEAN_ISSUES for c, _ in json.loads(issues or "[]")):
            continue
        groups[g][url] = Target(url, host, section or "", template, 0, clicks.get(page_key(url), 0), "crawl")
    for r in search_rows:
        url = r["page"]
        host = urlsplit(url).netloc.lower()
        c = classify_url(url, templates.get(host, ()), langs)
        g = (host, c.section if host in sectioned else "", c.template)
        if g in groups and url not in groups[g] and (r.get("clicks") or 0) > 0:
            groups[g][url] = Target(url, host, g[1], g[2], 0, clicks[page_key(url)], "search")

    by_host: dict = defaultdict(list)
    for g in sorted(groups):
        picks = sorted(groups[g].values(), key=lambda t: (-t.clicks, t.source != "crawl", t.url))[:per_template]
        size = sizes.get(g) or len(groups[g])
        by_host[g[0]].append([Target(t.url, t.host, t.section, t.template, size, t.clicks, t.source)
                              for t in picks])
    out: list[Target] = []
    host_lists = [[t for grp in by_host[h] for t in grp] for h in sorted(by_host)]
    for i in range(max((len(x) for x in host_lists), default=0)):  # round-robin over hosts
        out += [x[i] for x in host_lists if i < len(x)]
    return out


class PerfAudit:
    def __init__(self, tenant, client, audit_db, store, search_rows=None, hosts=None, sections=None,
                 per_template=2, workers=4, max_pages=None, log=print):
        self.tenant, self.client, self.audit_db, self.store = tenant, client, audit_db, store
        self.search_rows = search_rows or []
        self.hosts = [h for h in tenant.host_names if not hosts or h in hosts]
        self.sections, self.per_template, self.workers, self.max_pages = sections, per_template, workers, max_pages
        self.log = log or (lambda *_: None)
        self.fix_channels = {h["host"]: h.get("fix_channel") for h in tenant.hosts}
        self.pages: list[dict] = []
        self.origins: dict = {}  # host -> origin field data
        self.run_id: int | None = None
        self.stopped_early = False

    def targets(self) -> list[Target]:
        return select_pages(self.audit_db, self.tenant, self.search_rows, self.hosts, self.sections,
                            self.per_template)

    def _measure(self, t: Target) -> dict:
        rec = {"url": t.url, "host": t.host, "section": t.section, "template": t.template,
               "group_urls": t.group_urls, "clicks": t.clicks, "source": t.source, "strategy": self.client.strategy}
        empty = dict(field=None, origin=None, lab_score=None, lab={}, savings=[], final_url=None, lab_error=None)
        try:
            rec.update(extract(self.client.run(t.url)))
            rec["error"] = None
            rec["issues"] = page_issues(rec)
        except PageLoadError as e:  # measured: the page itself did not load, or loaded but showed nothing
            check = "lab_no_paint" if str(e).startswith("NO_FCP") else "lab_load_failed"
            rec.update(empty, lab_error=str(e)[:200], error=None, issues=[(check, str(e)[:200])])
        except PageSpeedError as e:  # not measured: API or network trouble
            rec.update(empty, error=str(e), issues=[])
        return rec

    def run(self) -> dict:
        started = time.monotonic()
        params = {"hosts": self.hosts, "sections": self.sections, "per_template": self.per_template,
                  "strategy": self.client.strategy, "max_pages": self.max_pages}
        targets = self.targets()
        if self.max_pages is not None and len(targets) > self.max_pages:
            targets, self.stopped_early = targets[: self.max_pages], True
        self.run_id = self.store.start_run(self.tenant.property, AUDITOR, params)
        self.log(f"{len(targets)} pages to measure on {len({t.host for t in targets})} hosts")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for rec in pool.map(self._measure, targets):
                self.pages.append(rec)
                f = (rec.get("field") or {}).get("metrics") or {}
                self.log(f"[{len(self.pages)}/{len(targets)}] {rec['error'] or 'score ' + str(rec['lab_score'])}"
                         f"  field={'yes' if f else 'no'}  {rec['url']}  issues={len(rec['issues'])}")
                if rec.get("origin") and rec["host"] not in self.origins:
                    self.origins[rec["host"]] = rec["origin"]
                if rec["error"] and "not enabled" in rec["error"]:
                    raise PageSpeedError(rec["error"])
        self._retry_failed(targets)
        errors = sum(1 for p in self.pages if p["error"])
        report = {"tenant": self.tenant.property, "auditor": AUDITOR, "run_id": self.run_id, "params": params,
                  "requests": self.client.calls, "duration_s": round(time.monotonic() - started, 1),
                  "pages_planned": len(targets), "stopped_early": self.stopped_early,
                  "incomplete": errors > MAX_ERROR_SHARE * max(1, len(self.pages)),
                  "origins": self.origins, "summary": summarize(self.pages)}
        report["findings"] = self.findings()
        self.store.finish_run(self.run_id, {k: v for k, v in report.items() if k != "findings"})
        report["pages"] = self.pages
        return report

    def _retry_failed(self, targets: list[Target]) -> None:
        """Measure pages that failed (API error or page not loaded) once more, one at a time: Lighthouse runs
        fail now and then for pages that load fine, so only a second failure counts."""
        by_url = {t.url: t for t in targets}
        failed = [i for i, p in enumerate(self.pages) if p["error"] or p.get("lab_error")]
        if failed:
            self.log(f"Retrying {len(failed)} failed pages")
        for i in failed:
            rec = self._measure(by_url[self.pages[i]["url"]])
            rec["retried"] = True
            self.pages[i] = rec
            if rec.get("origin") and rec["host"] not in self.origins:
                self.origins[rec["host"]] = rec["origin"]
            self.log(f"  retry: {rec['error'] or rec['lab_error'] or 'ok'}  {rec['url']}")

    def findings(self) -> list[dict]:
        ids = FindingIds()
        ok = [p for p in self.pages if not p["error"]]
        hits: dict = defaultdict(list)  # (check, host, section) -> [(page, note)]
        for p in ok:
            for check, note in p["issues"]:
                hits[(check, p["host"], p["section"])].append((p, note))
        out = []
        for (check, host, section), items in sorted(hits.items()):
            dimension, ftype, title, expected, tier, effort = CHECKS[check]
            pool = [p for p in ok if p["host"] == host and p["section"] == section and measured(p, NEEDS[check])]
            sampled: dict = defaultdict(int)
            size: dict = {}
            for p in pool:
                sampled[p["template"]] += 1
                size[p["template"]] = p["group_urls"]
            failed: dict = defaultdict(int)
            for p, _ in items:
                failed[p["template"]] += 1
            estimate = sum(round(size[t] * n / sampled[t]) for t, n in failed.items())
            tmpl = ", ".join(f"{t} {n}/{sampled[t]}" for t, n in sorted(failed.items()))
            examples = "; ".join(f"{p['url']}: {note}" for p, note in items[:EXAMPLES])
            source = {"lab_load_failed": "a lab attempt (each failed twice)",
                      "lab_no_paint": "a lab attempt (each failed twice)"}.get(
                check, "real-user data")
            observed = (f"{len(items)} of {len(pool)} pages with {source} (by template: {tmpl}). e.g. {examples}")
            where = f" (/{section}/)" if section else ""
            f = make_finding(
                ids=ids, tenant=self.tenant.property, subdomain=host, template=f"page:{section}" if section else "page",
                dimension=dimension, type=ftype, check=check, title=title + where,
                urls=[p["url"] for p, _ in items[:10]], observed=observed, expected=expected,
                affected_pages=estimate, effort=effort, fix_tier=tier, fix_channel=self.fix_channels.get(host))
            f["evidence"]["locations"] = [{"role": "page", "url": p["url"], "note": note[:200]}
                                          for p, note in items[:EXAMPLES]]
            f["evidence"]["sample"] = {t: [n, sampled[t]] for t, n in sorted(failed.items())}
            out.append(f)
        out += self.origin_findings(ids)
        return out

    def origin_findings(self, ids: FindingIds) -> list[dict]:
        out = []
        for host, origin in sorted(self.origins.items()):
            m = origin["metrics"]
            ratings = {k: rating(k, m[k]) for k in CWV if k in m}
            if all(r == "good" for r in ratings.values()):
                continue
            check = "origin_cwv_poor" if "poor" in ratings.values() else "origin_cwv_needs_work"
            dimension, ftype, title, expected, tier, effort = CHECKS[check]
            parts = ", ".join(f"{k.upper()} {fmt(k, m[k])} ({ratings[k].replace('_', ' ')})" for k in ratings)
            observed = (f"Whole host, real users over 28 days, 75th percentile ({self.client.strategy}): {parts}"
                        + (f"; TTFB {fmt('ttfb', m['ttfb'])}" if "ttfb" in m else ""))
            f = make_finding(ids=ids, tenant=self.tenant.property, subdomain=host, template="origin",
                             dimension=dimension, type=ftype, check=check, title=title, urls=[f"https://{host}/"],
                             observed=observed, expected=expected, effort=effort, fix_tier=tier,
                             fix_channel=self.fix_channels.get(host))
            out.append(f)
        return out


def summarize(pages: list[dict]) -> dict:
    """Per host: pages measured, API errors, pages Lighthouse could not load, pages with URL-level real-user
    data, median lab score, issue counts."""
    out: dict = {}
    for p in pages:
        h = out.setdefault(p["host"], {"pages": 0, "errors": 0, "not_loaded": 0, "field": 0, "scores": [],
                                       "issues": defaultdict(int)})
        h["pages"] += 1
        h["errors"] += int(bool(p["error"]))
        h["not_loaded"] += int(bool(p.get("lab_error")))
        h["field"] += int(bool(p.get("field")))
        if p.get("lab_score") is not None:
            h["scores"].append(p["lab_score"])
        for check, _ in p["issues"]:
            h["issues"][check] += 1
    for h in out.values():
        s = sorted(h.pop("scores"))
        h["median_score"] = s[len(s) // 2] if s else None
        h["issues"] = dict(sorted(h["issues"].items()))
    return out


def audit(tenant, client, audit_db, store, **kw) -> dict:
    return PerfAudit(tenant, client, audit_db, store, **kw).run()


def import_report(findings_store, report: dict) -> dict:
    """Record a speed-audit report in the findings store. A run stopped early, or with many API errors,
    resolves nothing."""
    params = report["params"]
    hosts = [] if report.get("stopped_early") or report.get("incomplete") else params["hosts"]
    return findings_store.import_run(report["findings"], f"{AUDITOR}-run{report['run_id']}", AUDITOR,
                                     hosts=hosts, sections=params.get("sections"), resolve_unsectioned=True)
