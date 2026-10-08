# Site Health & Recovery Agents

Domain-agnostic agents that audit a web property, report what is broken, not optimized, incorrect or improvable, and (in a later phase) fix it. Pilot tenant: **aspose.app** and all of its subdomains.

**Goal:** grow organic traffic from classic search and AI answer engines (SEO, GEO, AEO, AIO).

**Owner:** SK. This file is the master brief. A copy lives in the claude.ai Project "aspose.app Health Recovery Agents"; Claude Code in VS Code cannot write to it, so when this file changes meaningfully, tell SK so he can refresh the Project copy.

**Keep this file current:** when a decision is made, a fact is confirmed or a build step finishes, update the relevant section (Status, Seed observations, Open items) in the same session.

## Current phase: 1, AUDIT ONLY

Hard rules until SK explicitly opens Phase 2:

- Do NOT fix, change or write anything on a target site. No PRs or pushes to site repos, no WordPress or Discourse writes, no robots, sitemap or redirect edits.
- Use read-only credentials only: GSC Restricted, GA4 Viewer, read-only repo tokens.
- Do not build or mount write-capable tools yet. Fixer MCP servers belong to Phase 2.
- In this repo: do not commit or push unless SK asks.
- Crawl politely: respect robots.txt, rate-limit, send an identifiable user agent. Functional checks use small synthetic test files at low frequency.
- Out of scope: QA and staging hosts (`*-qa.*`). Do not crawl or check them.

The only output of Phase 1 is a health report made of findings (schema below).

## Operating loop

Discover → Audit → Diagnose → Prioritize → Fix → Verify → Monitor

Phase 1 covers Discover, Audit, Diagnose and Prioritize. Fix and Verify are Phase 2.

## Architecture (confirmed by SK, 2026-10-08)

```
core/         deterministic logic: crawl, parse, validate, score, findings store
mcp_servers/  MCP servers, thin wrappers over core (agent-facing). Not `mcp/`: that name would shadow the `mcp` SDK package
api/          REST, thin wrapper over core (dashboard, CI), later
agents/       LLM agents; they call MCP tools only
tenants/      one YAML per property (aspose.app.yaml)
scripts/      one-off CLIs (e.g. check_access.py)
reports/      generated (gitignored) health reports
```

- **Deterministic checks are code, not LLM calls.** Status codes, redirects, canonicals, hreflang, schema validation and Core Web Vitals must be reproducible and cheap.
- **LLM agents only where judgment is needed:** content correctness, root-cause diagnosis, prioritization, and later writing fixes.
- **Read and write live in separate MCP servers,** so Phase 1 cannot write even by accident.

| MCP server | Phase | Access |
|---|---|---|
| `gsc`, `ga4`, `crawl`, `pagespeed`, `findings` | 1 | read-only (`findings` writes only to our own store) |
| `fix-git`, `fix-wordpress`, `fix-discourse` | 2 | write, not built yet |

Stack (confirmed):

| Piece | Role |
|---|---|
| Python | One language for crawlers, Google APIs, MCP servers and agents |
| MCP Python SDK (`mcp`, FastMCP) | Our MCP servers; stdio transport locally, streamable HTTP later |
| `googleanalytics/google-analytics-mcp` | Google's GA4 server (experimental, read-only); reuse it or wrap the same API ourselves |
| Own `gsc` server | Thin wrapper over the Search Console API, `webmasters.readonly` scope |
| LangGraph | Orchestrates the audit run as a graph: fan out auditors, retry, checkpoint, resume, human pause |
| `langchain-mcp-adapters` | Lets LangGraph agents call our MCP servers; Claude Code calls the same servers directly |
| SQLite → PostgreSQL | Findings store: SQLite file for the pilot, PostgreSQL when multi-tenant, scheduled or shared |

## Where things run

Everything runs on SK's Mac (macOS) from VS Code / Claude Code, which has normal network access to aspose.app and Google APIs. (The earlier claude.ai cloud session could not reach aspose.app; that is why development moved here.)

