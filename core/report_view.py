"""What the report shows beyond the raw findings: confidence, root-cause merging, systemic issues, top risks,
the grade summary and the changes since the last run. Deterministic; computed when the report is built."""

from __future__ import annotations

from collections import Counter, defaultdict

from core.findings import template_section
from core.fix_guide import GUIDE, guide_key
from core.scorecard import TYPE_ORDER, effective_type, family_of, group_of

MERGE_GROUPS = {"sitemap"}  # root causes whose findings are one fix (sitemap down, double-slash children, ...)
SYSTEMIC_MIN_FAMILIES = 3
GRADES = ("A", "B", "C", "D", "F")


def is_open(f: dict) -> bool:
    return f["status"] in ("open", "unverified")


def confidence(f: dict) -> str:
    """complete = whole population checked; strong / partial / to confirm = share of the sample that failed."""
    if not f.get("check"):
        return "to confirm" if f["status"] == "unverified" else "manual"
    sample = (f["evidence"] or {}).get("sample")
    if not sample:
        return "complete"
    failed = sum(v[0] for v in sample.values())
    sampled = sum(v[1] for v in sample.values())
    if failed <= 1:
        return "to confirm"
    return "strong" if sampled and failed / sampled >= 0.5 else "partial"


def score_of(f: dict) -> int:
    return (f.get("impact") or {}).get("score") or 0


def merge_root_causes(findings: list[dict], host: str, families: set) -> None:
    """Findings that share one root cause in one family (e.g. 'sitemap cannot be fetched' and 'no working
    sitemap') are shown once: the worst becomes the primary, the others get `_merged_into`."""
    groups: dict = defaultdict(list)
    for f in findings:
        if not is_open(f) or not f.get("check") or group_of(f) not in MERGE_GROUPS:
            continue
        fam = family_of(f, host, families) if f["subdomain"] == host else template_section(f["template"]) or ""
        groups[(f["subdomain"], fam, group_of(f))].append(f)
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda f: (TYPE_ORDER.index(effective_type(f)), -score_of(f), f["id"]))
        primary = items[0]
        primary["_merged"] = [f["id"] for f in items[1:]]
        for f in items[1:]:
            f["_merged_into"] = primary["id"]


def systemic_issues(findings: list[dict], host: str, families: set) -> list[dict]:
    """One row per check that shows up in several families: likely one shared component, one fix."""
    by_check: dict = defaultdict(list)
    for f in findings:
        if not is_open(f) or f.get("_merged_into"):
            continue
        key = guide_key(f.get("check"), f.get("source"))
        fam = family_of(f, host, families)
        if key and fam:
            by_check[key].append((fam, f))
    out = []
    for key, items in by_check.items():
        fams = sorted({fam for fam, _ in items})
        if len(fams) < SYSTEMIC_MIN_FAMILIES:
            continue
        clicks = sum((f.get("impact") or {}).get("clicks_28d") or 0 for _, f in items)
        worst = min((effective_type(f) for _, f in items), key=TYPE_ORDER.index)
        out.append({"check": key, "name": GUIDE[key]["name"], "area": GUIDE[key]["area"], "type": worst,
                    "families": fams, "findings": [f["id"] for _, f in items], "clicks": clicks,
                    "score": max(score_of(f) for _, f in items)})
    return sorted(out, key=lambda r: (TYPE_ORDER.index(r["type"]), -len(r["families"]), -r["clicks"]))


def top_risks(findings: list[dict], host: str, families: set, limit: int = 5) -> list[dict]:
    """The highest-scoring open findings, one per kind of problem; a kind seen in several families says so."""
    seen: dict = {}
    for f in sorted((f for f in findings if f["status"] == "open" and not f.get("_merged_into")),
                    key=lambda f: (-score_of(f), f["id"])):
        key = guide_key(f.get("check"), f.get("source")) or f["id"]
        fam = family_of(f, host, families)
        if key in seen:
            seen[key]["also"].add(fam or f["subdomain"])
            continue
        if len(seen) < limit:
            seen[key] = {"id": f["id"], "name": GUIDE[key]["name"] if key in GUIDE else f["title"],
                         "where": f"/{fam}/" if fam else f["subdomain"], "type": effective_type(f),
                         "score": score_of(f), "impact": f.get("impact") or {}, "also": set()}
    risks = list(seen.values())
    for r in risks:
        r["also"] = sorted(a for a in r["also"] if a and f"/{a}/" != r["where"] and a != r["where"])
    return risks


def grade_summary(cards: list[dict]) -> dict:
    """(c) the grade distribution, plus (b) the share of search clicks that lands on each grade and the
    low-graded families that carry real traffic."""
    clicks = {c["family"]: ((c.get("traffic") or {}).get("current") or {}).get("clicks") or 0 for c in cards}
    total = sum(clicks.values()) or 1
    dist = Counter(c["grade"] for c in cards)
    share = {g: round(100 * sum(clicks[c["family"]] for c in cards if c["grade"] == g) / total) for g in GRADES}
    weak = sorted(({"family": c["family"], "grade": c["grade"], "share": round(100 * clicks[c["family"]] / total)}
                   for c in cards if c["grade"] in ("D", "F") and clicks[c["family"]] / total >= 0.05),
                  key=lambda x: -x["share"])
    return {"distribution": {g: dist.get(g, 0) for g in GRADES}, "click_share": share, "weak_with_traffic": weak,
            "families": len(cards)}


def run_changes(findings: list[dict], run_refs: list[str]) -> dict:
    """Findings first seen or resolved in the latest runs. A run that is its source's first is not 'new'."""
    def first_of_source(ref: str) -> bool:
        prefix, _, n = ref.rpartition("run")
        return not any((f.get("first_seen_run") or "").startswith(prefix) and
                       int((f.get("first_seen_run") or "run0").rpartition("run")[2] or 0) < int(n or 0)
                       for f in findings)
    refs = [r for r in run_refs if r and not first_of_source(r)]
    new = [f["id"] for f in findings if is_open(f) and f.get("first_seen_run") in refs]
    resolved = [f["id"] for f in findings if f["status"] == "resolved" and f.get("resolved_run") in run_refs]
    return {"runs": run_refs, "new": new, "resolved": resolved}


def health_trend(cards: list[dict], history: list[dict], runs: str) -> dict:
    """family -> previous health from the latest history entry of an earlier run."""
    earlier = [h for h in history if h.get("runs") != runs]
    if not earlier:
        return {}
    prev = earlier[-1]
    return {"date": prev["date"], "runs": prev["runs"],
            "families": {c["family"]: prev["families"].get(c["family"]) for c in cards}}
