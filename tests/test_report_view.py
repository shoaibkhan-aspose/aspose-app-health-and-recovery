"""Offline tests for the report views: confidence, root-cause merging, systemic issues, risks, summary, changes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.report_view import (  # noqa: E402
    confidence,
    grade_summary,
    health_trend,
    merge_root_causes,
    run_changes,
    systemic_issues,
    top_risks,
)

HOST = "app.example.com"
FAMS = {"pdf", "omr", "cells", "words"}


def f(fid, check, ftype="incorrect", fam="pdf", score=10, sample=None, status="open", source="crawl-audit",
      first=None, resolved=None):
    ev = {"urls": [f"https://{HOST}/{fam}/x"], "observed": "o", "expected": "e"}
    if sample:
        ev["sample"] = sample
    return {"id": fid, "check": check, "type": ftype, "subdomain": HOST, "template": f"page:{fam}",
            "status": status, "source": source, "title": f"{check} ({fam})", "evidence": ev,
            "impact": {"score": score, "clicks_28d": score * 100}, "first_seen_run": first, "resolved_run": resolved}


def test_confidence_levels():
    assert confidence(f("a", "lastmod_bulk", source="discovery")) == "complete"
    assert confidence(f("b", "title_length", sample={"tool": [1, 4]})) == "to confirm"
    assert confidence(f("c", "title_length", sample={"tool": [3, 4]})) == "strong"
    assert confidence(f("d", "title_length", sample={"tool": [2, 8]})) == "partial"
    assert confidence({**f("e", None), "check": None, "status": "unverified"}) == "to confirm"


def test_root_causes_merge_into_the_worst_finding():
    a = {**f("a", "fetch_failed", "broken", fam="omr", score=80), "template": "sitemap:omr", "source": "discovery"}
    b = {**f("b", "section_no_sitemap", "broken", fam="omr", score=89), "template": "sitemap:omr",
         "source": "discovery"}
    c = f("c", "hreflang_no_self", fam="omr")
    merge_root_causes([a, b, c], HOST, FAMS)
    assert b["_merged"] == ["a"] and a["_merged_into"] == "b" and "_merged_into" not in c


def test_systemic_issues_and_top_risks():
    rows = [f(f"h{i}", "hreflang_no_self", fam=fam, score=50 + i) for i, fam in enumerate(["pdf", "omr", "cells"])]
    rows.append(f("t1", "title_length", "improvable", fam="words", score=5))
    sys_rows = systemic_issues(rows, HOST, FAMS)
    assert [(r["check"], r["families"]) for r in sys_rows] == [("hreflang_no_self", ["cells", "omr", "pdf"])]
    risks = top_risks(rows, HOST, FAMS, limit=3)
    assert [r["id"] for r in risks] == ["h2", "t1"] and risks[0]["also"] == ["omr", "pdf"]


def test_grade_summary_and_trend():
    cards = [{"family": "pdf", "grade": "B", "health": 80, "traffic": {"current": {"clicks": 700}}},
             {"family": "cells", "grade": "D", "health": 45, "traffic": {"current": {"clicks": 250}}},
             {"family": "omr", "grade": "F", "health": 20, "traffic": {"current": {"clicks": 50}}}]
    s = grade_summary(cards)
    assert s["distribution"] == {"A": 0, "B": 1, "C": 0, "D": 1, "F": 1}
    assert s["click_share"]["B"] == 70 and s["weak_with_traffic"] == [{"family": "cells", "grade": "D", "share": 25},
                                                                      {"family": "omr", "grade": "F", "share": 5}]
    history = [{"date": "2026-10-01", "runs": "r1", "families": {"pdf": 70}}]
    assert health_trend(cards, history, "r2")["families"]["pdf"] == 70
    assert health_trend(cards, history, "r1") == {}


def test_run_changes_skip_a_sources_first_run():
    rows = [f("a", "x", first="crawl-audit-run2"), f("b", "x", first="crawl-audit-run9"),
            f("c", "x", status="resolved", first="crawl-audit-run2", resolved="crawl-audit-run9"),
            f("d", "x", first="discovery-run7")]
    ch = run_changes(rows, ["discovery-run7", "crawl-audit-run9"])
    assert ch["new"] == ["b"] and ch["resolved"] == ["c"]  # discovery-run7 is that source's first run
