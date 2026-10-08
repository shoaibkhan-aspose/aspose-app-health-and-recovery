"""Read-only Google Search Console client."""

from __future__ import annotations

from datetime import date, timedelta

# GSC data lags ~2-3 days; ending the window 3 days back avoids partial days.
GSC_LAG_DAYS = 3


def default_window(days: int = 28, today: date | None = None) -> tuple[str, str]:
    end = (today or date.today()) - timedelta(days=GSC_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


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
    ) -> list[dict]:
        body = {"startDate": start, "endDate": end, "rowLimit": row_limit, "startRow": start_row}
        if dimensions:
            body["dimensions"] = dimensions
        resp = self._svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return resp.get("rows", [])

    def list_sitemaps(self, site_url: str) -> list[dict]:
        return self._svc.sitemaps().list(siteUrl=site_url).execute().get("sitemap", [])
