"""Classify a URL into host, section, language and template.

Language: the first path segment (within `lang_max_index`) that is a language code is taken as the
language and removed. Template: the tenant's ordered regexes are matched against the remaining
path; the first match names the template. Without a match, a generic shape is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# ISO 639-1 codes. Tenants can override with `languages` in their YAML.
LANG_CODES = frozenset(
    """aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de
    dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig
    ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi
    mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa
    sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz
    ve vi vo wa wo xh yi yo za zh zu""".split()
)
# Optional suffix: region (pt-br, es-419) or script (zh-hant). Kept strict so "to-word" is not a language.
_LANG_RE = re.compile(r"^([a-z]{2})(?:[-_]([a-z]{2}|\d{3}|hans|hant|latn|cyrl))?$")


@dataclass(frozen=True)
class Classified:
    host: str
    path: str
    section: str
    lang: str | None
    template: str
    params: dict = field(default_factory=dict)


def normalize_lang(segment: str, lang_codes=LANG_CODES) -> str | None:
    m = _LANG_RE.match(segment.lower())
    if not m or m.group(1) not in lang_codes:
        return None
    return m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")


def compile_templates(specs: list[dict] | None) -> list[tuple[str, re.Pattern]]:
    return [(s["name"], re.compile(s["pattern"])) for s in specs or []]


def _shape(segments: list[str]) -> str:
    if not segments:
        return "generic:/"
    return "generic:/{section}" + "/*" * (len(segments) - 1)


def classify_url(url: str, templates=(), lang_codes=LANG_CODES, lang_max_index: int = 1) -> Classified:
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    lang = None
    for i, seg in enumerate(segments[: lang_max_index + 1]):
        lang = normalize_lang(seg, lang_codes)
        if lang:
            del segments[i]
            break

    path = "/" + "/".join(segments) + ("/" if segments and parts.path.endswith("/") else "")
    for name, pattern in templates:
        m = pattern.match(path)
        if m:
            return Classified(parts.netloc.lower(), path, segments[0] if segments else "", lang, name, m.groupdict())
    return Classified(parts.netloc.lower(), path, segments[0] if segments else "", lang, _shape(segments))
