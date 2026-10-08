"""Polite HTTP fetcher: identifiable user agent, per-host rate limit, retries, robots.txt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

DEFAULT_USER_AGENT = "SiteHealthAudit/0.1 (read-only SEO audit)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BYTES = 60 * 1024 * 1024  # above the 50 MB sitemap limit, so oversize files are still measurable
MAX_RETRY_WAIT = 60.0


def short_error(exc: Exception) -> str:
    """Short, secret-free error description."""
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}".rstrip(": ")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class FetchResult:
    url: str
    status: int | None
    final_url: str | None = None
    content_type: str | None = None
    body: bytes = b""
    redirects: list[str] = field(default_factory=list)
    redirect_statuses: list[int] = field(default_factory=list)
    headers: dict = field(default_factory=dict)  # lower-case names; repeated headers joined with ", "
    error: str | None = None
    elapsed_ms: int = 0
    truncated: bool = False
    blocked_by_robots: bool = False
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None and not self.truncated


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


class PoliteFetcher:
    """GET-only fetcher. Waits `min_interval` seconds between requests to the same host."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = 1.0,
        timeout: float = 30.0,
        retries: int = 2,
        max_bytes: int = MAX_BYTES,
        client=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if client is None:
            import httpx  # imported late so offline tests don't need the package

            client = httpx.Client(headers={"User-Agent": user_agent}, follow_redirects=True, timeout=timeout)
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.retries = retries
        self.max_bytes = max_bytes
        self.requests = 0
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[FetchResult, RobotFileParser]] = {}

    def robots(self, origin: str) -> FetchResult:
        """Fetch and cache robots.txt for an origin (https://host)."""
        if origin not in self._robots:
            res = self._get(f"{origin}/robots.txt")
            parser = RobotFileParser()
            if res.status == 200 and res.error is None:
                parser.parse(res.body.decode("utf-8", "replace").splitlines())
            elif res.status is not None and 400 <= res.status < 500:
                parser.parse([])  # RFC 9309: robots.txt unavailable (4xx) = allow all
            else:
                parser.parse(["User-agent: *", "Disallow: /"])  # unreachable (5xx/network) = disallow all
            self._robots[origin] = (res, parser)
        return self._robots[origin][0]

    def allowed(self, url: str) -> bool:
        origin = origin_of(url)
        self.robots(origin)
        return self._robots[origin][1].can_fetch(self.user_agent, url)

    def get(self, url: str) -> FetchResult:
        if not self.allowed(url):
            return FetchResult(url=url, status=None, error="disallowed by robots.txt", blocked_by_robots=True)
        return self._get(url)

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def _get(self, url: str) -> FetchResult:
        host = urlsplit(url).netloc
        attempt = 0
        while True:
            self._wait(host)
            res = self._once(url)
            attempt += 1
            retryable = res.status in RETRY_STATUSES or (res.status is None and res.error is not None)
            if attempt > self.retries or not retryable:
                return res
            self._sleep(min(res.retry_after or 2.0**attempt, MAX_RETRY_WAIT))

    def _once(self, url: str) -> FetchResult:
        start = self._clock()
        self.requests += 1
        try:
            with self._client.stream("GET", url) as resp:
                chunks, size, truncated = [], 0, False
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    final_url=str(resp.url),
                    content_type=resp.headers.get("content-type"),
                    body=b"".join(chunks),
                    redirects=[str(r.url) for r in resp.history],
                    redirect_statuses=[r.status_code for r in resp.history],
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    truncated=truncated,
                    retry_after=_retry_after(resp.headers.get("retry-after")),
                    elapsed_ms=int((self._clock() - start) * 1000),
                )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((self._clock() - start) * 1000)
            return FetchResult(url=url, status=None, error=short_error(exc), elapsed_ms=elapsed)
