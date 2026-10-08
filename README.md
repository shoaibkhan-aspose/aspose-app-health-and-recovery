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
pip install -e ".[dev]"
cp .env.example .env          # set the key path; the key file stays outside the repo
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/outside/repo/sa-key.json
pytest                        # offline tests
python scripts/check_access.py --tenant aspose.app
```

`check_access.py` is read-only. It reports which GSC properties the service account can read (with 28-day clicks, top pages and sitemap errors) and which hostnames GA4 tracks, then writes `reports/access-check-<date>.json`.
