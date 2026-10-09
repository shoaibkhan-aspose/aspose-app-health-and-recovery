"""Offline tests for family scorecards: traffic-free health, root-cause groups, cosmetic cap, grades."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scorecard import family_scorecards, grade  # noqa: E402

HOST = "app.example.com"


def f(fid, check, ftype, template="page:pdf", sample=None, share=None, status="open", urls=None, locations=None,
      clicks=0):
    ev = {"urls": urls or [f"https://{HOST}/pdf/x"], "observed": "o", "expected": "e"}
    if sample:
        ev["sample"] = sample
    if locations:
        ev["locations"] = locations
    return {"id": fid, "check": check, "type": ftype, "template": template, "subdomain": HOST, "status": status,
            "title": f"{check} title", "affected_pages": 0, "evidence": ev,
            "impact": {"share": share, "clicks_28d": clicks}}


def card(findings, families=("pdf", "omr"), stats=None, traffic=None):
    cards = family_scorecards(findings, HOST, list(families), stats or {"pdf": {"urls": 100, "langs": {"en": 1}}},
                              traffic or {})
    return {c["family"]: c for c in cards}


def test_same_breakage_same_grade_whatever_the_traffic():
    traffic = {"pdf": {"current": {"clicks": 100000}}, "omr": {"current": {"clicks": 10}}}
    cards = card([f("a", "section_no_sitemap", "broken", "sitemap:pdf"),
                  f("b", "section_no_sitemap", "broken", "sitemap:omr")], traffic=traffic)
    assert cards["pdf"]["health"] == cards["omr"]["health"] == 60
    assert cards["pdf"]["grade"] == "C" and cards["pdf"]["status"]["Sitemap"] == "bad"


def test_root_cause_groups_count_once_and_malformed_page_urls_join_the_sitemap():
    c = card([f("a", "fetch_failed", "broken", "sitemap:pdf", share=1.0),
              f("b", "section_no_sitemap", "broken", "sitemap:pdf"),
              f("c", "http_error", "broken", sample={"tool": [4, 4]}, urls=[f"https://{HOST}//pdf/x"])])["pdf"]
    assert c["health"] == 60 and [p["group"] for p in c["penalties"]] == ["sitemap"]


def test_share_cosmetic_cap_and_canonical_to_404_is_broken():
    c = card([f("a", "hreflang_no_self", "incorrect", sample={"tool": [2, 4]}),  # 20 x 0.5
              *[f(f"t{i}", f"cosmetic_{i}", "not_optimized") for i in range(10)]])["pdf"]  # 60, capped at 20
    assert c["health"] == 70 and c["cosmetic_capped"]
    bad = f("x", "canonical_elsewhere", "incorrect", sample={"tool": [4, 4]},
            locations=[{"role": "canonical target", "url": "u", "note": "HTTP 404"}])
    c = card([bad])["pdf"]
    assert c["health"] == 60 and c["top"][0]["blocker"] and c["counts"]["broken"] == 1


def test_manual_findings_map_by_url_and_unverified_do_not_count():
    seed = {**f("seed-onpage-01", None, "incorrect", template="family-hub", urls=[f"https://{HOST}/pdf/"]),
            "check": None}
    c = card([seed, f("u", "title_length", "improvable", status="unverified")])["pdf"]
    assert c["health"] == 95 and c["unverified"] == 1  # 20 x MIN_SHARE 0.25
    assert grade(90) == "A" and grade(39) == "F"


def test_cards_sorted_worst_first_and_top_issues_blockers_first():
    cards = family_scorecards([f("a", "title_length", "improvable", clicks=999), f("b", "http_error", "broken"),
                               f("c", "hreflang_no_self", "incorrect", template="page:omr")],
                              HOST, ["pdf", "omr"], {}, {})
    assert [c["family"] for c in cards] == ["pdf", "omr"]
    assert [t["id"] for t in cards[0]["top"]] == ["b", "a"]
