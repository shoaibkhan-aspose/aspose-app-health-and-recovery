"""Finding records, in the schema confirmed in CLAUDE.md. The findings store (SQLite) comes later."""

from __future__ import annotations

from collections import Counter
from datetime import date

FINDING_TYPES = ("broken", "not_optimized", "incorrect", "improvable")
EFFORTS = ("S", "M", "L")


class FindingIds:
    """Sequential ids per dimension: crawl-0001, crawl-0002, intl-0001, ..."""

    def __init__(self):
        self._n: Counter = Counter()

    def next(self, dimension: str) -> str:
        self._n[dimension] += 1
        return f"{dimension}-{self._n[dimension]:04d}"


def make_finding(
    *,
    ids: FindingIds,
    tenant: str,
    subdomain: str,
    template: str,
    dimension: str,
    type: str,  # noqa: A002 - schema field name
    title: str,
    urls: list[str],
    observed: str,
    expected: str,
    affected_pages: int = 0,
    effort: str = "S",
    fix_tier: int | None = None,
    fix_channel: str | None = None,
    detected_at: str | None = None,
) -> dict:
    if type not in FINDING_TYPES:
        raise ValueError(f"finding type must be one of {FINDING_TYPES}: {type}")
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}: {effort}")
    if not urls or not observed:
        raise ValueError("every finding needs at least one URL and an observed value")
    return {
        "id": ids.next(dimension),
        "tenant": tenant,
        "subdomain": subdomain,
        "template": template,
        "dimension": dimension,
        "type": type,
        "title": title,
        "evidence": {"urls": list(urls), "observed": observed, "expected": expected},
        "affected_pages": affected_pages,
        "impact": {"clicks_28d": 0, "impressions_28d": 0, "score": 0},  # filled in by the Prioritizer
        "effort": effort,
        "fix_tier": fix_tier,
        "fix_channel": fix_channel,
        "status": "open",
        "detected_at": detected_at or date.today().isoformat(),
    }
