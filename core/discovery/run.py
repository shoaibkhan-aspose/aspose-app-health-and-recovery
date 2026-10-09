"""Run discovery for one tenant: robots.txt per host, sitemap tree walk, URL inventory, findings.

Seeds, per host in scope: `Sitemap:` lines from robots.txt, `/sitemap.xml`, the tenant's per-section
sitemap probes (`discovery.section_sitemaps`) and the GSC sitemaps snapshot. Sitemap indexes are
walked breadth-first. Every fetch goes through the polite fetcher (robots.txt, rate limit, retries).
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict, deque
from urllib.parse import urlsplit

from core.discovery.classify import LANG_CODES, classify_url, compile_templates
from core.discovery.sitemap import MAX_ENTRIES, MAX_UNCOMPRESSED, parse_sitemap
from core.findings import FindingIds, make_finding

MAX_DEPTH = 4
EXAMPLES = 5
BULK_LASTMOD_MIN_URLS = 100
HREFLANG_PARTIAL = 0.9  # below this share of URLs with alternates, hreflang coverage is a finding
LASTMOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Seeds whose failure is a finding. A failed guess (`/sitemap.xml`, section probe) is not.
REPORTED_ORIGINS = {"robots", "gsc-snapshot", "child", "child-corrected"}
AI_BOTS = (
    "GPTBot", "ChatGPT-User", "OAI-SearchBot", "ClaudeBot", "Claude-User", "Claude-SearchBot", "anthropic-ai",
    "PerplexityBot", "Perplexity-User", "Google-Extended", "CCBot", "Bytespider", "Applebot-Extended",
    "Amazonbot", "meta-externalagent",
)

# check -> (dimension, type, title, expected, fix_tier)
SITEMAP_CHECKS = {
    "fetch_failed": ("crawl", "broken", "Sitemap cannot be fetched", "HTTP 200 with a sitemap", 1),
    "parse_error": ("crawl", "broken", "Sitemap is not a valid sitemap", "a valid <urlset> or <sitemapindex>", 1),
    "empty": ("crawl", "broken", "Sitemap lists no URLs", "at least one URL or child sitemap", 1),
    "over_limit": ("crawl", "broken", "Sitemap exceeds 50,000 URLs or 50 MB",
                   "files split under the sitemaps.org limits", 1),
    "foreign_child": ("crawl", "incorrect", "Sitemap index points outside the property or to an excluded host",
                      "child sitemaps on the property's own production hosts", 1),
    "malformed_child": ("crawl", "broken", "Sitemap index lists child sitemaps with a double slash",
                        "child sitemap URLs with a single slash that return HTTP 200", 1),
    "redirected": ("crawl", "not_optimized", "Sitemap URL redirects", "sitemap served directly with HTTP 200", 1),
    "off_host": ("crawl", "incorrect", "Sitemap lists URLs on another host", "URLs on the sitemap's own host", 1),
    "http_urls": ("crawl", "incorrect", "Sitemap lists http:// URLs", "https:// URLs only", 1),
    "excluded_host": ("crawl", "incorrect", "Sitemap lists URLs on an excluded (QA/staging) host",
                      "production URLs only", 1),
    "dup_in_file": ("crawl", "not_optimized", "Sitemap lists the same URL more than once", "each URL listed once", 1),
    "lastmod_bulk": ("crawl", "not_optimized", "All lastmod dates are identical (bulk-stamped)",
                     "lastmod = the date each page last changed", 1),
    "lastmod_missing": ("crawl", "improvable", "Sitemap has no lastmod dates", "lastmod on every entry", 1),
    "lastmod_invalid": ("crawl", "incorrect", "Sitemap has invalid lastmod values",
                        "W3C datetime (YYYY-MM-DD or full timestamp)", 1),
}


def summarize_robots(res) -> dict:
    out = {"url": res.url, "status": res.status, "error": res.error, "bytes": len(res.body),
           "is_html": False, "sitemaps": [], "user_agents": [], "disallow_rules": 0, "ai_bots_named": []}
    if res.status != 200 or res.error:
        return out
    if res.body.lstrip()[:1] == b"<":
        out["is_html"] = True
        return out
    for line in res.body.decode("utf-8", "replace").splitlines():
        key, sep, val = line.split("#", 1)[0].partition(":")
        if not sep:
            continue
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            out["user_agents"].append(val)
        elif key == "disallow" and val:
            out["disallow_rules"] += 1
        elif key == "sitemap" and val:
            out["sitemaps"].append(val)
    named = {a.lower() for a in out["user_agents"]}
    out["ai_bots_named"] = [b for b in AI_BOTS if b.lower() in named]
    return out


def lastmod_stats(entries) -> dict:
    values = [e.lastmod for e in entries if e.lastmod]
    valid = [v[:10] for v in values if LASTMOD_RE.match(v)]
    return {
        "lastmod_missing": len(entries) - len(values),
        "lastmod_invalid": len(values) - len(valid),
        "lastmod_min": min(valid, default=None),
        "lastmod_max": max(valid, default=None),
        "lastmod_distinct": len(set(valid)),
    }


def path_section(url: str) -> str:
    """First path segment of a sitemap URL if it sits in a sub-folder (/pdf/sitemap.xml -> pdf), else ''."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return segments[0] if len(segments) > 1 else ""


