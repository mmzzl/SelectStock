from config import Settings, get_settings
from scheduler.loader import parse_inputs_conf


def test_new_keys_exist_with_defaults():
    s = get_settings()
    assert s.selection_plugin_base_url == "http://localhost:8002"
    assert s.selection_plugin_mode in ("http", "mock")
    assert "stock_code" in s.trades_columns
    assert s.daily_diag_time == "18:20"
    assert s.daily_selection_time == "17:30"
    assert "/upgrade" in s.upgrade_upload_dir


def test_selector_url_switches_engine_without_moving_news_and_sector():
    """选股内核可以单独切到 8003，但 news/sector 仍走 selection_plugin_base_url（8002）。

    qlib 插件只实现选股那三条路由，两个地址一旦合成一个键，App 的消息面页和
    板块热力图就会一起 404 —— 这条测试守的就是那个合并。
    """
    # selector_base_url 必须显式钉成空：本机 .env 里它已经指到 8003，
    # 不钉的话这条测的是部署现状而不是回落逻辑，换台机器结论就翻。
    s = Settings(selector_base_url="", selection_plugin_base_url="http://localhost:8002/")
    assert s.selector_url == "http://localhost:8002"          # 没配就是老行为
    s = Settings(selector_base_url="http://localhost:8003",
                 selection_plugin_base_url="http://localhost:8002")
    assert s.selector_url == "http://localhost:8003"
    assert s.selection_plugin_base_url == "http://localhost:8002"


def test_diag_cron_in_conf_matches_setting():
    """真正决定触发时刻的是 inputs.conf 的 cron，settings 里的时间只是声明值。
    两处各写各的曾让诊断排在 18:00，和日线特征重建抢同一分钟，指标永远是上一交易日的。"""
    item = next(i for i in parse_inputs_conf(get_settings().schedule_conf)
                if i.target == "market_daily:daily_diag")
    assert f"{item.cron_kwargs['hour']:02d}:{item.cron_kwargs['minute']:02d}" \
        == get_settings().daily_diag_time