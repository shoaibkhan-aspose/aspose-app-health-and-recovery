# Site Health & Recovery Agents

Domain-agnostic agents that audit a web property and report what is broken, not optimized, incorrect or improvable. Phase 1 is **audit only**. The project brief and rules are in [`CLAUDE.md`](CLAUDE.md).

## Layout

```
core/          deterministic logic (no tenant names allowed)
mcp_servers/   MCP servers wrapping core (read-only in Phase 1)
agents/        LLM agents (LangGraph)
tenants/       one YAML per property, e.g. tenants/aspose.app.yaml
scripts/       CLIs, e.g. check_access.py
tests/         offline tests
reports/       generated output (gitignored)
```

## Setup (macOS)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,crawl]"
cp .env.example .env          # set the key path (quote it if it has spaces); the key file stays outside the repo
set -a; source .env; set +a
pytest                        # offline tests
python scripts/check_access.py --tenant aspose.app
python scripts/discover.py --hosts products.aspose.app --sections pdf,words   # sample
python scripts/discover.py                                                  # all hosts
```

Findings from every run, plus the manual ones in `tenants/<tenant>.seed-findings.yaml`, are kept in `data/findings.sqlite` with permanent ids, first/last seen and resolved status (`python scripts/findings.py summary`). `scripts/build_report.py` turns a run plus the store into a shareable HTML page.

## MCP servers

`mcp_servers/gsc`: read-only Search Console tools (`list_properties`, `search_performance`, `page_performance`, `compare_periods`, `list_sitemaps`, `inspect_url`). Needs `pip install -e ".[mcp]"`. Registered for Claude Code in `.mcp.json`; approve it once when Claude Code starts. It reads the key path from `GOOGLE_APPLICATION_CREDENTIALS` or, if unset, from `.env`. Run by hand: `python -m mcp_servers.gsc` (stdio).

`discover.py` is read-only and polite (robots.txt respected, 1 request per host per second, QA hosts skipped). It walks robots.txt and sitemaps for every tenant host, stores the URL inventory in `data/inventory.sqlite` and writes a summary plus sitemap findings to `reports/discovery-<date>-run<N>.json`.

`check_access.py` is read-only. It reports which GSC properties the service account can read (with 28-day clicks, top pages and sitemap errors) and which hostnames GA4 tracks, then writes `reports/access-check-<date>.json`.
