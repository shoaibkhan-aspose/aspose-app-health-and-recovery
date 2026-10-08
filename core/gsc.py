"""Read-only Google Search Console client and helpers (windows, filters, period comparison)."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit

# GSC data lags ~2-3 days; ending the window 3 days back avoids partial days.
GSC_LAG_DAYS = 3
DIMENSIONS = ("page", "query", "country", "device", "date", "searchAppearance")
FILTER_OPERATORS = ("contains", "equals", "notContains", "notEquals", "includingRegex", "excludingRegex")
SEARCH_TYPES = ("web", "image", "video", "news", "discover", "googleNews")
API_MAX_ROWS = 25_000


def default_window(days: int = 28, today: date | None = None) -> tuple[str, str]:
    end = (today or date.today()) - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def previous_window(start: str, end: str) -> tuple[str, str]:
    """The window of the same length that ends the day before `start`."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    length = (e - s).days + 1
    prev_end = s - timedelta(days=1)
    return (prev_end - timedelta(days=length - 1)).isoformat(), prev_end.isoformat()


def build_filters(filters: list[dict] | None) -> list[dict]:
    """[{'dimension': 'page', 'operator': 'contains', 'expression': '/slides/'}] -> dimensionFilterGroups (AND)."""
    if not filters:
        return []
    out = []
    for f in filters:
        dim, op, expr = f.get("dimension"), f.get("operator", "equals"), f.get("expression")
        if dim not in DIMENSIONS or dim == "date":
            raise ValueError(f"filter dimension must be one of {[d for d in DIMENSIONS if d != 'date']}: {dim}")
        if op not in FILTER_OPERATORS:
            raise ValueError(f"filter operator must be one of {FILTER_OPERATORS}: {op}")
        if not expr:
            raise ValueError("filter expression is required")
        out.append({"dimension": dim, "operator": op, "expression": str(expr)})
    return [{"groupType": "and", "filters": out}]


def to_records(rows: list[dict], dimensions: list[str] | None) -> list[dict]:
    """API rows -> flat records: {<dimension>: value, ..., clicks, impressions, ctr, position}."""
    out = []
    for r in rows:
        rec = dict(zip(dimensions or [], r.get("keys", []), strict=False))
        rec.update(clicks=int(r.get("clicks", 0)), impressions=int(r.get("impressions", 0)),
                   ctr=round(r.get("ctr", 0.0), 4), position=round(r.get("position", 0.0), 1))
        out.append(rec)
    return out


def compare_records(current: list[dict], previous: list[dict], dimensions: list[str]) -> list[dict]:
    """Join two periods on the dimension values; add deltas. Rows present in only one period count as 0 there."""
    key = lambda r: tuple(r.get(d) for d in dimensions)  # noqa: E731
    prev = {key(r): r for r in previous}
    cur = {key(r): r for r in current}
    out = []
    for k in cur.keys() | prev.keys():
        c, p = cur.get(k, {}), prev.get(k, {})
        rec = dict(zip(dimensions, k, strict=True))
        for m in ("clicks", "impressions"):
            rec[m], rec[f"{m}_prev"] = c.get(m, 0), p.get(m, 0)
            rec[f"{m}_delta"] = rec[m] - rec[f"{m}_prev"]
        rec["position"], rec["position_prev"] = c.get("position"), p.get("position")
        out.append(rec)
    return out


def site_for_url(url: str, sites: list[str]) -> str | None:
    """Pick the property that covers a URL: the longest matching URL-prefix property, else a domain property."""
    prefixes = [s for s in sites if not s.startswith("sc-domain:") and url.startswith(s)]
    if prefixes:
        return max(prefixes, key=len)
    host = urlsplit(url).netloc.lower()
    for s in sites:
        if s.startswith("sc-domain:"):
            domain = s.removeprefix("sc-domain:")
            if host == domain or host.endswith("." + domain):
                return s
    return None


def summarize_inspection(result: dict) -> dict:
    """Keep the URL Inspection fields that matter for an audit."""
    idx = (result.get("inspectionResult") or {}).get("indexStatusResult") or {}
    rich = (result.get("inspectionResult") or {}).get("richResultsResult") or {}
    keys = ("verdict", "coverageState", "indexingState", "robotsTxtState", "pageFetchState", "lastCrawlTime",
            "crawledAs", "googleCanonical", "userCanonical", "sitemap", "referringUrls")
    out = {k: idx.get(k) for k in keys if k in idx}
    if rich:
        out["richResults"] = {"verdict": rich.get("verdict"),
                              "types": [d.get("richResultType") for d in rich.get("detectedItems", [])]}
    out["inspectionResultLink"] = (result.get("inspectionResult") or {}).get("inspectionResultLink")
    return out


class GSCClient:
    def __init__(self, credentials):
        from googleapiclient.discovery import build

        self._svc = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)

    def list_sites(self) -> list[dict]:
        """Sites the identity can see: [{'siteUrl': ..., 'permissionLevel': ...}]."""
        return self._svc.sites().list().execute().get("siteEntry", [])

    def search_analytics(
        self,
        site_url: str,
        start: str,
        end: str,
        dimensions: list[str] | None = None,
        row_limit: int = 1000,
        start_row: int = 0,
        filters: list[dict] | None = None,
        search_type: str = "web",
    ) -> list[dict]:
        body = {"startDate": start, "endDate": end, "rowLimit": min(row_limit, API_MAX_ROWS), "startRow": start_row,
                "type": search_type}
        if dimensions:
            body["dimensions"] = dimensions
        groups = build_filters(filters)
        if groups:
            body["dimensionFilterGroups"] = groups
        resp = self._svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return resp.get("rows", [])

    def list_sitemaps(self, site_url: str) -> list[dict]:
        return self._svc.sitemaps().list(siteUrl=site_url).execute().get("sitemap", [])

    def inspect_url(self, site_url: str, url: str) -> dict:
        body = {"inspectionUrl": url, "siteUrl": site_url}
        return self._svc.urlInspection().index().inspect(body=body).execute()
