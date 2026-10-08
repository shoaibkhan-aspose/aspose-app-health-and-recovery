"""Offline tests: tenant config, read-only guard, and the access-check logic with fake clients."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.google_auth import CredentialsError, assert_readonly, get_credentials  # noqa: E402
from core.gsc import default_window  # noqa: E402
from core.tenant import load_tenant  # noqa: E402
from scripts.check_access import check_ga4, check_gsc  # noqa: E402


def test_tenant_loads():
    t = load_tenant("aspose.app")
    assert t.property == "aspose.app"
    assert t.ga4_property_id == "253117495"
    assert len(t.raw["product_families"]) == 29
    assert len(t.raw["gsc_sitemaps_snapshot"]["sitemaps"]) == 38
    assert "https://products.aspose.app/" in t.gsc_properties


def test_qa_hosts_excluded():
    t = load_tenant("aspose.app")
    assert t.is_excluded("products-qa.aspose.app")
    assert not t.is_excluded("products.aspose.app")


def test_core_names_no_tenant():
    for f in (Path(__file__).resolve().parent.parent / "core").rglob("*.py"):
        assert "aspose" not in f.read_text().lower(), f"{f.name} names a tenant"


def test_readonly_guard():
    assert_readonly(["https://www.googleapis.com/auth/webmasters.readonly"])
    with pytest.raises(CredentialsError):
        assert_readonly(["https://www.googleapis.com/auth/webmasters"])


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(CredentialsError):
        get_credentials()


def test_gsc_window_lag():
    from datetime import date

    assert default_window(28, today=date(2026, 10, 8)) == ("2026-09-08", "2026-10-05")


class FakeGSC:
    def list_sites(self):
        return [{"siteUrl": "https://products.aspose.app/", "permissionLevel": "siteRestrictedUser"}]

    def search_analytics(self, site, start, end, dimensions=None, row_limit=1000, start_row=0):
        if dimensions:
            return [{"keys": ["https://products.aspose.app/pdf/"], "clicks": 5, "impressions": 50}]
        return [{"clicks": 100, "impressions": 1000}]

    def list_sitemaps(self, site):
        return [{"path": "x", "errors": "1"}, {"path": "y", "errors": "0", "isPending": True}]


class FakeGA4:
    def run_report(self, property_id, dimensions, metrics, **kw):
        return [
            {"hostName": "products.aspose.app", "sessionDefaultChannelGroup": "Organic Search", "sessions": 80.0},
            {"hostName": "products.aspose.app", "sessionDefaultChannelGroup": "Direct", "sessions": 20.0},
            {"hostName": "products-qa.aspose.app", "sessionDefaultChannelGroup": "Direct", "sessions": 1.0},
        ]


def test_check_gsc_marks_missing_properties():
    t = load_tenant("aspose.app")
    r = check_gsc(FakeGSC(), t, "2026-09-08", "2026-10-05")
    prod = r["properties"]["https://products.aspose.app/"]
    assert prod["clicks"] == 100 and prod["sitemaps"]["count"] == 2
    assert len(prod["sitemaps"]["with_errors"]) == 1 and prod["sitemaps"]["pending"] == ["y"]
    assert "error" in r["properties"]["https://blog.aspose.app/"]
    assert r["ok"] is False


def test_check_ga4_groups_by_host():
    t = load_tenant("aspose.app")
    r = check_ga4(FakeGA4(), t)
    assert r["hosts"]["products.aspose.app"] == {"sessions": 100.0, "organic_sessions": 80.0}
    assert "products-qa.aspose.app" in r["excluded_hosts_seen"]
    assert "www.aspose.app" in r["tenant_hosts_not_seen"]
