from config import get_settings


def test_new_keys_exist_with_defaults():
    s = get_settings()
    assert s.selection_plugin_base_url == "http://localhost:8002"
    assert s.selection_plugin_mode in ("http", "mock")
    assert "stock_code" in s.trades_columns
    assert s.daily_diag_time == "18:00"
    assert s.daily_selection_time == "17:30"
    assert "/upgrade" in s.upgrade_upload_dir