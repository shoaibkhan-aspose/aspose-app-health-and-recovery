"""Parse the SEO-relevant facts of one HTML page: title, meta description, H1, canonical, robots, hreflang.

Head-only signals (canonical, hreflang, robots meta) are read from <head> only, as search engines do.
HTTP headers add `Link: rel=canonical` and `X-Robots-Tag`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

_LINK_HEADER_RE = re.compile(r'<([^>]+)>\s*;([^,]*)')


@dataclass
class PageFacts:
    titles: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    h1: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)  # absolute, from <head> and the Link header
    head_canonicals: int = 0  # how many of them are <link> tags in <head>
    robots: list[str] = field(default_factory=list)  # meta robots/googlebot + X-Robots-Tag values, lower case
    hreflang: list[tuple[str, str]] = field(default_factory=list)  # (code as written, absolute href)
    html_lang: str | None = None
    viewport: str | None = None
    generator: str | None = None
    refresh: str | None = None  # <meta http-equiv=refresh> content

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def noindex(self) -> bool:
        return any("noindex" in r or "none" in [t.strip() for t in r.split(",")] for r in self.robots)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _rel_tokens(value: str | None) -> set[str]:
    return {t.lower() for t in (value or "").split()}


def link_header_canonicals(value: str | None, base_url: str) -> list[str]:
    out = []
    for target, params in _LINK_HEADER_RE.findall(value or ""):
        m = re.search(r'rel\s*=\s*"?([^";]+)"?', params, re.I)
        if m and "canonical" in _rel_tokens(m.group(1)):
            out.append(urljoin(base_url, target.strip()))
    return out


def parse_page(html: bytes | str, base_url: str, headers: dict | None = None) -> PageFacts:
    from selectolax.lexbor import LexborHTMLParser  # imported late so offline tests of other modules don't need it

    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    tree = LexborHTMLParser(text)
    headers = headers or {}
    facts = PageFacts()

    root = tree.css_first("html")
    if root is not None:
        facts.html_lang = (root.attributes.get("lang") or "").strip() or None

    facts.titles = [_clean(n.text()) for n in tree.css("head title")]
    for n in tree.css("head meta"):
        attrs = {k.lower(): v for k, v in n.attributes.items()}
        name = (attrs.get("name") or "").strip().lower()
        content = attrs.get("content")
        if (attrs.get("http-equiv") or "").strip().lower() == "refresh":
            facts.refresh = _clean(content)
        if name == "description":
            facts.descriptions.append(_clean(content))
        elif name in ("robots", "googlebot"):
            facts.robots.append(_clean(content).lower())
        elif name == "viewport":
            facts.viewport = _clean(content)
        elif name == "generator":
            facts.generator = _clean(content)

    for n in tree.css("head link"):
        rel = _rel_tokens(n.attributes.get("rel"))
        href = (n.attributes.get("href") or "").strip()
        if not href:
            continue
        if "canonical" in rel:
            facts.canonicals.append(urljoin(base_url, href))
        elif "alternate" in rel and n.attributes.get("hreflang"):
            facts.hreflang.append((n.attributes["hreflang"].strip(), urljoin(base_url, href)))

    facts.head_canonicals = len(facts.canonicals)
    facts.canonicals += link_header_canonicals(headers.get("link"), base_url)
    if headers.get("x-robots-tag"):
        facts.robots.append(headers["x-robots-tag"].lower())
    facts.h1 = [_clean(n.text(deep=True)) for n in tree.css("h1")]
    return facts
