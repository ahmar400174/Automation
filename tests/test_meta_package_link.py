"""The meta_ads_verifier folder is a real package and feeds the shared cache."""
import datetime as dt
from types import SimpleNamespace

import meta_ads_verifier
import verify_active
from meta_ads_verifier.bridge import to_cache_entry


def test_import_resolves_to_the_package_not_a_root_script():
    assert meta_ads_verifier.__file__.replace("\\", "/").endswith("meta_ads_verifier/__init__.py")


def test_positive_library_count_becomes_a_live_cache_entry():
    active = SimpleNamespace(status="active", ad_count=12, page_name="Nike", http_status=200)
    entry = to_cache_entry(active)
    assert entry["live"] == 12 and entry["state"] == "live" and entry["confirmed"] is True
    assert entry["approximate"] is False and entry["source"] == "meta_ads_verifier"


def test_zero_without_an_all_ads_view_is_not_published():
    active = SimpleNamespace(status="inactive", ad_count=0, page_name="Quiet", http_status=200)
    assert to_cache_entry(active) is None
    assert to_cache_entry(active, SimpleNamespace(status="blocked", ad_count=None, page_name="", http_status=403)) is None


def test_confirmed_zero_needs_a_parsed_all_ads_view():
    active = SimpleNamespace(status="inactive", ad_count=0, page_name="Quiet", http_status=200)
    all_ads = SimpleNamespace(status="active", ad_count=4, page_name="Quiet", http_status=200)
    entry = to_cache_entry(active, all_ads)
    assert entry["live"] == 0 and entry["state"] == "inactive" and entry["confirmed"] is True
    assert entry["all_ads"] == 4
    assert verify_active.trusted_zero(entry) is True


def test_missing_page_is_not_stored_as_zero():
    active = SimpleNamespace(status="not_found", ad_count=0, page_name="", http_status=200)
    all_ads = SimpleNamespace(status="not_found", ad_count=0, page_name="", http_status=200)
    assert to_cache_entry(active, all_ads) is None


def test_package_answers_skip_the_browser(monkeypatch, tmp_path):
    seen = {}

    def fake_fill(pending, cache, cache_path):
        seen["pending"] = list(pending)
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        cache["FR:1"] = {"live": 4, "state": "live", "confirmed": True, "checked": stamp,
                         "page_id": "1", "country": "FR", "approximate": False}
        verify_active.save_cache(cache, cache_path)
        return {"FR:1"}

    def fail_browser(*args, **kwargs):
        raise AssertionError("browser verifier should not run after the package answers the page")

    monkeypatch.setenv("WH_USE_META_PACKAGE", "1")
    monkeypatch.setattr("meta_ads_verifier.bridge.fill_cache", fake_fill)
    monkeypatch.setattr(verify_active, "_run_checks", fail_browser)
    cache_path = tmp_path / "cache.json"
    cache = verify_active.verify_targets([("1", "FR", "Brand")], cache_path=cache_path, refresh=True)
    assert seen["pending"] == [("1", "FR", "Brand")]
    assert cache["FR:1"]["live"] == 4


def test_package_off_does_not_call_the_bridge(monkeypatch, tmp_path):
    monkeypatch.delenv("WH_USE_META_PACKAGE", raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("bridge called while the package link is off")

    monkeypatch.setattr("meta_ads_verifier.bridge.fill_cache", boom)
    write = tmp_path / "cache.json"
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    verify_active.save_cache({"US:9": {"live": 3, "state": "live", "checked": stamp}}, write)
    cache = verify_active.verify_targets([("9", "US", "Cached")], cache_path=write)
    assert cache["US:9"]["live"] == 3


def test_estimate_mode_does_not_enable_the_package():
    from webapp.runner import JobManager
    env = JobManager.build_env({}, "estimate", "r", {"countries": ["US"]}, "direct", "", "/tmp/m")
    assert env["WH_USE_META_PACKAGE"] == "0"
    verify = JobManager.build_env({}, "verify", "r", {"countries": ["US"]}, "direct", "", "/tmp/m")
    assert verify["WH_USE_META_PACKAGE"] == "1"
