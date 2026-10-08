"""Read-only GA4 Data API client."""

from __future__ import annotations


class GA4Client:
    def __init__(self, credentials):
        from google.analytics.data_v1beta import BetaAnalyticsDataClient

        self._client = BetaAnalyticsDataClient(credentials=credentials)

    def run_report(
        self,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        start: str = "28daysAgo",
        end: str = "yesterday",
        limit: int = 10000,
    ) -> list[dict]:
        from google.analytics.data_v1beta.types import (
            DateRange,
            Dimension,
            Metric,
            RunReportRequest,
        )

        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start, end_date=end)],
            limit=limit,
        )
        resp = self._client.run_report(request)
        rows = []
        for r in resp.rows:
            row = {d: v.value for d, v in zip(dimensions, r.dimension_values, strict=True)}
            row.update({m: float(v.value) for m, v in zip(metrics, r.metric_values, strict=True)})
            rows.append(row)
        return rows