class Discovery:
    def __init__(self, tenant, fetcher, store, hosts=None, sections=None, max_sitemaps=None, log=print):
        self.tenant = tenant
        self.fetcher = fetcher
        self.store = store
        self.max_sitemaps = max_sitemaps
        self.log = log or (lambda *_: None)
        self.hosts = [h for h in tenant.host_names if not hosts or h in hosts]
        self.section_filter = set(sections or [])
        raw = tenant.raw
        disc = raw.get("discovery") or {}
        self.section_cfg = disc.get("section_sitemaps") or {}
        self.templates = {h: compile_templates(specs) for h, specs in (raw.get("url_templates") or {}).items()}
        self.lang_codes = frozenset(raw["languages"]) if raw.get("languages") else LANG_CODES
        self.fix_channels = {h["host"]: h.get("fix_channel") for h in tenant.hosts}
        self.known_hosts = {h["host"] for h in tenant.hosts}

        self.origins: dict[str, set] = {}
        self.queue: deque = deque()
        self.records: list[dict] = []
        self.robots: dict[str, dict] = {}
        self.foreign_children: list[tuple[str, str]] = []
        self.malformed_children: list[tuple[str, str, str]] = []  # (index, listed child, corrected child)
        self.new_hosts: Counter = Counter()
        self.run_id: int | None = None

    # ---- scope ----

    def _in_property(self, host: str) -> bool:
        prop = self.tenant.property
        return host == prop or host.endswith("." + prop)

    def _group(self, sitemap_url: str) -> str:
        """Findings group sitemaps per section folder on hosts with configured sections, else per host."""
        host = urlsplit(sitemap_url).netloc.lower()
        return path_section(sitemap_url) if host in self.section_cfg else ""

    def _sections_for(self, host: str) -> list[str]:
        cfg = self.section_cfg.get(host) or {}
        src = cfg.get("sections") or []
        sections = self.tenant.raw.get(src, []) if isinstance(src, str) else src
        return [str(s) for s in sections]

    # ---- seeds ----

    def _enqueue(self, url: str, origin: str, parent=None, root=None, depth=0) -> None:
        if url in self.origins:
            self.origins[url].add(origin)
            return
        self.origins[url] = {origin}
        self.queue.append((url, parent, root or url, depth))

    def _seed(self) -> None:
        for host in self.hosts:
            info = summarize_robots(self.fetcher.robots(f"https://{host}"))
            self.robots[host] = info
            self.store.add_robots(self.run_id, host, info)
            self.log(f"robots  {info['status'] or info['error']}  https://{host}/robots.txt  "
                     f"sitemap lines={len(info['sitemaps'])}")
            for sm in info["sitemaps"]:
                sm_host = urlsplit(sm).netloc.lower()
                if self._in_property(sm_host) and not self.tenant.is_excluded(sm_host):
                    self._enqueue(sm, "robots")
                else:
                    self.foreign_children.append((info["url"], sm))
            self._enqueue(f"https://{host}/sitemap.xml", "root")

        for host in self.hosts:
            for section in self._sections_for(host):
                if self.section_filter and section not in self.section_filter:
                    continue
                for path in (self.section_cfg[host].get("paths") or []):
                    self._enqueue(f"https://{host}{path.format(section=section)}", "section-probe")

        snap = self.tenant.raw.get("gsc_sitemaps_snapshot")
        if snap:
            base = snap["property"].rstrip("/")
            if urlsplit(base).netloc in self.hosts:
                for s in snap["sitemaps"]:
                    sec = path_section(base + s["path"])
                    if self.section_filter and sec and sec not in self.section_filter:
                        continue
                    self._enqueue(base + s["path"], "gsc-snapshot")

    # ---- walk ----

    def _process(self, url: str, parent, root: str, depth: int) -> dict:
        res = self.fetcher.get(url)
        host = urlsplit(url).netloc.lower()
        rec = {
            "url": url, "host": host, "parent": parent, "root": root, "depth": depth,
            "origins": ",".join(sorted(self.origins[url])), "status": res.status, "final_url": res.final_url,
            "redirected": int(bool(res.redirects)), "content_type": res.content_type, "bytes": len(res.body),
            "error": res.error, "kind": None, "entries": 0, "child_count": 0, "url_count": 0, "examples": {},
        }
        if res.truncated:
            rec["error"] = f"body larger than {self.fetcher.max_bytes} bytes; not parsed"
        if not res.ok:
            return rec

        parsed = parse_sitemap(res.body)
        rec.update(kind=parsed.kind, gzipped=int(parsed.gzipped), had_bom=int(parsed.had_bom),
                   uncompressed_bytes=parsed.uncompressed_bytes, entries=len(parsed.entries),
                   **lastmod_stats(parsed.entries))
        if parsed.error:
            rec["error"] = parsed.error
        if parsed.kind == "index":
            rec["child_count"] = len(parsed.entries)
            for e in parsed.entries:
                child_host = urlsplit(e.loc).netloc.lower()
                if not self._in_property(child_host) or self.tenant.is_excluded(child_host):
                    self.foreign_children.append((url, e.loc))
                elif depth + 1 > MAX_DEPTH:
                    continue
                elif "//" in urlsplit(e.loc).path:
                    # Fetch both: the listed URL (evidence) and the corrected one (so the inventory is complete).
                    fixed = urlsplit(e.loc)._replace(path=re.sub("/{2,}", "/", urlsplit(e.loc).path)).geturl()
                    self.malformed_children.append((url, e.loc, fixed))
                    self._enqueue(e.loc, "child-malformed", parent=url, root=root, depth=depth + 1)
                    self._enqueue(fixed, "child-corrected", parent=url, root=root, depth=depth + 1)
                else:
                    self._enqueue(e.loc, "child", parent=url, root=root, depth=depth + 1)
        elif parsed.kind in ("urlset", "text"):
            self._record_urls(rec, parsed.entries)
        return rec

    def _record_urls(self, rec: dict, entries) -> None:
        seen: set = set()
        counts: Counter = Counter()
        examples: dict = defaultdict(list)
        rows = []
        for e in entries:
            loc = e.loc
            if loc in seen:
                counts["dup_in_file"] += 1
                if len(examples["dup_in_file"]) < EXAMPLES:
                    examples["dup_in_file"].append(loc)
                continue
            seen.add(loc)
            parts = urlsplit(loc)
            u_host = parts.netloc.lower()
            for check, hit in (
                ("http_urls", parts.scheme != "https"),
                ("off_host", u_host != rec["host"]),
                ("excluded_host", self.tenant.is_excluded(u_host)),
            ):
                if hit:
                    counts[check] += 1
                    if len(examples[check]) < EXAMPLES:
                        examples[check].append(loc)
            if self._in_property(u_host) and u_host not in self.known_hosts:
                self.new_hosts[u_host] += 1
            if e.alternates:
                counts["hreflang_urls"] += 1
            c = classify_url(loc, self.templates.get(u_host, ()), self.lang_codes)
            rows.append((self.run_id, loc, u_host, c.section, c.lang, c.template, e.lastmod, rec["url"],
                         len(e.alternates)))
        self.store.add_urls(rows)
        rec["url_count"] = len(rows)
        rec.update({k: counts[k] for k in ("dup_in_file", "http_urls", "off_host", "excluded_host", "hreflang_urls")})
        rec["off_host_urls"] = rec.pop("off_host")
        rec["excluded_host_urls"] = rec.pop("excluded_host")
        rec["examples"] = dict(examples)

    def run(self) -> dict:
        started = time.monotonic()
        self.run_id = self.store.start_run(self.tenant.property, {
            "hosts": self.hosts, "sections": sorted(self.section_filter), "max_sitemaps": self.max_sitemaps,
        })
        self._seed()
        while self.queue:
            if self.max_sitemaps is not None and len(self.records) >= self.max_sitemaps:
                break
            url, parent, root, depth = self.queue.popleft()
            rec = self._process(url, parent, root, depth)
            self.records.append(rec)
            self.store.add_sitemap(self.run_id, rec)
            self.log(f"[{len(self.records)}] {rec['status'] or rec['error']}  {url}  "
                     f"{rec['kind'] or '-'}  children={rec['child_count']} urls={rec['url_count']}")

        report = self.summary()
        report["params"] = {"hosts": self.hosts, "sections": sorted(self.section_filter) or None,
                            "max_sitemaps": self.max_sitemaps}
        report["requests"] = self.fetcher.requests
        report["duration_s"] = round(time.monotonic() - started, 1)
        report["not_fetched"] = [q[0] for q in self.queue]
        report["findings"] = self.findings()
        self.store.finish_run(self.run_id, {k: v for k, v in report.items() if k != "findings"})
        return report

    # ---- results ----

    def _recursive_urls(self) -> dict[str, int]:
        own = {r["url"]: r["url_count"] for r in self.records}
        kids: dict = defaultdict(list)
        for r in self.records:
            if r["parent"]:
                kids[r["parent"]].append(r["url"])
        memo: dict = {}

        def total(u: str, stack: frozenset = frozenset()) -> int:
            if u in memo:
                return memo[u]
            if u in stack:
                return 0
            memo[u] = own.get(u, 0) + sum(total(k, stack | {u}) for k in kids[u])
            return memo[u]

        return {u: total(u) for u in own}

    def summary(self) -> dict:
        by_url = {r["url"]: r for r in self.records}
        totals = self._recursive_urls()

        hosts: dict = {}
        for host in self.hosts:
            recs = [r for r in self.records if r["host"] == host]
            hosts[host] = {
                "robots": {k: self.robots[host][k] for k in ("status", "error", "is_html", "sitemaps",
                                                              "disallow_rules", "ai_bots_named")},
                "sitemaps_fetched": len(recs),
                "sitemaps_ok": sum(1 for r in recs if r["status"] == 200 and r["kind"] in ("index", "urlset", "text")),
                "sitemaps_failed": [r["url"] for r in recs if (r["status"] != 200 or r["error"])
                                    and self.origins[r["url"]] & REPORTED_ORIGINS],
                "urls": 0,
            }

        sections: dict = defaultdict(lambda: defaultdict(lambda: {"urls": 0, "hreflang_urls": 0, "langs": {},
                                                                  "templates": {}}))
        for host, section, lang, n, with_hreflang in self.store.section_stats(self.run_id):
            s = sections[host][section]
            s["urls"] += n
            s["hreflang_urls"] += with_hreflang or 0
            s["langs"][lang or "default"] = n
            if host in hosts:
                hosts[host]["urls"] += n
        for host, section, template, n in self.store.template_stats(self.run_id):
            sections[host][section]["templates"][template] = n

        gsc = []
        snap = self.tenant.raw.get("gsc_sitemaps_snapshot")
        if snap:
            base = snap["property"].rstrip("/")
            for s in snap["sitemaps"]:
                r = by_url.get(base + s["path"])
                if r is None:
                    continue
                gsc.append({
                    "path": s["path"], "gsc_status": s.get("status"), "gsc_discovered": s.get("discovered"),
                    "live_status": r["status"] or r["error"], "live_kind": r["kind"],
                    "live_urls": totals.get(r["url"], 0),
                })

        return {
            "tenant": self.tenant.property,
            "run_id": self.run_id,
            "hosts": hosts,
            "sections": {h: dict(v) for h, v in sections.items()},
            "gsc_comparison": gsc,
            "new_hosts": dict(self.new_hosts.most_common()),
            "sitemaps_fetched": len(self.records),
        }

    def _finding(self, ids, **kw) -> dict:
        kw.setdefault("fix_channel", self.fix_channels.get(kw["subdomain"]))
        return make_finding(ids=ids, tenant=self.tenant.property, **kw)

    def findings(self) -> list[dict]:
        ids = FindingIds()
        out: list[dict] = []
        snap = self.tenant.raw.get("gsc_sitemaps_snapshot") or {}
        gsc_status = {snap.get("property", "").rstrip("/") + s["path"]: s for s in snap.get("sitemaps", [])}

        # robots.txt, per host
        for host, info in self.robots.items():
            url = info["url"]
            if info["status"] is None or info["status"] >= 500:
                out.append(self._finding(
                    ids, check="robots_unreachable", subdomain=host, template="robots.txt", dimension="crawl",
                    type="broken", title="robots.txt is unreachable (search engines may stop crawling the host)",
                    urls=[url],
                    observed=str(info["status"] or info["error"]), expected="HTTP 200, or 404 if intentionally absent",
                    fix_tier=3))
            elif info["status"] != 200:
                out.append(self._finding(
                    ids, check="robots_missing", subdomain=host, template="robots.txt", dimension="crawl",
                    type="improvable", title="No robots.txt on this host",
                    urls=[url], observed=f"HTTP {info['status']}",
                    expected="robots.txt with a Sitemap: line", fix_tier=3))
            elif info["is_html"]:
                out.append(self._finding(
                    ids, check="robots_html", subdomain=host, template="robots.txt", dimension="crawl",
                    type="incorrect", title="robots.txt returns an HTML page",
                    urls=[url], observed="HTTP 200 with HTML body",
                    expected="plain-text robots.txt", fix_tier=3))
            elif not info["sitemaps"]:
                out.append(self._finding(
                    ids, check="robots_no_sitemap", subdomain=host, template="robots.txt", dimension="crawl",
                    type="improvable", title="robots.txt has no Sitemap: line",
                    urls=[url], observed="no Sitemap: directive",
                    expected="Sitemap: line(s) pointing to the host's sitemap(s)", fix_tier=3))

        # sitemap checks, grouped per host + sitemap folder so one template fix maps to one finding
        groups: dict = defaultdict(list)  # (check, host, section) -> [(url, note, pages)]
        for r in self.records:
            key = (r["host"], self._group(r["url"]))
            reported = bool(self.origins[r["url"]] & REPORTED_ORIGINS)
            if r["status"] != 200 or (r["error"] and r["kind"] is None):
                if reported:
                    note = f"HTTP {r['status']}" if r["status"] else r["error"]
                    g = gsc_status.get(r["url"])
                    if g:
                        note += f" (GSC: {g.get('status')}, last read {g.get('last_read')})"
                    groups[("fetch_failed", *key)].append((r["url"], note, 0))
                continue
            # A guessed URL that answers with an HTML page or a redirect is not a sitemap problem.
            if reported and (r["kind"] == "unknown" or (r["error"] and r["kind"] in ("index", "urlset"))):
                groups[("parse_error", *key)].append((r["url"], r["error"] or "unparseable", r["url_count"]))
            if reported and r["kind"] in ("index", "urlset", "text") and r["entries"] == 0:
                groups[("empty", *key)].append((r["url"], "0 entries", 0))
            if r["entries"] > MAX_ENTRIES or (r.get("uncompressed_bytes") or 0) > MAX_UNCOMPRESSED:
                groups[("over_limit", *key)].append(
                    (r["url"], f"{r['entries']:,} entries, {r.get('uncompressed_bytes', 0):,} bytes", r["url_count"]))
            if reported and r["redirected"]:
                groups[("redirected", *key)].append((r["url"], f"redirects to {r['final_url']}", r["url_count"]))
            for check, col in (("off_host", "off_host_urls"), ("http_urls", "http_urls"),
                               ("excluded_host", "excluded_host_urls"), ("dup_in_file", "dup_in_file")):
                n = r.get(col) or 0
                if n:
                    ex = ", ".join(r["examples"].get(check, [])[:2])
                    groups[(check, *key)].append((r["url"], f"{n:,} URLs, e.g. {ex}", n))
            if r["entries"]:
                if r.get("lastmod_invalid"):
                    groups[("lastmod_invalid", *key)].append(
                        (r["url"], f"{r['lastmod_invalid']:,} invalid of {r['entries']:,}", r["url_count"]))
                if r.get("lastmod_missing") == r["entries"]:
                    groups[("lastmod_missing", *key)].append(
                        (r["url"], f"0 of {r['entries']:,} entries", r["url_count"]))
                elif (r["kind"] == "urlset" and r["url_count"] >= BULK_LASTMOD_MIN_URLS and not r.get("lastmod_missing")
                      and r.get("lastmod_distinct") == 1):
                    groups[("lastmod_bulk", *key)].append(
                        (r["url"], f"all {r['url_count']:,} = {r['lastmod_min']}", r["url_count"]))
        status_of = {r["url"]: r["status"] or r["error"] for r in self.records}
        urls_in = {r["url"]: r["url_count"] for r in self.records}
        lead: dict = defaultdict(list)  # group -> locations shown before the sitemaps (where the fix is)
        for parent, child, fixed in self.malformed_children:
            host = urlsplit(parent).netloc.lower()
            key = ("malformed_child", host, self._group(parent))
            if not any(loc["url"] == parent for loc in lead[key]):
                lead[key].append({"role": "sitemap index", "url": parent,
                                  "note": "lists its child sitemaps with a double slash"})
            lead[key].append({"role": "corrected child", "url": fixed,
                              "note": f"HTTP {status_of.get(fixed, 'not fetched')}"})
            groups[key].append(
                (child, f"listed by {parent}, HTTP {status_of.get(child, 'not fetched')}; "
                        f"corrected URL gives HTTP {status_of.get(fixed, 'not fetched')}", urls_in.get(fixed, 0)))
        for parent, child in self.foreign_children:
            host = urlsplit(parent).netloc.lower()
            groups[("foreign_child", host, self._group(parent))].append((parent, f"points to {child}", 0))

        roles = {"malformed_child": "listed child", "foreign_child": "sitemap index"}
        for (check, host, section), items in sorted(groups.items()):
            dimension, ftype, title, expected, tier = SITEMAP_CHECKS[check]
            where = f" ({section})" if section else ""
            observed = "; ".join(f"{u}: {note}" for u, note, _ in items[:EXAMPLES])
            if len(items) > EXAMPLES:
                observed += f"; and {len(items) - EXAMPLES} more sitemaps"
            finding = self._finding(
                ids, subdomain=host, template=f"sitemap{':' + section if section else ''}", dimension=dimension,
                type=ftype, check=check, title=title + where, urls=[u for u, _, _ in items[:10]], observed=observed,
                expected=expected, affected_pages=sum(p for _, _, p in items), fix_tier=tier)
            # clickable evidence: where the fix is first (e.g. the index), then the affected sitemaps
            finding["evidence"]["locations"] = lead[(check, host, section)][:EXAMPLES + 1] + [
                {"role": roles.get(check, "sitemap"), "url": u, "note": note[:200]} for u, note, _ in items[:EXAMPLES]]
            out.append(finding)

        # coverage: hosts and configured sections with no URLs from any sitemap
        summary_hosts = {h: sum(1 for r in self.records if r["host"] == h and r["url_count"]) for h in self.hosts}
        url_hosts = {row[0] for row in self.store.section_stats(self.run_id)}
        for host in self.hosts:
            if host not in url_hosts and not summary_hosts[host]:
                tried = [r["url"] for r in self.records if r["host"] == host][:5] or [f"https://{host}/sitemap.xml"]
                out.append(self._finding(
                    ids, subdomain=host, template="sitemap", dimension="crawl", type="not_optimized",
                    check="host_no_sitemap", title="No working sitemap found for this host", urls=tried,
                    observed="no sitemap with URLs via robots.txt, /sitemap.xml or known paths",
                    expected="a sitemap listed in robots.txt and submitted in GSC", fix_tier=1))
        stats = self.store.section_stats(self.run_id)
        present = {(h, s) for h, s, *_ in stats}
        for host in self.hosts:
            for section in self._sections_for(host):
                if self.section_filter and section not in self.section_filter:
                    continue
                if (host, section) not in present:
                    tried = [r["url"] for r in self.records if r["host"] == host and path_section(r["url"]) == section]
                    out.append(self._finding(
                        ids, subdomain=host, template=f"sitemap:{section}", dimension="crawl", type="broken",
                        check="section_no_sitemap", title=f"No working sitemap lists any /{section}/ URL",
                        urls=tried[:5] or [f"https://{host}/{section}/"],
                        observed="0 URLs found under this section in any sitemap",
                        expected="a working sitemap for the section, submitted in GSC", fix_tier=1))

        # intl: localized pages with no hreflang in sitemaps (grouped per section where sections are configured)
        agg: dict = defaultdict(lambda: {"langs": set(), "urls": 0, "hreflang": 0})
        for host, section, lang, n, with_hreflang in stats:
            key = (host, section if host in self.section_cfg else None)
            agg[key]["langs"].add(lang)
            agg[key]["urls"] += n
            agg[key]["hreflang"] += with_hreflang or 0
        for (host, section), a in sorted(agg.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            if len(a["langs"]) < 2 or a["hreflang"] / a["urls"] >= HREFLANG_PARTIAL:
                continue
            where = f" /{section}/" if section else ""
            missing = a["urls"] - a["hreflang"]
            if a["hreflang"] == 0:
                ftype, check = "not_optimized", "hreflang_none"
                title = f"Localized{where} pages have no hreflang in sitemaps"
                urls = self.store.sample_sitemaps(self.run_id, host, section)
            else:
                ftype, check = "incorrect", "hreflang_partial"
                title = f"hreflang in sitemaps covers only some localized{where} pages"
                urls = self.store.sample_urls(self.run_id, host, section, "hreflang_count = 0")
            out.append(self._finding(
                ids, subdomain=host, template=f"sitemap{':' + section if section else ''}", dimension="intl",
                type=ftype, check=check, title=title, urls=urls,
                observed=f"{len(a['langs'])} language variants, {a['urls']:,} URLs, "
                         f"{a['hreflang']:,} with xhtml:link alternates",
                expected="hreflang alternates on every localized URL, in sitemaps or page HTML "
                         "(verify HTML in the intl audit)",
                affected_pages=missing, effort="M", fix_tier=2))

        # URLs listed in more than one sitemap
        dups: Counter = Counter()
        for host, section, n in self.store.cross_sitemap_dups(self.run_id):
            dups[(host, section if host in self.section_cfg else None)] += n
        for (host, section), n in sorted(dups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            where = f" (/{section}/)" if section else ""
            out.append(self._finding(
                ids, subdomain=host, template=f"sitemap{':' + section if section else ''}", dimension="crawl",
                type="not_optimized", check="cross_sitemap_dups", title=f"URLs listed in more than one sitemap{where}",
                urls=self.store.sample_urls(self.run_id, host, section, "sitemap_count > 1"),
                observed=f"{n:,} URLs appear in 2+ sitemaps", expected="each URL in exactly one sitemap",
                affected_pages=n, fix_tier=1))
        return out


def discover(tenant, fetcher, store, **kw) -> dict:
    return Discovery(tenant, fetcher, store, **kw).run()


def import_report(findings_store, report: dict) -> dict:
    """Record a discovery report in the findings store. The run's host/section scope limits what can resolve."""
    params = report.get("params") or {}
    hosts = params.get("hosts") or list(report["hosts"])
    if report.get("not_fetched"):
        hosts = []  # stopped early (--max-sitemaps): absence of a finding proves nothing, so resolve nothing
    return findings_store.import_run(report["findings"], f"discovery-run{report['run_id']}", "discovery",
                                     hosts=hosts, sections=params.get("sections"))
