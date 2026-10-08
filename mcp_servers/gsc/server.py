"""Read-only Google Search Console MCP server (stdio).

    python -m mcp_servers.gsc

Credentials: the service-account key named by GOOGLE_APPLICATION_CREDENTIALS. If it is not set, the server
reads it from the repo's .env. Only `.readonly` scopes are requested, and there are no write tools.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from core.google_auth import GSC_READONLY, get_credentials
from core.gsc import (
    DIMENSIONS,
    SEARCH_TYPES,
    build_filters,
    compare_records,
    default_window,
    previous_window,
    site_for_url,
    summarize_inspection,
    to_records,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAX_TOOL_ROWS = 1000  # per call, so one answer cannot flood the conversation
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

mcp = MCPServer(
    "gsc",
    instructions=(
        "Read-only Google Search Console data for the properties the service account can see "
        "(call list_properties first). Data lags about 3 days, so default windows end 3 days ago. "
        "Properties are URL-prefix ('https://www.example.com/') or domain ('sc-domain:example.com'). "
        "Filters are ANDed: [{'dimension': 'page', 'operator': 'contains', 'expression': '/slides/'}]."
    ),
)

_client = None


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Set variables from a KEY=VALUE .env file without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def client():
    """Lazily build the GSC client so the server starts (and lists tools) even before credentials work."""
    global _client
    if _client is None:
        from core.gsc import GSCClient

        load_env()
        try:
            credentials, _identity = get_credentials((GSC_READONLY,))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"credentials: {_err(exc)}") from None
        _client = GSCClient(credentials)
    return _client


def _err(exc: Exception) -> str:
    """Short, secret-free error description."""
    text = str(exc).splitlines()[0][:300] if str(exc) else ""
    return f"{type(exc).__name__}: {text}".rstrip(": ")


def _window(days: int, start_date: str | None, end_date: str | None) -> tuple[str, str]:
    if start_date or end_date:
        if not (start_date and end_date):
            raise ToolError("give both start_date and end_date (YYYY-MM-DD), or neither and use days")
        try:
            if date.fromisoformat(start_date) > date.fromisoformat(end_date):
                raise ToolError("start_date is after end_date")
        except ValueError as exc:
            raise ToolError(f"dates must be YYYY-MM-DD: {exc}") from None
        return start_date, end_date
    if not 1 <= days <= 480:
        raise ToolError("days must be between 1 and 480 (GSC keeps 16 months)")
    return default_window(days)


def _check(dimensions: list[str], row_limit: int, search_type: str = "web", filters: list[dict] | None = None) -> int:
    try:
        build_filters(filters)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    bad = [d for d in dimensions if d not in DIMENSIONS]
    if bad:
        raise ToolError(f"unknown dimensions {bad}; use {list(DIMENSIONS)}")
    if search_type not in SEARCH_TYPES:
        raise ToolError(f"search_type must be one of {list(SEARCH_TYPES)}")
    return max(1, min(row_limit, MAX_TOOL_ROWS))


def _query(site, start, end, dimensions=None, filters=None, row_limit=MAX_TOOL_ROWS, search_type="web"):
    try:
        return client().search_analytics(site, start, end, dimensions=dimensions, row_limit=row_limit,
                                         filters=filters, search_type=search_type)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(_err(exc)) from None


def _totals(site, start, end, filters=None, search_type="web") -> dict:
    rows = to_records(_query(site, start, end, filters=filters, search_type=search_type), None)
    return rows[0] if rows else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}


@mcp.tool(annotations=READ_ONLY)
def list_properties() -> list[dict]:
    """Search Console properties the service account can read, with its permission level."""
    try:
        sites = client().list_sites()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(_err(exc)) from None
    return sorted(({"site": s["siteUrl"], "permission": s.get("permissionLevel")} for s in sites),
                  key=lambda s: s["site"])


@mcp.tool(annotations=READ_ONLY)
def search_performance(
    site: str,
    dimensions: list[str] | None = None,
    days: int = 28,
    start_date: str | None = None,
    end_date: str | None = None,
    filters: list[dict] | None = None,
    row_limit: int = 100,
    search_type: str = "web",
) -> dict:
    """Clicks, impressions, CTR and average position for a property, grouped by dimensions
    (page, query, country, device, date, searchAppearance) and sorted by clicks.
    Window: the last `days` days ending 3 days ago, or start_date..end_date (YYYY-MM-DD).
    Filters (ANDed): [{"dimension": "page", "operator": "contains", "expression": "/slides/"}];
    operators: contains, equals, notContains, notEquals, includingRegex, excludingRegex.
    Also returns totals for the same window and filters."""
    dimensions = dimensions or ["page"]
    limit = _check(dimensions, row_limit, search_type, filters)
    start, end = _window(days, start_date, end_date)
    rows = to_records(_query(site, start, end, dimensions, filters, limit, search_type), dimensions)
    return {"site": site, "window": [start, end], "dimensions": dimensions, "filters": filters or [],
            "totals": _totals(site, start, end, filters, search_type), "rows": rows,
            "truncated": len(rows) >= limit}


@mcp.tool(annotations=READ_ONLY)
def page_performance(url: str, by: str = "query", days: int = 28, match: str = "equals",
                     row_limit: int = 50) -> dict:
    """Performance of one URL (match="equals") or every URL containing a pattern (match="contains"),
    broken down by query, country, device or date. The property is picked automatically."""
    if by not in ("query", "country", "device", "date", "page"):
        raise ToolError("by must be query, country, device, date or page")
    if match not in ("equals", "contains"):
        raise ToolError("match must be equals or contains")
    sites = [s["site"] for s in list_properties()]
    site = site_for_url(url, sites) if url.startswith("http") else None
    if site is None:
        raise ToolError(f"no readable property covers {url}; readable: {sites}")
    limit = _check([by], row_limit)
    start, end = _window(days, None, None)
    filters = [{"dimension": "page", "operator": match, "expression": url}]
    rows = to_records(_query(site, start, end, [by], filters, limit), [by])
    return {"site": site, "url": url, "match": match, "window": [start, end],
            "totals": _totals(site, start, end, filters), "by": by, "rows": rows, "truncated": len(rows) >= limit}


@mcp.tool(annotations=READ_ONLY)
def compare_periods(site: str, dimension: str = "page", days: int = 28, filters: list[dict] | None = None,
                    limit: int = 25, rows_per_period: int = 1000) -> dict:
    """Compare the last `days` days (ending 3 days ago) with the `days` before: totals, plus the biggest
    click drops and gains per dimension value. Only the top `rows_per_period` rows of each period (by clicks)
    are compared, so long-tail values may be missing; totals are exact."""
    _check([dimension], limit, filters=filters)
    per_period = max(1, min(rows_per_period, 5000))
    cur_w = _window(days, None, None)
    prev_w = previous_window(*cur_w)
    cur = to_records(_query(site, *cur_w, [dimension], filters, per_period), [dimension])
    prev = to_records(_query(site, *prev_w, [dimension], filters, per_period), [dimension])
    merged = compare_records(cur, prev, [dimension])
    n = max(1, min(limit, 200))
    drops = sorted((r for r in merged if r["clicks_delta"] < 0), key=lambda r: r["clicks_delta"])[:n]
    gains = sorted((r for r in merged if r["clicks_delta"] > 0), key=lambda r: -r["clicks_delta"])[:n]
    t_cur, t_prev = _totals(site, *cur_w, filters), _totals(site, *prev_w, filters)
    return {
        "site": site, "dimension": dimension, "filters": filters or [],
        "window": list(cur_w), "previous_window": list(prev_w),
        "totals": {"current": t_cur, "previous": t_prev,
                   "clicks_delta": t_cur["clicks"] - t_prev["clicks"],
                   "impressions_delta": t_cur["impressions"] - t_prev["impressions"]},
        "top_drops": drops, "top_gains": gains, "rows_compared": [len(cur), len(prev)],
    }


@mcp.tool(annotations=READ_ONLY)
def list_sitemaps(site: str) -> list[dict]:
    """Sitemaps submitted for a property: status, last read, errors, warnings and discovered URL counts."""
    try:
        sitemaps = client().list_sitemaps(site)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(_err(exc)) from None
    out = []
    for s in sitemaps:
        contents = s.get("contents") or []
        out.append({
            "path": s.get("path"), "type": s.get("type"), "is_index": s.get("isSitemapsIndex"),
            "last_submitted": s.get("lastSubmitted"), "last_downloaded": s.get("lastDownloaded"),
            "pending": s.get("isPending"), "errors": int(s.get("errors", 0) or 0),
            "warnings": int(s.get("warnings", 0) or 0),
            "discovered": sum(int(c.get("submitted", 0) or 0) for c in contents),
        })
    return sorted(out, key=lambda s: s["path"] or "")


@mcp.tool(annotations=READ_ONLY)
def inspect_url(url: str, site: str | None = None) -> dict:
    """Google's view of one URL (URL Inspection API): indexed or not, coverage state, Google-selected vs
    declared canonical, robots.txt state, last crawl, sitemaps and referring URLs. Quota: 2,000 per property
    per day, so use it on samples, not lists."""
    if site is None:
        site = site_for_url(url, [s["site"] for s in list_properties()])
        if site is None:
            raise ToolError(f"no readable property covers {url}")
    try:
        result = client().inspect_url(site, url)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(_err(exc)) from None
    return {"url": url, "site": site, **summarize_inspection(result)}


def main() -> None:
    mcp.run("stdio")
