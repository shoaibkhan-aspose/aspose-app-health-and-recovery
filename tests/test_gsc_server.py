"""Offline tests for the gsc MCP server and core/gsc helpers (fake client, no network)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from core.gsc import build_filters, compare_records, previous_window, site_for_url  # noqa: E402
from mcp_servers.gsc import server  # noqa: E402

SITE = "https://app.example.com/"


class FakeGSC:
    def __init__(self):
        self.calls = []

    def list_sites(self):
        return [{"siteUrl": SITE, "permissionLevel": "siteRestrictedUser"},
                {"siteUrl": "sc-domain:other.org", "permissionLevel": "siteFullUser"}]

    def search_analytics(self, site, start, end, dimensions=None, row_limit=1000, filters=None, search_type="web"):
        self.calls.append({"site": site, "start": start, "end": end, "dimensions": dimensions,
                           "row_limit": row_limit, "filters": filters})
        if not dimensions:
            return [{"clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 7.25}]
        late = start > "2026-09-01"
        return [
            {"keys": [f"{SITE}a"], "clicks": 50 if late else 80, "impressions": 500, "ctr": 0.1, "position": 3.0},
            {"keys": [f"{SITE}b"], "clicks": 30 if late else 10, "impressions": 300, "ctr": 0.1, "position": 5.0},
        ][:row_limit]

    def list_sitemaps(self, site):
        return [{"path": f"{SITE}s.xml", "errors": "1", "warnings": "0",
                 "contents": [{"type": "web", "submitted": "40"}, {"type": "image", "submitted": "2"}]}]

    def inspect_url(self, site, url):
        return {"inspectionResult": {"inspectionResultLink": "https://search.google.com/x",
                "indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed",
                                      "googleCanonical": url, "userCanonical": url, "ignored": "x"}}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeGSC()
    monkeypatch.setattr(server, "_client", f)
    return f


def call(name, args=None):
    result = asyncio.run(server.mcp.call_tool(name, args or {}))
    data = result.structured_content
    if data is None:
        return json.loads(result.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def test_tools_are_all_read_only():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"list_properties", "search_performance", "page_performance", "compare_periods",
                     "list_sitemaps", "inspect_url"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


def test_search_performance_passes_filters_and_caps_rows(fake):
    out = call("search_performance", {"site": SITE, "dimensions": ["page"], "row_limit": 5000,
                                      "filters": [{"dimension": "page", "operator": "contains",
                                                   "expression": "/slides/"}]})
    assert fake.calls[0]["row_limit"] == server.MAX_TOOL_ROWS
    assert fake.calls[0]["filters"][0]["expression"] == "/slides/"
    assert out["totals"]["clicks"] == 100 and out["rows"][0]["page"] == f"{SITE}a"


def test_page_performance_picks_property(fake):
    out = call("page_performance", {"url": f"{SITE}pdf/split", "by": "query"})
    assert out["site"] == SITE and fake.calls[0]["filters"] == [
        {"dimension": "page", "operator": "equals", "expression": f"{SITE}pdf/split"}]
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("page_performance", {"url": "https://nope.net/x"}))


def test_compare_periods_reports_drops_and_gains(fake, monkeypatch):
    monkeypatch.setattr(server, "default_window", lambda days: ("2026-09-08", "2026-10-05"))
    out = call("compare_periods", {"site": SITE})
    assert out["previous_window"] == ["2026-08-11", "2026-09-07"]
    assert out["top_drops"][0]["page"] == f"{SITE}a" and out["top_drops"][0]["clicks_delta"] == -30
    assert out["top_gains"][0]["page"] == f"{SITE}b" and out["top_gains"][0]["clicks_delta"] == 20


def test_sitemaps_and_inspection(fake):
    sm = call("list_sitemaps", {"site": SITE})
    assert sm[0]["errors"] == 1 and sm[0]["discovered"] == 42
    ins = call("inspect_url", {"url": f"{SITE}pdf/"})
    assert ins["site"] == SITE and ins["verdict"] == "PASS" and "ignored" not in ins


def test_bad_input_is_a_tool_error(fake):
    for args in ({"site": SITE, "dimensions": ["bogus"]}, {"site": SITE, "days": 0},
                 {"site": SITE, "start_date": "2026-10-01"},
                 {"site": SITE, "filters": [{"dimension": "page", "operator": "like", "expression": "x"}]}):
        with pytest.raises(ToolError):
            asyncio.run(server.mcp.call_tool("search_performance", args))


def test_helpers():
    assert previous_window("2026-09-08", "2026-10-05") == ("2026-08-11", "2026-09-07")
    assert site_for_url("https://www.other.org/x", ["sc-domain:other.org"]) == "sc-domain:other.org"
    assert site_for_url("https://app.example.com/a/b", [SITE, "https://app.example.com/a/"]) == \
        "https://app.example.com/a/"
    assert build_filters(None) == []
    merged = compare_records([{"page": "x", "clicks": 1, "impressions": 2}], [], ["page"])
    assert merged[0]["clicks_prev"] == 0 and merged[0]["clicks_delta"] == 1


def test_load_env_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# c\nGOOGLE_APPLICATION_CREDENTIALS="/keys/my key.json"\nTENANT=x\n')
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("TENANT", "keep")
    server.load_env(env)
    import os
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/keys/my key.json" and os.environ["TENANT"] == "keep"


def test_mcp_servers_name_no_tenant():
    for f in (ROOT / "mcp_servers").rglob("*.py"):
        assert "aspose" not in f.read_text().lower(), f"{f.name} names a tenant"
