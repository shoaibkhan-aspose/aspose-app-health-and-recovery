"""AI-readiness auditor, host level: can AI search and answer engines reach each host, and does it offer an
llms.txt? The page-level AI checks (text without JavaScript, structured data) run inside the crawl auditor.

- robots.txt: each host's rules are evaluated (RFC 9309) for the AI search/answer crawlers (ANSWER_BOTS) on the
  homepage and every section root. A block is a finding. Blocks on AI *training* crawlers (TRAINING_BOTS) are a
  business choice (SK, 2026-10-15): reported per host as information, never as a finding.
- Firewall/CDN: the homepage is fetched as usual and once per crawler user agent (with the audit's name
  appended, so requests stay identifiable). HTTP errors, challenge pages or a page much smaller than the usual
  one are a finding. A CDN that verifies crawler IP addresses may block this impersonation and still let the
  real crawler in; the fix guide says how to confirm.
- llms.txt (llmstxt.org): every host in scope should serve /llms.txt (text with an H1 line and links). On hosts
  with sections, each section must be covered by its own /<section>/llms.txt or by a link from the host's file.
  While the host has no usable file, one host finding lists the sections without their own file; section
  findings appear once a host file exists and leaves sections out. Existing files are validated and up to
  MAX_LINKS links per file (on the tenant's hosts) are fetched.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

from core.findings import FindingIds, make_finding

AUDITOR = "ai-audit"
MAX_LINKS = 10
MIN_SIZE_SHARE = 0.5  # a crawler's page below this share of the usual page counts as blocked
AUDIT_TAG = "SiteHealthAudit/0.1 (read-only SEO audit; AI crawler access check)"
_UA = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; {}; +{})"

# robots.txt token -> user agent for the firewall probe (None: the token exists only in robots.txt, or a CDN is
# right to block impersonators, as for Googlebot and Bingbot)
ANSWER_BOTS = {
    "OAI-SearchBot": _UA.format("OAI-SearchBot/1.0", "https://openai.com/searchbot"),
    "ChatGPT-User": _UA.format("ChatGPT-User/1.0", "https://openai.com/bot"),
    "PerplexityBot": _UA.format("PerplexityBot/1.0", "https://perplexity.ai/perplexitybot"),
    "Perplexity-User": _UA.format("Perplexity-User/1.0", "https://perplexity.ai/perplexity-user"),
    "Claude-SearchBot": _UA.format("Claude-SearchBot/1.0", "https://www.anthropic.com"),
    "Claude-User": _UA.format("Claude-User/1.0", "https://www.anthropic.com"),
    "Googlebot": None,  # AI Overviews and AI Mode use Google's search index
    "Bingbot": None,  # Copilot and several assistants use Bing's index
}
TRAINING_BOTS = {
    "GPTBot": _UA.format("GPTBot/1.2", "https://openai.com/gptbot"),
    "ClaudeBot": _UA.format("ClaudeBot/1.0", "claudebot@anthropic.com"),
    "CCBot": "CCBot/2.0 (https://commoncrawl.org/faq/)",
    "Google-Extended": None,
    "Applebot-Extended": None,
    "meta-externalagent": None,
    "Bytespider": None,
}
CHALLENGE_RE = re.compile(r"cf-chl|challenge-platform|captcha|just a moment\.\.\.|attention required", re.I)
LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

# check -> (dimension, type, title, expected, fix_tier, effort)
LLMS_EXPECTED = ("/llms.txt as plain text or Markdown: an H1 with the site name, a short summary and links to the "
                 "key pages")
CHECKS = {
    "ai_bot_blocked": ("ai", "broken", "robots.txt blocks AI search and answer crawlers",
                       "AI search and answer crawlers allowed on public pages", 3, "S"),
    "ai_bot_firewalled": ("ai", "broken", "Firewall or CDN blocks AI search and answer crawlers",
                          "AI crawlers get the same page as other visitors", 3, "S"),
    "llms_txt_missing": ("ai", "improvable", "No llms.txt for AI assistants", LLMS_EXPECTED, 1, "S"),
    "llms_txt_invalid": ("ai", "incorrect", "llms.txt is not a usable file", LLMS_EXPECTED, 1, "S"),
}


# ---- robots.txt (RFC 9309) ----

def parse_robots(text: str) -> list[tuple[set[str], list[tuple[bool, str]]]]:
    """Groups of (user-agent tokens, [(allow, path pattern)]). Consecutive user-agent lines share a group."""
    groups: list = []
    agents: set[str] = set()
    rules: list = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = set(), []
            agents.add(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((key == "allow", value))
    if agents:
        groups.append((agents, rules))
    return groups


def _matches(pattern: str, path: str) -> bool:
    rx = "".join(".*" if c == "*" else "$" if c == "$" and i == len(pattern) - 1 else re.escape(c)
                 for i, c in enumerate(pattern))
    return re.match(rx, path) is not None


def robots_allows(groups: list, token: str, path: str) -> bool:
    """RFC 9309: the groups naming the crawler (else the '*' groups) apply; the longest matching rule wins and
    allow wins a tie; no matching rule means allowed."""
    token = token.lower()
    named = [rules for agents, rules in groups if token in agents]
    rules = [r for rs in (named or [rs for agents, rs in groups if "*" in agents]) for r in rs]
    best: tuple[int, bool] | None = None
    for allow, pattern in rules:
        if pattern and _matches(pattern, path) and (best is None or (len(pattern), allow) > best):
            best = (len(pattern), allow)
    return best is None or best[1]


# ---- llms.txt ----

def llms_state(res, url: str) -> tuple[str, str]:
    """('ok' | 'missing' | 'invalid' | 'error', note) for a fetched llms.txt. Only a text file served at the URL
    can be invalid: an error status, a redirect or a web page means there is no file (many apps send unknown
    URLs to an app or error page); the note keeps what the URL answered."""
    if res.status is None:
        return "error", res.error or "no response"
    if res.status != 200:
        return "missing", f"HTTP {res.status}"
    final = urlsplit(res.final_url or url).path
    if final != urlsplit(url).path:
        return "missing", f"redirects to {res.final_url}"
    body = res.body.decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if "html" in (res.content_type or "").lower() or body[:15].lower().startswith(("<!doctype", "<html")):
        return "missing", "HTTP 200 with a web page (HTML), not a text file"
    if not body.strip():
        return "invalid", "empty file"
    if not any(ln.startswith("# ") for ln in body.splitlines()):
        return "invalid", "no H1 line ('# Site name')"
    if not LINK_RE.search(body):
        return "invalid", "no links to pages"
    return "ok", f"{len(LINK_RE.findall(body))} links, {len(body):,} characters"


def llms_links(body: bytes, base: str) -> list[str]:
    return list(dict.fromkeys(urljoin(base, m) for m in LINK_RE.findall(body.decode("utf-8", "replace"))))


class AiAudit:
    def __init__(self, tenant, fetcher, store, hosts=None, log=print):
        self.tenant, self.fetcher, self.store = tenant, fetcher, store
        self.hosts = [h for h in tenant.host_names if not hosts or h in hosts]
        self.log = log or (lambda *_: None)
        raw = tenant.raw
        self.cfg = (raw.get("audit") or {}).get("ai") or {}
        self.fix_channels = {h["host"]: h.get("fix_channel") for h in tenant.hosts}
        self.sections = {h: self._sections_for(h) for h in self.hosts}
        self.results: dict = {}
        self.run_id: int | None = None

    def _sections_for(self, host: str) -> list[str]:
        cfg = ((self.tenant.raw.get("discovery") or {}).get("section_sitemaps") or {}).get(host) or {}
        src = cfg.get("sections") or []
        return [str(x) for x in (self.tenant.raw.get(src, []) if isinstance(src, str) else src)]

    def _robots(self, host: str) -> dict:
        res = self.fetcher.robots(f"https://{host}")
        out = {"status": res.status, "error": res.error, "answer_blocked": {}, "training_blocked": {}}
        is_html = "html" in (res.content_type or "").lower()
        if res.status != 200 or is_html:  # missing (= allow all) or broken: discovery reports robots.txt itself
            out["note"] = "HTML page" if res.status == 200 else f"HTTP {res.status}" if res.status else res.error
            return out
        groups = parse_robots(res.body.decode("utf-8", "replace"))
        paths = ["/", *(f"/{s}/" for s in self.sections[host])]
        for key, bots in (("answer_blocked", ANSWER_BOTS), ("training_blocked", TRAINING_BOTS)):
            for bot in bots:
                blocked = [p for p in paths if not robots_allows(groups, bot, p)]
                if blocked:
                    out[key][bot] = blocked
        return out

    def _firewall(self, host: str) -> dict:
        url = f"https://{host}/"
        base = self.fetcher.get(url)
        out = {"url": url, "baseline": base.status or base.error, "bytes": len(base.body), "blocked": {}}
        if base.status != 200:
            return out  # the crawl audit reports a failing homepage
        for bot, ua in (*ANSWER_BOTS.items(), *TRAINING_BOTS.items()):
            if not ua:
                continue
            res = self.fetcher.get(url, headers={"User-Agent": f"{ua} {AUDIT_TAG}"})
            text = res.body[:20000].decode("utf-8", "replace")
            if res.status != 200:
                why = f"HTTP {res.status}" if res.status else res.error or "no response"
            elif CHALLENGE_RE.search(text) and not CHALLENGE_RE.search(base.body[:20000].decode("utf-8", "replace")):
                why = "challenge page"
            elif len(res.body) < MIN_SIZE_SHARE * len(base.body):
                why = f"{len(res.body):,} bytes instead of {len(base.body):,}"
            else:
                continue
            out["blocked"][bot] = why
        return out

    def _llms(self, url: str) -> dict:
        res = self.fetcher.get(url)
        state, note = llms_state(res, url)
        out = {"url": url, "state": state, "note": note, "links": []}
        if state != "ok":
            return out
        links = llms_links(res.body, url)
        out["links"] = links
        ours = [u for u in links if urlsplit(u).netloc.lower() in self.tenant.host_names][:MAX_LINKS]
        broken = []
        for u in ours:
            r = self.fetcher.get(u)
            if r.status != 200 and not r.blocked_by_robots:
                broken.append(f"{u} ({f'HTTP {r.status}' if r.status else r.error or 'no response'})")
        if broken:
            out.update(state="invalid", note=f"{len(broken)} of {len(ours)} links checked fail: "
                                             + "; ".join(broken[:3]))
        return out

    def check_host(self, host: str) -> dict:
        r = {"robots": self._robots(host), "firewall": self._firewall(host)}
        if host in (self.cfg.get("llms_txt_skip_hosts") or []):
            r["llms"] = {"state": "skipped", "note": "not expected on this host (tenant audit.ai)"}
            return r
        r["llms"] = self._llms(f"https://{host}/llms.txt")
        if self.sections[host]:
            covered_by_root = set()
            if r["llms"]["state"] == "ok":
                paths = [urlsplit(u).path for u in r["llms"]["links"] if urlsplit(u).netloc.lower() == host]
                covered_by_root = {s for s in self.sections[host]
                                   if any(p == f"/{s}" or p.startswith(f"/{s}/") for p in paths)}
            r["sections"] = {s: self._llms(f"https://{host}/{s}/llms.txt") for s in self.sections[host]}
            for s, v in r["sections"].items():
                v["covered_by_host_file"] = s in covered_by_root
        return r

    def run(self) -> dict:
        started = time.monotonic()
        params = {"hosts": self.hosts}
        self.run_id = self.store.start_run(self.tenant.property, AUDITOR, params)
        for host in self.hosts:
            self.results[host] = self.check_host(host)
            res = self.results[host]
            self.log(f"{host}: robots {res['robots']['status']}, answer bots blocked "
                     f"{len(res['robots']['answer_blocked'])}, firewall blocks {len(res['firewall']['blocked'])}, "
                     f"llms.txt {res['llms']['state']}")
        report = {"tenant": self.tenant.property, "auditor": AUDITOR, "run_id": self.run_id, "params": params,
                  "requests": self.fetcher.requests, "duration_s": round(time.monotonic() - started, 1),
                  "stopped_early": False, "summary": summarize(self.results)}
        report["findings"] = self.findings()
        self.store.finish_run(self.run_id, {k: v for k, v in report.items() if k != "findings"})
        report["hosts"] = self.results
        return report

    def _finding(self, ids, check, host, template, urls, observed, title_suffix="", locations=None) -> dict:
        dimension, ftype, title, expected, tier, effort = CHECKS[check]
        f = make_finding(ids=ids, tenant=self.tenant.property, subdomain=host, template=template,
                         dimension=dimension, type=ftype, check=check, title=title + title_suffix, urls=urls,
                         observed=observed, expected=expected, effort=effort, fix_tier=tier,
                         fix_channel=self.fix_channels.get(host))
        if locations:
            f["evidence"]["locations"] = locations
        return f

    def findings(self) -> list[dict]:
        ids = FindingIds()
        out = []
        for host, r in sorted(self.results.items()):
            blocked = r["robots"]["answer_blocked"]
            if blocked:
                parts = "; ".join(f"{bot} on {', '.join(paths[:4])}{' ...' if len(paths) > 4 else ''}"
                                  for bot, paths in sorted(blocked.items()))
                out.append(self._finding(ids, "ai_bot_blocked", host, "robots.txt", [f"https://{host}/robots.txt"],
                                         f"robots.txt disallows: {parts}"))
            fw = {b: why for b, why in r["firewall"]["blocked"].items() if b in ANSWER_BOTS}
            if fw:
                parts = "; ".join(f"{b}: {why}" for b, why in sorted(fw.items()))
                out.append(self._finding(ids, "ai_bot_firewalled", host, "host", [r["firewall"]["url"]],
                                         f"Homepage answers HTTP {r['firewall']['baseline']} "
                                         f"({r['firewall']['bytes']:,} bytes) normally, but to {parts}"))
            out += self._llms_findings(ids, host, r)
        return out

    def _llms_findings(self, ids, host: str, r: dict) -> list[dict]:
        root, out = r["llms"], []
        if root["state"] in ("skipped", "error"):
            return out
        sections = r.get("sections") or {}
        own = sorted(s for s, v in sections.items() if v["state"] == "ok")
        for s, v in sorted(sections.items()):  # a section's own file that exists but is broken
            if v["state"] == "invalid":
                out.append(self._finding(ids, "llms_txt_invalid", host, f"llms.txt:{s}", [v["url"]],
                                         f"{v['url']}: {v['note']}", f" (/{s}/)"))
        if root["state"] != "ok":
            uncovered = sorted(s for s, v in sections.items() if v["state"] == "missing")
            observed = f"{root['url']}: {root['note']}"
            if sections:
                observed += (f". {len(uncovered)} of {len(sections)} sections have no llms.txt of their own"
                             + (f" ({', '.join(uncovered[:12])}{', ...' if len(uncovered) > 12 else ''})"
                                if uncovered else "")
                             + (f"; own file: {', '.join(own)}" if own else ""))
            unchecked = sorted(s for s, v in sections.items() if v["state"] == "error")
            if unchecked:
                observed += f"; not checked (no response): {', '.join(unchecked)}"
            check = "llms_txt_missing" if root["state"] == "missing" else "llms_txt_invalid"
            locs = [{"role": "page", "url": root["url"], "note": root["note"]}]
            locs += [{"role": "section file", "url": sections[s]["url"], "note": "valid"} for s in own]
            out.append(self._finding(ids, check, host, "llms.txt", [root["url"]], observed, locations=locs))
            return out
        for s, v in sorted(sections.items()):
            if v["state"] == "missing" and not v["covered_by_host_file"]:
                out.append(self._finding(ids, "llms_txt_missing", host, f"llms.txt:{s}", [v["url"]],
                                         f"{root['url']} links no page under /{s}/ and {v['url']} returns "
                                         f"{v['note']}", f" (/{s}/)"))
        return out


def summarize(results: dict) -> dict:
    """Per host, what the report's reference table shows."""
    out = {}
    for host, r in results.items():
        sections = r.get("sections") or {}
        out[host] = {
            "robots": r["robots"]["status"] if r["robots"]["status"] else r["robots"]["error"],
            "answer_blocked": sorted(r["robots"]["answer_blocked"]),
            "training_blocked": sorted(r["robots"]["training_blocked"]),
            "firewall_blocked": {k: v for k, v in r["firewall"]["blocked"].items() if k in ANSWER_BOTS},
            "firewall_training": {k: v for k, v in r["firewall"]["blocked"].items() if k in TRAINING_BOTS},
            "llms": {k: r["llms"].get(k) for k in ("state", "note", "url")},
            "sections": len(sections),
            "sections_covered": sum(1 for v in sections.values()
                                    if v["state"] == "ok" or v.get("covered_by_host_file")),
            "section_files": sorted(s for s, v in sections.items() if v["state"] == "ok"),
        }
    return out


def audit(tenant, fetcher, store, **kw) -> dict:
    return AiAudit(tenant, fetcher, store, **kw).run()


def import_report(findings_store, report: dict) -> dict:
    """Record an AI-audit report in the findings store; the run checked its hosts in full."""
    return findings_store.import_run(report["findings"], f"{AUDITOR}-run{report['run_id']}", AUDITOR,
                                     hosts=report["params"]["hosts"], resolve_unsectioned=True)