- Repo: `/Users/Apple/Work/Aspose/GitHub/aspose.app-health-and-recovery/site-health-and-recovery` (remote: GitLab `gitlab.recruitize.ai/.../site-health-and-recovery`, branch `master`, no commits yet).
- Parent folder `/Users/Apple/Work/Aspose/GitHub/aspose.app-health-and-recovery` also has empty `docs/` and `ClaudeChat/`.
- products.aspose.app source (read-only): `/Users/Apple/Work/Aspose/GitHub/aspose-app/products.aspose.app`.
- **Standalone project (SK, 2026-10-08):** do not integrate with `conholdate/blog-team-tools` or its dashboard for now. Everything, including reports and any UI, is built here.
- **Sharing reports:** `scripts/build_report.py` renders a self-contained HTML page from a run JSON; it is published as a private claude.ai Artifact (free, link shared at SK's discretion). Not on public hosting: the report lists site weaknesses.

## Status (2026-10-08)

Done:
- Brief, stack and layout confirmed by SK.
- Scaffold: `pyproject.toml`, `.gitignore` (keys, `.env`, `reports/`), `.env.example`, `README.md`.
- `core/tenant.py` (load + validate tenant YAML, `is_excluded()` for QA hosts), `core/google_auth.py` (ADC from `GOOGLE_APPLICATION_CREDENTIALS`, refuses non-`.readonly` scopes), `core/gsc.py` (sites, searchanalytics, sitemaps; 3-day lag window), `core/ga4.py` (runReport).
- `scripts/check_access.py`: read-only check of GSC properties (permission, 28-day clicks/impressions, top pages, sitemap errors) and GA4 (sessions + organic sessions per hostName). Writes `reports/access-check-<date>.json`; exit 1 if any check fails.
- `tenants/aspose.app.yaml`: hosts, GSC properties, GA4 ID, 29 product families, GSC sitemaps snapshot (38 entries).
- `tests/test_foundation.py`: 8 offline tests with fake clients. Pass; `ruff check .` clean.

- `.venv` set up with `pip install -e ".[dev]"`; `.env` holds the key path (quoted: the path contains a space). Load it with `set -a; source .env; set +a` (scripts do not read `.env` themselves).
- **`check_access.py` run 2026-10-08** (report `reports/access-check-2026-10-08.json`, exit 1 because of GSC gaps). Identity: `sk-service-acc@aspose-app-health-and-recovery.iam.gserviceaccount.com`.
  - GSC (window 2026-09-08..2026-10-05): **only `https://products.aspose.app/` is readable** (`siteRestrictedUser`): 1,629,131 clicks, 23,994,949 impressions; 38 sitemaps, 6 with errors (`/gis/sitemaps/sitemap.xml`, `/omr/sitemap-percentage.xml`, `/omr/sitemap.xml`, `/video-app/sitemap.xml`, `/drawing/sitemap.xml`, root `/sitemap.xml`), matching the GSC UI snapshot. Top pages are barcode locale pages (`/barcode/fa/recognize/qr` 49k clicks, `/barcode/ar/recognize` 49k, `/barcode/ru/recognize/qr` 36k, `/barcode/fa/scan` 33k) and `/pdf/merger/jpg-to-jpg` (31k clicks, 692k impr).
  - about, blog, releases, websites: not visible on the first run; SK added the service account, and the **re-run (2026-10-08) passes, exit 0**. 28-day clicks / impressions / submitted sitemaps: about 15 / 2,306 / 0; blog 154 / 57,088 / 1; releases 6 / 570 / 0; websites 2 / 729 / 1. No sitemap errors. about and releases have **no sitemap submitted** (candidate finding).
  - GSC clicks are far below GA4 organic sessions on small hosts (about 15 vs 2,263; websites 2 vs 594; blog 154 vs 443). GA4 "Organic Search" includes Bing and other engines, but gaps this size suggest misattribution. Investigate before using GA4 organic for prioritization.
  - GA4 `253117495` readable. One property covers all subdomains. Sessions / organic over 28 days: products 2,712,015 / 1,769,580; (empty hostName) 241,398 / 204,923; purchase 9,447 / 7,616; www 8,707 / 4,366; forum 7,545 / 387; dashboard 5,152 / 3,670; about 3,917 / 2,263; blog 2,061 / 443; websites 1,286 / 594; metrics 407 / 51; releases 279 / 12; status 95 / 36; api.products 14,998 / 80.
  - GA4 also shows noise hosts: `politesardine.info` (19,893), `sparklingcattle.com`, `aspose.test`, `about.aspose.org`, `products-aspose-app.translate.goog`, and QA hosts (`products-qa`, `slides-qa-ui-app...k8s.dynabic.com`). Likely ghost/spam or a misplaced tag; a candidate finding (data hygiene, no hostname filter).
  - Hosts not in the tenant YAML: purchase, dashboard, metrics, status, api.products. Discovery should confirm them.

- **Discovery built (2026-10-08):** `core/discovery/` (`fetch` polite GET-only fetcher with robots.txt per RFC 9309, per-host rate limit, retries; `sitemap` parser for XML/gz/BOM/plain-text; `classify` host/section/lang/template; `store` SQLite inventory; `run` orchestration + findings), `core/findings.py` (`make_finding`, schema only; store is step 3), CLI `scripts/discover.py`. Tenant YAML gained `fix_channel` per host, 5 GA4-seen hosts, `discovery.section_sitemaps` and `url_templates`. 20 offline tests pass, ruff clean. Install with `pip install -e ".[dev,crawl]"`.
  - Findings are grouped per host, or per section folder on hosts with configured sections (products), so one template fix = one finding.
  - Sample run (www + products pdf/words/omr, 80 sitemaps, ~80 s) confirmed: products robots.txt 404; root `/sitemap.xml` and both omr sitemaps 404 live (GSC: Couldn't fetch); www robots.txt has no `Sitemap:` line, www sitemaps have no lastmod; pdf language sitemaps have no hreflang; words sitemaps have full hreflang (40 langs, 19,320 URLs = GSC count) but **33 of 36 words sitemaps (14,880 URLs) carry the same lastmod 2024-02-13** (new).

Not done yet:
- GSC properties for www and forum (none exist or none shared; the service account sees only the 5 URL-prefix properties).
- `agents/`, `mcp_servers/` are empty packages.

## Working in this repo

```bash
python3 --version                      # needs 3.11+
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,crawl]"          # extras: mcp, agents, crawl
pytest                                 # offline, no network or keys needed
ruff check .
set -a; source .env; set +a            # loads GOOGLE_APPLICATION_CREDENTIALS (key outside the repo)
python scripts/check_access.py --tenant aspose.app
python scripts/discover.py             # all hosts; --hosts/--sections/--max-sitemaps for samples
python scripts/build_report.py <run json> --notes reports/discovery-notes.txt --out reports/discovery-latest.html
```

Conventions:
- Tests are offline: inject fake clients (see `FakeGSC`, `FakeGA4`); never hit the network or Google in `pytest`.
- Google and HTTP libraries are imported lazily inside functions so offline tests do not need them.
- Errors in reports are short and secret-free (`_err()` pattern). Never print or store key material or tokens.
- `core/` must not contain the string "aspose" (a test enforces this).
- New dependencies go in `pyproject.toml` under the right extra.
- Generated files go to `reports/` (gitignored) or `data/` (gitignored, for SQLite).

## Multi-tenant rule

Nothing in `core/`, `mcp_servers/` or `agents/` may name aspose.app. Everything domain-specific goes in `tenants/<property>.yaml`. The real file is `tenants/aspose.app.yaml`; its keys:

| Key | Meaning |
|---|---|
| `property` | Tenant name |
| `excluded_host_patterns` | Never crawl or check (`*-qa.*`) |
| `gsc_properties` | URL-prefix GSC properties: products, about, blog, releases, websites |
| `ga4_property_id` | `253117495` (confirmed) |
| `hosts` | www, products, about, blog, forum, websites, releases, with platform and known sitemap/robots/repo |
| `product_families` | The 29 `/<family>/` sections of products.aspose.app |
| `gsc_sitemaps_snapshot` | GSC → Sitemaps for products, as of 2026-10-08 (path, type, status, discovered) |

Hierarchy: **property → subdomain → template → page.** Group findings by template: one template fix corrects thousands of pages.

## Tenant 1: aspose.app

| Subdomain | Platform | Role | Fix channel (Phase 2) |
|---|---|---|---|
| www | Unknown | Main entry; 36 language homepages (`/fr/`, `/de/`, …) | Unknown |
| products | Hugo 0.110 | Core landing pages, 29 product families, hundreds of apps | Git PR (hubs only, see below) |
| blog | WordPress 5.7.2 multisite | Top of funnel | WP REST API / WP-CLI |
| forum | Discourse 3.1.1 | Long-tail questions; signal source for broken apps | Admin API, theme |
| about | Hugo 0.135 | Trust and legal; has `llms.txt`, `llms-full.txt`, `.md` endpoints | Git PR |
| websites | Hugo 0.110 | Links across the Aspose network | Git PR |
| releases | Static, platform unconfirmed | Micro-app downloads | Git PR, repo unconfirmed |

SK's list ended with "etc", so the Discovery agent must find every host from DNS, sitemaps and internal links. Do not rely on this table.

### products.aspose.app source repo

- Local: `/Users/Apple/Work/Aspose/GitHub/aspose-app/products.aspose.app` (GitHub `Aspose/products.aspose.app`, branch `main`). Read-only in Phase 1.
- Covers only the homepage and the 29 family hubs: 59 `_index.md` files (`/<family>/` and `/<family>/family/`). The individual app pages (`/pdf/conversion`, `/words/merger`, …) are not in this repo; their source is unknown.
- Theme: `themes/lutsk-aspose-theme` (layouts, shortcodes, `structured-data.html`, `robots.txt` template).
- Deploy: `hugo deploy` to Ceph S3 (staging `products-qa`, production); workflows run manually from `Aspose/products.aspose.app-workflows`.

## Audit dimensions

Each finding answers one of: broken, not optimized, incorrect, improvable.

1. `crawl` – status codes, redirect chains, robots.txt, sitemaps, canonicals, noindex, orphans
2. `perf` – Core Web Vitals (LCP, INP, CLS), rendering, mobile, HTTPS
3. `onpage` – titles, metas, headings, duplicate or thin pages, internal links, alt text
4. `schema` – structured data validity and coverage
5. `intl` – hreflang, translation parity, language and URL consistency
6. `content` – factual accuracy, stale info, intent match, cannibalization
7. `ai` – AI crawler access, answer-ready structure, entity clarity, llms.txt, citations in AI answers
8. `functional` – apps work end to end (upload, convert, download), broken links, JS errors, uptime
9. `ux` – engagement signals, accessibility (WCAG), layout
10. `authority` – backlinks gained and lost, toxic links, brand mentions
11. `competitive` – keywords and SERP or AI features competitors win
12. `cross` – issues between subdomains: stale cross-links, blog vs product cannibalization, mismatched language sets
13. `platform` – outdated CMS or generator versions (security and performance risk)

## Agents

- **Discovery/Inventory** – hosts, URLs, template map
- **Auditors** – one per dimension above
- **Diagnosis/Recovery** – correlates traffic drops with deploys, algorithm updates, index changes
- **Prioritizer** – scores findings by impact × effort, using GSC and GA4 data
- **Reporter** – scorecards per subdomain and for the whole property
- **Fixer, Verifier** – Phase 2 only

## Finding schema (confirmed)

```json
{
  "id": "crawl-0001",
  "tenant": "aspose.app",
  "subdomain": "forum.aspose.app",
  "template": "footer",
  "dimension": "cross",
  "type": "broken | not_optimized | incorrect | improvable",
  "title": "Footer legal links point to a retired host",
  "evidence": {"urls": [], "observed": "", "expected": ""},
  "affected_pages": 0,
  "impact": {"clicks_28d": 0, "impressions_28d": 0, "score": 0},
  "effort": "S | M | L",
  "fix_tier": 1,
  "fix_channel": "discourse-admin",
  "status": "open",
  "detected_at": "2026-10-07"
}
```

Every finding needs evidence a human can re-check. No finding without a URL and an observed value.

## Autonomy tiers (Phase 2, inactive)

1. **Auto-fix and merge:** broken links, missing alt text and meta descriptions, schema errors, typos.
2. **Auto-fix with canary:** titles, canonicals, internal linking, template changes. Small page set first, Verifier checks, then roll out or roll back.
3. **Human approval:** robots.txt, noindex, bulk redirects, URL changes, content removal, platform upgrades.

## Data sources and secrets

- Google Search Console and GA4 through a read-only service account. SK has added the service-account email to both.
- GSC: URL-prefix properties exist for products, about, blog, releases and websites (`https://<host>.aspose.app/`). www and forum are not listed yet. Query each property separately and merge per host.
- GA4 property ID: GA4 → Admin → Property details (a number, not the `G-` measurement ID). Subdomains may share one property or use several.
- Key path comes from `GOOGLE_APPLICATION_CREDENTIALS`. The key file stays outside this repo. Never commit, print or paste keys or tokens.
- Also planned: Bing Webmaster Tools, CrUX/PageSpeed, server logs, own crawl, optional rank and backlink API.
- Blog and forum are not Git-driven; they will need API credentials (read-only in Phase 1).

## Discovery results (full run 3, 2026-10-08; verified live)

534 sitemaps, 584 requests, ~15 min at 1 req/s/host. **769,659 unique URLs** in sitemaps: products 722,942, forum 41,891, metrics 3,780, releases 308, about 234, websites 215 (incl. cross-folder), status 150, blog 153, www 36. purchase, dashboard, api.products: no sitemap. No unknown aspose.app hosts referenced in sitemaps.

- **robots.txt:** missing (404) on products, about, websites, dashboard, api.products. purchase returns an HTML page for robots.txt. Present but no `Sitemap:` line on www, blog, metrics, status. Only forum and releases list their sitemap.
- **products sitemaps vs GSC:** most families match GSC exactly. Differences explained:
  - **slides:** `/slides/sitemaps/slides.xml` lists its 16 children as `https://products.aspose.app//slides/...` (double slash) → HTTP 503; the single-slash URLs work (24,752 URLs). GSC still says Success/24,804, likely from an older read. Verified with curl.
  - **imaging:** 229,372 in GSC vs 149,240 unique live; the imaging sitemaps list **80,132 duplicate entries** inside files (GSC counts duplicates).
  - Broken live: root `/sitemap.xml` 404, `/omr/sitemap.xml` and `/omr/sitemap-percentage.xml` 404 (omr: no working sitemap at all), `/drawing/sitemap.xml` 503 (drawing: none), `/gis/sitemaps/sitemap.xml` 404 (gis still covered by `/gis/static/sitemap/*`), `/video-app/sitemap.xml` 200 but empty.
  - Small gaps: 3d 44,590 → 43,680 live, psd 13,300 → 12,768, gis 18,178 → 17,739 (sitemaps shrank since GSC read).
- **hreflang in sitemaps:** full on words, cad, ebook (products), about, metrics, www. Partial: email 22%, finance 2%, websites ~50%. **None on the other 21 products families**, incl. cells (228k), imaging (149k), audio, diagram, video, 3d, pdf. Page HTML still to check in the intl audit.
- **lastmod:** every URL in a file shares one date (bulk-stamped) in 22 products families; none at all on about, metrics, releases, status, websites, www, total, slides.
- **Duplicates across sitemaps:** small (diagram 267 via `sitemap_update.xml`, websites folders, www 35, forum 22).
- **Hosts to ask SK about:** metrics.aspose.app exposes 3,780 usage/subscription pages in 36 languages via sitemap; status.aspose.app lists 150 incident/date-query URLs (`?start_date=`). Should these be indexed at all?
- **Soft 404 (products):** `/email/sitemap.xml` redirects to `/email/error?code=404`, which returns HTTP 200. Check app error pages for soft 404s in the crawl audit.
- **Run 6 (after fixes):** slides recovered via corrected child URLs (24,752); products 747,694 URLs, total ~794k; 89 findings. Report page built by `scripts/build_report.py` (template `scripts/templates/discovery_report.html`), takeaways in `reports/discovery-notes.txt` (gitignored).
- Discovery fixes after run 3: double-slash child check (fetches the corrected URL too); parse/redirect/empty checks only on real sitemaps (robots, GSC, child), not on guessed URLs; duplicate findings grouped per host unless the host has sections.

## Seed observations (unverified unless marked, re-check before reporting)

- products, **verified live and in repo:** `/pdf/` title and H1 read "Free Online Apps to view, convert, edit **test** PDF" (`content/pdf/_index.md`).
- products: `/robots.txt` and `/sitemap.xml` returned 404 to a web fetcher; confirm with curl from the Mac. robots.txt and sitemaps are per host, so www's files do not cover products.
- products repo: the Hugo build does generate both files (`enableRobotsTXT = true`, Hugo's default sitemap is not disabled), and live canonicals are absolute, so CI overrides `baseURL = "/"`. Likely cause of the 404: products.aspose.app is path-routed (Hugo hubs in S3 + a separate app platform), and `/robots.txt` and `/sitemap.xml` are not routed to the Hugo bucket. Confirm with whoever owns the routing / CDN.
- products repo: robots template emits only `User-agent: *` (no `Sitemap:` line); unchanged since first commit (2024-02-26).
- products app pages (e.g. `/pdf/conversion`): separate platform (`/pdf/apps-ui/` assets), absolute canonical, locale variants at `/<family>/<lang>/<app>`, but **no hreflang tags seen** in page HTML. Verify on raw HTML.
- products app sitemaps exist **per family** at `/<family>/sitemap.xml` (sitemap indexes), each in a different format, which suggests a separate codebase per family:

  | Family | Child sitemaps | Languages | lastmod | hreflang |
  |---|---|---|---|---|
  | imaging | `/imaging/sitemap.<lang>.xml` | 52 | none on index; every URL in `en` = 2026-04-17 (bulk-stamped) | none |
  | cells | `/cells/sitemap-<lang>.xml` | 37 | all 2026-09-21 | none |
  | pdf | `/pdf/sitemaps/pdf.<lang>.xml` + `llms.xml` | 29 | per URL, 2026-08-26..09-30 | none |
  | words | `/words/common/<tool>.xml.gz` (split by tool, not language; gzipped, download + decompress) | 40 (per SK) | present | none |

  `imaging/sitemap.en.xml` has 500+ URLs, `pdf/sitemaps/pdf.en.xml` ~573 (fetcher truncated both; counts are minimums). Patterns: `/<family>/<tool>`, `/<family>/conversion/<fmt>`, `/<family>/conversion/<src>-to-<dst>`.
  Discovery must probe `/<family>/sitemap.xml` for all 29 families. With no root `/sitemap.xml` and no robots.txt, search engines find these only if submitted in GSC.
- products, **from GSC → Sitemaps (SK, 2026-10-08):** 38 sitemaps submitted, all 29 families covered, ~880k discovered URLs in total. Full list in `tenants/aspose.app.yaml`.
  - imaging (229k) and cells (228k) are 52% of all discovered URLs. Compare with indexed counts: likely index bloat or thin programmatic pages.
  - **No working sitemap:** omr (both sitemaps "Couldn't fetch" since mid-2025, 50k URLs previously discovered) and drawing ("Couldn't fetch").
  - Also failing: `/gis/sitemaps/sitemap.xml` (since Apr 2025; the `/gis/static/sitemap/*` ones work), root `/sitemap.xml` (matches the 404), `/video-app/sitemap.xml` (1 error, 0 URLs).
  - Legacy or overlapping entries: diagram `sitemap_update.xml`, psd `sitemap.en.xml` beside its index, finance `sitemap_all.xml`, three gis sitemaps.
  - At least 6 path conventions (`/sitemap.xml`, `/sitemaps/<x>.xml`, `/sitemap/sitemap.xml`, `/static/sitemap/…`, `/sitemap` without extension), more evidence of one codebase per family.
  - GSC API can list sitemaps but not the Page indexing report; indexed-vs-discovered per family needs the UI export or URL Inspection sampling (2,000/day per property).
- products: language sets differ per family (imaging 52, words 40, cells 37, pdf 29), so translation parity is uneven.
- products/pdf: `pdf/sitemaps/llms.xml` is a sitemap whose only URL is `/pdf/llms.txt`. Sitemap lists `/pdf/split` while the homepage links `/pdf/splitter`; check for redirects or duplicates.
- products/imaging: near-duplicate conversion targets (`jpg-to-jp2`, `jpg-to-j2k`, `jpg-to-jpeg2000`); check for cannibalization / thin pages. The theme's `alt-links.html` already has hreflang logic for exactly this URL pattern (`langdirlevel`, `locales` params), but only `/svg/`, `/html/`, `/total/` pages use it; hub pages have no hreflang.
- www: `sitemap.xml` is an index of 36 language sitemaps, no `lastmod` anywhere. `/fr/sitemap.xml` (and presumably the others) holds 1 URL with 36 hreflang alternates; `/en/sitemap.xml` is inconsistent: it lists all 36 homepages, with alternates only on the root. Whole index covers only the 36 www homepages.
- www: `robots.txt` has only `Allow: /$` and `Allow: /sitemap.xml` (allows everything) and no `Sitemap:` line.
- products repo: `config.toml` still carries a Universal Analytics ID (`UA-…`, retired 2023); GA tracking itself comes via GTM.

- blog: WordPress 5.7.2; few recent posts; categories with 0 articles.
- forum: footer ToS and Privacy links point to `company.aspose.app`; Diagram category description says "Microsoft Project".
- websites: aspose.net logo alt text says "aspose.org"; 6 languages vs 13 on about.
- releases: no canonical or meta description seen on the homepage.
- about: already AI-readable (`llms.txt`); check whether other subdomains match.

## Next steps (in order)

1. ~~Run `check_access.py`~~ Done 2026-10-08 (see Status). All 5 URL-prefix properties are readable. Remaining gap: www and forum (and any other hosts Discovery finds) have no GSC property; a Domain property would cover them all. Not blocking step 2.
2. **Discovery (inventory)**: done. Latest clean run: **run 6** (550 sitemaps, ~794k unique URLs, 89 findings). Shared report: https://claude.ai/artifact/MUUTzbXEymKq4tw5mmmPA5 (private; SK shares via its Share menu). To refresh: `discover.py` → `build_report.py <run json> --notes reports/discovery-notes.txt --out reports/discovery-latest.html` → republish that file. Original spec:
   - Fetch robots.txt and root sitemap per host; probe `/<family>/sitemap.xml` for all 29 families plus every path in `gsc_sitemaps_snapshot`.
   - Recurse sitemap indexes; handle `.xml.gz` (words) and BOMs; record per sitemap: URL, status, type, child count, URL count, lastmod spread, hreflang presence.
   - Normalise URLs to an inventory: host, family, language (from path), tool/template pattern (`/<family>/<tool>`, `/<family>/conversion/<fmt>`, `/<family>/conversion/<src>-to-<dst>`, `/<family>/<lang>/<tool>`).
   - Store in SQLite (`data/inventory.sqlite`, gitignored). Polite: identifiable user agent, rate limit, retries, respect robots.txt, skip excluded hosts.
   - Output: per-family / per-language URL counts vs the GSC snapshot; sitemap problems as findings.
3. **Findings store** (`core/findings.py`, SQLite) using the confirmed schema.
4. **`mcp_servers/gsc`** (FastMCP, stdio, read-only) wrapping `core/gsc.py`; register with Claude Code.
5. **Crawl auditor** on a sample per template (status, canonical, hreflang, title/H1/meta, robots meta), then the other dimensions.
6. **LangGraph** run graph once 2+ auditors exist; Prioritizer and Reporter after that.

## Open items

- Source of the individual app pages on products, and repos for about, websites, releases, not shared yet. This repo holds the agents, not the sites.
- GA4 property `253117495` is readable and covers all subdomains (verified 2026-10-08). GSC: service account reads all 5 URL-prefix properties (products, about, blog, releases, websites). GSC properties for www and forum still missing. SK (2026-10-08): **all subdomains are in scope**, so GSC must cover every host. Recommended: a Domain property `sc-domain:aspose.app` (DNS-verified, covers all subdomains) with the service account added as Restricted user. Auth stays service account, not SK's personal OAuth (see conversation 2026-10-08).
- GA4: 241k sessions with an empty hostName (205k organic) and spam/foreign hosts. Ask who owns the GA4 setup whether a hostname filter exists.
- Page indexing (indexed vs discovered) per family: needs SK's export from GSC → Indexing → Pages, or URL Inspection sampling.
- SK checking: who owns routing/CDN for products.aspose.app, and where the app pages are built. Not blocking Phase 1: audit runs on the live site; repos are needed for root cause and Phase 2, requested only for the highest-impact families.
- Full subdomain list to be produced by Discovery.
