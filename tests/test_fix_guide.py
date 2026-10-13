"""Every check an auditor can emit has a fix-guide row; core stays tenant-free."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.audit.crawl import CHECKS  # noqa: E402
from core.discovery.run import SITEMAP_CHECKS  # noqa: E402
from core.fix_guide import GUIDE, guide_key  # noqa: E402


def test_every_check_has_a_guide_row():
    discovery_src = (ROOT / "core" / "discovery" / "run.py").read_text()
    discovery = set(SITEMAP_CHECKS) | set(re.findall(r'check="([a-z_]+)"', discovery_src))
    discovery |= {"hreflang_none", "hreflang_partial"}  # set via a variable
    missing = [c for c in CHECKS if guide_key(c, "crawl-audit") is None]
    missing += [c for c in discovery if guide_key(c, "discovery") is None]
    assert not missing, missing
    assert guide_key("redirected", "discovery") == "sitemap_redirected"
    assert guide_key("redirected", "crawl-audit") == "redirected"
    assert guide_key(None, "seed") is None


def test_rows_are_complete_and_short():
    for key, row in GUIDE.items():
        assert all(row[k].strip() for k in ("area", "name", "means", "impact", "fix", "verify")), key
        assert row["severity"] in ("broken", "incorrect", "not_optimized", "improvable"), key
        assert len(row["name"]) <= 60, key
