from config import get_settings
from scheduler.loader import parse_inputs_conf


def test_new_keys_exist_with_defaults():
    s = get_settings()
    assert s.selection_plugin_base_url == "http://localhost:8002"
    assert s.selection_plugin_mode in ("http", "mock")
    assert "stock_code" in s.trades_columns
    assert s.daily_diag_time == "18:20"
    assert s.daily_selection_time == "17:30"
    assert "/upgrade" in s.upgrade_upload_dir


def test_diag_cron_in_conf_matches_setting():
    """真正决定触发时刻的是 inputs.conf 的 cron，settings 里的时间只是声明值。
    两处各写各的曾让诊断排在 18:00，和日线特征重建抢同一分钟，指标永远是上一交易日的。"""
    item = next(i for i in parse_inputs_conf(get_settings().schedule_conf)
                if i.target == "market_daily:daily_diag")
    assert f"{item.cron_kwargs['hour']:02d}:{item.cron_kwargs['minute']:02d}" \
        == get_settings().daily_diag_time