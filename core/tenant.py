"""Load and validate a tenant config from tenants/<name>.yaml."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_DIR = REPO_ROOT / "tenants"


@dataclass(frozen=True)
class Tenant:
    property: str
    gsc_properties: list[str]
    ga4_property_id: str | None
    hosts: list[dict]
    excluded_host_patterns: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    def is_excluded(self, host: str) -> bool:
        """True if a host must never be crawled or checked (e.g. QA/staging)."""
        return any(fnmatch.fnmatch(host, pattern) for pattern in self.excluded_host_patterns)

    @property
    def sectioned_hosts(self) -> list[str]:
        """Hosts whose findings and stats are split per section (discovery.section_sitemaps)."""
        return list((self.raw.get("discovery") or {}).get("section_sitemaps") or {})

    def sections_for(self, host: str) -> list[str]:
        cfg = ((self.raw.get("discovery") or {}).get("section_sitemaps") or {}).get(host) or {}
        src = cfg.get("sections") or []
        return [str(x) for x in (self.raw.get(src, []) if isinstance(src, str) else src)]

    @property
    def host_names(self) -> list[str]:
        return [h["host"] for h in self.hosts if not self.is_excluded(h["host"])]


def load_tenant(name: str, tenants_dir: Path = TENANTS_DIR) -> Tenant:
    path = tenants_dir / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Tenant config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    for key in ("property", "hosts"):
        if key not in data:
            raise ValueError(f"{path.name}: missing required key '{key}'")

    gsc = data.get("gsc_properties") or []
    for site in gsc:
        if not (site.startswith("https://") and site.endswith("/")) and not site.startswith("sc-domain:"):
            raise ValueError(f"{path.name}: GSC property must be 'https://host/' or 'sc-domain:…': {site}")

    ga4 = data.get("ga4_property_id")
    if ga4 is not None and not str(ga4).isdigit():
        raise ValueError(f"{path.name}: ga4_property_id must be numeric (not a G- measurement ID): {ga4}")

    return Tenant(
        property=data["property"],
        gsc_properties=list(gsc),
        ga4_property_id=str(ga4) if ga4 is not None else None,
        hosts=list(data["hosts"]),
        excluded_host_patterns=list(data.get("excluded_host_patterns") or []),
        raw=data,
    )
