"""Pick a small, stable sample of URLs per template from a discovery inventory run.

A group is (host, section, template); section is '' on hosts without configured sections, as in discovery
findings. Per group: `per_template` URLs in the default language (no language segment, or `default_lang`)
plus one URL in each of up to `other_langs` other languages; `per_template_by_host` overrides `per_template`
for single hosts (e.g. a forum with one template and many pages). URLs are ranked by a hash of the URL, so the
same inventory always gives the same sample and findings stay comparable between runs.
"""

from __future__ import annotations

import hashlib
import heapq
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class SampledUrl:
    url: str
    host: str
    section: str
    lang: str | None
    template: str
    group_urls: int  # URLs of this (host, section, template) in the inventory
    source: str = "sitemap"  # sitemap | homepage | extra | search
    sitemap: str | None = None  # the sitemap file that lists the URL (inventory), for evidence


def _rank(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def group_langs(db, run_id: int, sectioned_hosts: set[str]) -> dict[tuple[str, str], set]:
    """Languages per (host, section) group: more than one means the pages are localized."""
    out: dict = defaultdict(set)
    for host, section, lang in db.execute("SELECT DISTINCT host, section, lang FROM urls WHERE run_id = ?", (run_id,)):
        out[(host, section if host in sectioned_hosts else "")].add(lang)
    return dict(out)


def sample_inventory(db, run_id: int, hosts: list[str], sectioned_hosts: set[str], sections: list[str] | None = None,
                     per_template: int = 2, other_langs: int = 2, default_lang: str = "en",
                     max_per_host: int | None = None, per_template_by_host: dict | None = None) -> list[SampledUrl]:
    marks = ", ".join("?" * len(hosts))
    rows = db.execute("SELECT url, host, section, lang, template, sitemap FROM urls "
                      f"WHERE run_id = ? AND host IN ({marks})", [run_id, *hosts])
    default: dict = defaultdict(list)  # group -> heap of (-rank, url, lang, sitemap): the lowest ranks
    other: dict = defaultdict(dict)  # group -> lang -> (rank, url, sitemap)
    counts: dict = defaultdict(int)
    for url, host, section, lang, template, sitemap in rows:
        sec = section if host in sectioned_hosts else ""
        if sections is not None and host in sectioned_hosts and sec not in sections:
            continue  # a section filter narrows only hosts with sections; other hosts are sampled in full
        group = (host, sec, template)
        counts[group] += 1
        rank = _rank(url)
        if lang is None or lang == default_lang:
            heap = default[group]
            item = (_neg(rank), url, lang, sitemap)
            if len(heap) < (per_template_by_host or {}).get(host, per_template):
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
        else:
            best = other[group].get(lang)
            if best is None or rank < best[0]:
                other[group][lang] = (rank, url, sitemap)

    # Per host, take the 1st pick of every group, then the 2nd, ... so a host cap trims evenly across templates.
    by_host: dict = defaultdict(list)  # host -> [[SampledUrl, ...] per group]
    for group in sorted(counts):
        host, sec, template = group
        picks = [(url, lang, sm) for _, url, lang, sm in sorted(default[group], reverse=True)]
        langs = sorted(other[group].items(), key=lambda kv: kv[1][0])
        extra = other_langs if picks else other_langs + 1  # no default-language page: one more localized page
        picks += [(url, lang, sm) for lang, (_, url, sm) in langs[:extra]]
        by_host[host].append([SampledUrl(url, host, sec, lang, template, counts[group], sitemap=sm)
                              for url, lang, sm in picks])

    out: list[SampledUrl] = []
    for host in sorted(by_host):
        groups = by_host[host]
        taken = []
        for i in range(max(len(g) for g in groups)):
            taken += [g[i] for g in groups if i < len(g)]
        out += taken[:max_per_host] if max_per_host is not None else taken
    return out


def _neg(rank: str) -> str:
    """Invert a hex rank so a min-heap keeps the lowest ranks (heap root = the highest kept rank)."""
    return rank.translate(str.maketrans("0123456789abcdef", "fedcba9876543210"))
