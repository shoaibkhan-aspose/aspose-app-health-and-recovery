"""PageSpeed Insights API: one call per page gives both real-user data (CrUX, 28 days, 75th percentile) for the
URL and its origin, and a Lighthouse lab run. Read-only: Google loads the page once from its own servers.

Auth: the service account (identity-only `openid` scope; quota is charged to its Cloud project, where the
PageSpeed Insights API must be enabled), or an API key in PAGESPEED_API_KEY. The keyless quota is shared by
everyone and usually exhausted.
"""

from __future__ import annotations

import os
import time

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
RETRY_STATUSES = (429, 500, 502, 503, 504)

# CrUX metric names in the API -> short name; CLS comes as an integer x100.
FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": "lcp",
    "INTERACTION_TO_NEXT_PAINT": "inp",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls",
    "FIRST_CONTENTFUL_PAINT_MS": "fcp",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb",
}
LAB_METRICS = {
    "largest-contentful-paint": "lcp",
    "total-blocking-time": "tbt",
    "cumulative-layout-shift": "cls",
    "first-contentful-paint": "fcp",
    "speed-index": "si",
}
SAVINGS_MIN_MS = 100  # Lighthouse suggestions below this are noise
TOP_SAVINGS = 3


class PageSpeedError(RuntimeError):
    pass


class PageLoadError(PageSpeedError):
    """The API worked but Lighthouse could not load the page (server error, timeout, not HTML): a finding about
    the page, not a failed measurement."""


def make_session():
    """An HTTP session for the API: API key if PAGESPEED_API_KEY is set, else the service account."""
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        import requests

        s = requests.Session()
        s.params = {"key": key}
        return s
    from google.auth.transport.requests import AuthorizedSession

    from core.google_auth import IDENTITY_SCOPES, get_credentials

    credentials, _ = get_credentials(IDENTITY_SCOPES)
    return AuthorizedSession(credentials)


class PageSpeedClient:
    def __init__(self, session, strategy: str = "mobile", timeout: float = 180, retries: int = 3, sleep=time.sleep):
        self.session, self.strategy, self.timeout, self.retries, self.sleep = session, strategy, timeout, retries, sleep
        self.calls = 0

    def run(self, url: str) -> dict:
        """Raw API response for `url`. Raises PageSpeedError with a short, secret-free message."""
        params = {"url": url, "strategy": self.strategy, "category": "performance"}
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                r = self.session.get(API, params=params, timeout=self.timeout)
            except Exception as e:  # network error or timeout
                err = type(e).__name__
                status = None
            else:
                if r.status_code == 200:
                    return r.json()
                status = r.status_code
                try:
                    err = (r.json().get("error") or {}).get("message", "")[:200]
                except ValueError:
                    err = ""
                if status == 403 and "has not been used" in err:
                    raise PageSpeedError("PageSpeed Insights API is not enabled in the service account's project")
                if status in (400, 500) and "Lighthouse returned error" in err:
                    raise PageLoadError(err.split("Lighthouse returned error: ", 1)[1])
            if attempt < self.retries and (status is None or status in RETRY_STATUSES):
                self.sleep(5 * 2 ** attempt)
                continue
            raise PageSpeedError(f"HTTP {status}: {err}" if status else err)
        raise PageSpeedError("no response")


def _field(exp: dict | None) -> dict | None:
    """CrUX block -> {'id', 'metrics': {short: p75}} (CLS as a decimal), or None if it has no metrics."""
    if not exp or not exp.get("metrics"):
        return None
    metrics = {}
    for name, short in FIELD_METRICS.items():
        p75 = (exp["metrics"].get(name) or {}).get("percentile")
        if p75 is not None:
            metrics[short] = p75 / 100 if short == "cls" else p75
    return {"id": exp.get("id"), "metrics": metrics} if metrics else None


def _savings(audit: dict) -> float:
    details = audit.get("details") or {}
    if details.get("overallSavingsMs"):
        return float(details["overallSavingsMs"])
    ms = audit.get("metricSavings") or {}
    return float(max((ms.get(k) or 0) for k in ("LCP", "FCP", "TBT", "INP")) if ms else 0)


def extract(resp: dict) -> dict:
    """The parts of a PSI response the auditor keeps: URL field data (None when Google only has origin data),
    origin field data, the lab score and metrics, the biggest Lighthouse savings, and any lab error."""
    le = resp.get("loadingExperience") or {}
    url_field = None if le.get("origin_fallback") else _field(le)
    lh = resp.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    score = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    lab = {short: audits[name].get("numericValue") for name, short in LAB_METRICS.items()
           if (audits.get(name) or {}).get("numericValue") is not None}
    savings = sorted(((a.get("title") or k, round(_savings(a))) for k, a in audits.items()
                      if a.get("score") is not None and a["score"] < 0.9 and _savings(a) >= SAVINGS_MIN_MS),
                     key=lambda x: -x[1])
    err = lh.get("runtimeError") or {}
    return {
        "field": url_field,
        "origin": _field(resp.get("originLoadingExperience")),
        "lab_score": round(score * 100) if score is not None else None,
        "lab": lab,
        "savings": [list(s) for s in savings[:TOP_SAVINGS]],
        "final_url": lh.get("finalDisplayedUrl") or lh.get("finalUrl"),
        "lab_error": f"{err.get('code')}: {(err.get('message') or '')[:150]}" if err.get("code") else None,
        "lighthouse": lh.get("lighthouseVersion"),
    }
