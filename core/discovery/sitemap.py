"""Parse XML sitemaps and sitemap indexes: plain, gzipped, BOM-prefixed, or plain-text URL lists."""

from __future__ import annotations

import gzip
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

MAX_ENTRIES = 50_000  # sitemaps.org protocol limit per file
MAX_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB uncompressed
_BOM = b"\xef\xbb\xbf"


@dataclass
class Entry:
    loc: str
    lastmod: str | None = None
    alternates: list[tuple[str, str]] = field(default_factory=list)  # (hreflang, href)


@dataclass
class ParsedSitemap:
    kind: str  # index | urlset | text | unknown
    entries: list[Entry]
    gzipped: bool = False
    had_bom: bool = False
    uncompressed_bytes: int = 0
    error: str | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _entry(elem) -> Entry | None:
    loc, lastmod, alternates = None, None, []
    for child in elem:
        name = _local(child.tag)
        if name == "loc":
            loc = (child.text or "").strip()
        elif name == "lastmod":
            lastmod = (child.text or "").strip() or None
        elif name == "link" and child.get("rel") == "alternate" and child.get("hreflang"):
            alternates.append((child.get("hreflang"), child.get("href") or ""))
    return Entry(loc, lastmod, alternates) if loc else None


def parse_sitemap(body: bytes) -> ParsedSitemap:
    gzipped = body[:2] == b"\x1f\x8b"
    if gzipped:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
                body = f.read(MAX_UNCOMPRESSED + 1)
        except (OSError, EOFError) as exc:
            return ParsedSitemap("unknown", [], gzipped=True, error=f"gzip: {exc}")

    size = len(body)
    had_bom = body.startswith(_BOM)
    data = (body[len(_BOM) :] if had_bom else body).lstrip()

    if not data.startswith(b"<"):
        lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
        if lines and all(ln.startswith(("http://", "https://")) for ln in lines):
            return ParsedSitemap("text", [Entry(ln) for ln in lines], gzipped, had_bom, size)
        return ParsedSitemap("unknown", [], gzipped, had_bom, size, error="not XML and not a plain-text URL list")

    kind, entries = None, []
    try:
        for event, elem in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            name = _local(elem.tag)
            if event == "start":
                if kind is None:
                    kind = {"sitemapindex": "index", "urlset": "urlset"}.get(name, f"<{name}>")
                continue
            if name in ("url", "sitemap"):
                e = _entry(elem)
                if e:
                    entries.append(e)
                elem.clear()
    except ET.ParseError as exc:
        return ParsedSitemap(kind if kind in ("index", "urlset") else "unknown", entries, gzipped, had_bom, size,
                             error=f"XML parse error: {exc}")

    if kind not in ("index", "urlset"):
        return ParsedSitemap("unknown", [], gzipped, had_bom, size,
                             error=f"root element {kind} is not <urlset> or <sitemapindex>")
    return ParsedSitemap(kind, entries, gzipped, had_bom, size)
