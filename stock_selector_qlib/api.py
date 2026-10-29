"""入口：qlib 选股插件（与 stock_selector 8002 同契约的 HTTP 服务）。

启动：
    python -m uvicorn stock_selector_qlib.api:app --host 172.17.0.1 --port 8003
    # 绑 docker0 网关：nginx 容器经 host.docker.internal(=172.17.0.1) 回连宿主机，绑 127.0.0.1 连不通。
    # 对外查看走 deploy/nginx/selectstock.conf 的 location /qlib/（Basic 认证 + 限流）。

后端切引擎改 `SELECTOR_BASE_URL=http://172.17.0.1:8003` 一个键（`settings.selector_base_url`），
业务代码零改动。注意不要整键切 `selection_plugin_base_url`：新闻与板块那两条服务也复用它，
而本进程只实现选股这三条路由，切过去会让 App 的消息面页和板块热力图一起 404。

红线：本模块零选股逻辑，只做契约适配；打分全在 serving.py，模型全在 artifacts.py。
"""
from __future__ import annotations

import logging
import re

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from stock_selector.features import chips
from stock_selector.features.base_columns import is_limit_up
from stock_selector.models.result import SelectResult
from stock_selector_qlib import artifacts, pref, serving

logger = logging.getLogger("qlib_plugin")

STRATEGY = "qlib_alpha158"
STRATEGY_BOARD = "qlib_boards"          # 板后低吸榜：今日板票 × 模型分板内名次（2026-10-29）
STRATEGY_DIP = "qlib_dip"               # 低开买入榜：今日上涨票 × 事件臂（2026-10-29 用户点名口径）

app = FastAPI(title="qlib 插件化量化选股系统", version="0.1.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RunRequest(BaseModel):
    strategies: list[str] | None = Field(
        default=None, description="策略名列表；缺省或含 qlib_alpha158 时出名单，未知策略返回 400")
    topk: int | None = Field(default=None, ge=1, le=200, description="取前 N 只，缺省读 QLIB_TOPK")


# qlib 只出横截面排序分：风险剔除层、判据后验都在自研内核里，这里没有对应计算。
# 必须显式声明而不是留 null：null 到 App 上是「—」或整行消失，使用者会把空白读成「没有风险」。
# 2026-10-28：买点参考位/失效位从这里移出去了——现由近 60 日筹码主峰算（features/chips.py），
# 单只票算不出时留空并写明原因，那是「判不出」而不是「不产」，两者在 na_fields 里必须分开。
_NA_FIELDS = ("risk_ex10_pp", "posterior_verdict")


def _level_fields(levels: dict | None, missing_reason: str | None) -> dict:
    """把筹码价位块摊平成 extra 的扁平键（后端 _http_run 只认扁平键）。

    三档全是**规则价**：买入=近 60 日筹码主峰下沿，第一止盈=主峰上沿，止损=买入价−2×ATR14。
    口径句子必须随行返回——页面上光有三个数字，使用者会把它读成模型预测的价格。
    """
    if missing_reason:
        return {"buy_ref_price": None, "sell_target_price": None, "buy_invalid_price": None,
                "level_note": missing_reason}
    if not levels:
        return {"buy_ref_price": None, "sell_target_price": None, "buy_invalid_price": None,
                "level_note": f"近 {serving.CHIP_WINDOW} 交易日历史不足（新上市或面板缺数），"
                              f"算不出筹码主峰"}
    note = chips.levels_note(levels)
    return {"buy_ref_price": levels["buy_ref_price"],
            "sell_target_price": levels["sell_target_price"],
            "buy_invalid_price": levels["buy_invalid_price"],
            "level_note": note}


def _chip_levels(rows: list[dict]) -> tuple[dict, str | None]:
    """按名单一次取回价位；整块失败时返回 (空表, 原因)，名单照常出。"""
    symbols = [r["qlib_symbol"] for r in rows if r.get("qlib_symbol")]
    # 模型榜的行用 date，偏好榜的行用 asof——同一个信号日，只是两条榜的行形状不同
    day = next((r.get("date") or r.get("asof") for r in rows if (r.get("date") or r.get("asof"))), "")
    if not symbols or not day:
        return {}, None
    try:
        return serving.chip_levels(symbols, day), None
    except Exception as exc:  # noqa: BLE001 - 展示层的缺数不能打断出票
        logger.exception("筹码价位取数失败")
        return {}, f"筹码价位没算出来（{type(exc).__name__}：{exc}）"


def _label_horizon(label: str | None) -> int | None:
    m = re.match(r"Ref\(\$close,\s*-(\d+)\)", str(label or ""))
    return int(m.group(1)) if m else None


_STRATEGY_BASE = {
    "name": STRATEGY,
    "as_of": True,
    # 与 8002 不同：qlib 读自己的 bin 面板，不吃后端送来的日线，这两项只是元信息
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "na_fields": list(_NA_FIELDS),
}

_CROSS_SECTION_DESC = ("qlib Alpha158 因子 + LightGBM：预测未来 N 日横截面收益，按分数排名出名单"
                       "（N 随线上模型 label，见 params.label_horizon）")


def arm_wording(art) -> dict:
    """这条臂自己的说法：人口闸门、买法、卖法、榜名。

    三处文案（主榜详情、事件榜详情、行内价位块）必须**同源**——分叉过一次就是 §45.7 那个
    「换臂没人记得改」的 bug：batchG_evt 训在涨 ≥+0.1%、batchH_r1 训在涨 ≥+5%，卖法一条是
    「当天收盘无条件出」、一条是「拿着等涨、跌 5% 止损、上限 10 个交易日」。
    """
    meta = getattr(art, "meta", None) or {}
    rule = meta.get("rule_spec")
    spec = rule or meta.get("event_spec") or {}
    min_chg = float(spec.get("min_chg", serving.DIP_MIN_CHG))
    kind = "rule" if rule else ("event" if meta.get("event_spec") else "")
    out = {
        "kind": kind,
        "label": {"rule": "低吸持有", "event": "低开买入"}.get(kind, ""),
        "min_chg": min_chg,
        "population": f"当日收盘涨幅 ≥+{min_chg * 100:.1f}%（与本臂训练人群同一条）",
        "entry_rule": "明开 ≤ 今收 才买（低开即触发），按开盘价成交、开盘高于今收不追",
        "invalid_rule": "不设失效线：开盘跌到任何位置都按规则价挂单（涨跌停不挡单）",
    }
    if rule:
        stop = float(spec.get("stop_pct", -0.05)) * 100
        hold = int(spec.get("max_hold", 10))
        out["exit_rule"] = (f"哪天收盘价高过买入价就哪天卖；一直不涨则跌 {abs(stop):.0f}% 止损；"
                            f"最多持 {hold} 个交易日")
    else:
        out["exit_rule"] = "涨了就卖、收盘无条件出"
    return out


def main_detail(art) -> dict:
    """主榜的说明跟着**线上模型自己是不是臂**。

    §46 之前线上是 batchW_c3（全市场横截面），主榜说的是「谁最强」；换成低吸臂之后
    `day_ranking` 在打分前就按闸门筛窄了人群，主榜回答的题变成了「今天这批上涨票里买哪只」，
    继续挂那句「预测未来 N 日横截面收益」就是假话。
    """
    base = {**_STRATEGY_BASE,
            "params": {"universe": {"source": serving.POOL_SOURCE,
                                    "pool_size": len(serving.score_pool(art))},
                       "warmup_days": serving.WARMUP_DAYS,
                       "label": art.meta.get("label"),
                       "label_horizon": _label_horizon(art.meta.get("label")),
                       "model": art.run_id}}
    w = arm_wording(art)
    if not w["kind"]:
        return {**base, "description": _CROSS_SECTION_DESC}
    return {**base, "label": f"{w['label']}臂",
            "description": (f"主榜＝{w['label']}臂（模型 {art.run_id} 只在「今天涨 → 明天低开买这一笔"
                            "赚不赚钱」的训练行上拟合）：行=" + w["population"] + "且过两条护栏的票，"
                            "名次=模型分在这批上涨票里的位次。回答的是「明天早上低开时买哪只、这笔该怎么拿」，"
                            "不是全市场谁最强——所以跌票一律不上这条榜。行内价位块=低吸规则价："
                            f"{w['entry_rule']}；不设失效线；{w['exit_rule']}。"),
            "params": {**base["params"], "population": w["population"],
                       "entry_rule": w["entry_rule"], "exit_rule": w["exit_rule"],
                       "invalid_rule": w["invalid_rule"]}}

_BOARD_DETAIL = {
    "name": STRATEGY_BOARD,
    "label": "板后低吸",
    "description": ("今日涨停票的模型分切片：行=当日涨停且过两条护栏的票，名次=模型分在板内的名次"
                    "（同一份线上模型自己的分数，换个人群切，不是人工塞票）。行内价位块=板后低吸规则价："
                    "明早开盘≤今收×0.95（低开5%起）挂开盘价买、"
                    "买入后涨了就卖、收盘无条件出；不设失效线（跌停一字开盘也照样挂单买）。"),
    "as_of": True,
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "params": {"population": "当日涨停（base_columns.is_limit_up，主板10%口径）",
               "invalid_rule": "不设失效线：开盘跌到任何位置都按规则价挂单（涨跌停不挡单）",
               "label": "由线上模型 meta 提供"},
    "na_fields": list(_NA_FIELDS),
}


_DIP_BASE = {
    "name": STRATEGY_DIP,
    "label": "低开买入",
    "as_of": True,
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "na_fields": list(_NA_FIELDS),
}


def dip_detail(art) -> dict:
    """低开买入榜的口径说明**跟着模型 meta 走**（判据都在 `arm_wording`，与主榜同源）。

    两条臂共用一个入口、卖法却不一样：
    - §44 事件臂（`event_spec`）：明开买、**当天收盘无条件出**；
    - §45 低吸持有臂（`rule_spec`）：明开买开盘价、**哪天收盘高过买入价哪天卖**，
      跌 `stop_pct` 止损、最多持 `max_hold` 个交易日（用户 2026-10-29 点名的原话）。
    人口闸门同理：`min_chg` 是各自训练时用的那条（+0.1% / +5%），写死一条就会在换臂时骗人。
    """
    w = arm_wording(art)
    description = (f"{w['label']}臂榜：行=今日上涨（收盘相对昨收 ≥+{w['min_chg'] * 100:.1f}%）"
                   "且过两条护栏的票，名次=模型（只在「今天涨 → 明天低开买这一笔赚不赚钱」的训练行上拟合）的分。"
                   "回答的是「明天早上低开时买哪只、这笔该怎么拿」，不是全市场谁最强——"
                   "所以它只在上涨票里排，跌票一律不上这条榜。行内价位块=低吸规则价："
                   f"明早开盘≤今收挂开盘价买、开盘高于今收不追；不设失效线；{w['exit_rule']}。")
    return {**_DIP_BASE, "label": w["label"], "description": description,
            "params": {"population": w["population"],
                       "entry_rule": w["entry_rule"],
                       "exit_rule": w["exit_rule"],
                       "invalid_rule": w["invalid_rule"],
                       "model": getattr(art, "run_id", ""),
                       "label": (getattr(art, "meta", None) or {}).get("label", "")}}


@app.get("/api/strategy/list")
def strategy_list():
    return {"code": 0, "data": _strategies()}



@app.get("/api/strategy/detail")
def strategy_detail():
    # 每次都按**当前线上模型**现搭（旧代码改的是模块级字典：换臂后那份写死的说明
    # 会一直挂在详情里，而且多个请求共享同一份可变小抄）
    data = [main_detail(artifacts.load()), _BOARD_DETAIL]
    dip_art, _ = serving.dip_artifact()
    if dip_art is not None:
        data.append(dip_detail(dip_art))
    return {"code": 0, "data": data + _pref_details()}


def _strategies() -> list[str]:
    """本插件能出票的策略名。热门偏好榜默认下线，只有 `PREF_LIST=on` 才出现；
    低开买入榜只在事件模型（`QLIB_DIP_MODEL_DIR`）在场时出现——没有事件模型就不出这条榜，
    绝不用全市场那臂冒充它。"""
    out = [STRATEGY, STRATEGY_BOARD]
    if serving.dip_artifact()[0] is not None:
        out.append(STRATEGY_DIP)
    return out + ([pref.STRATEGY] if serving.pref_enabled() else [])


def _pref_details() -> list[dict]:
    if not serving.pref_enabled():
        return []
    return [{
        "name": pref.STRATEGY,
        "label": pref.LABEL,
        "description": ("热门偏好榜：在模型当日榜内，按「热门板块 + 新闻利好 + 排除小市值 + "
                        "趋势向上」四条重排（热度 45% / 趋势 35% / 新闻 20%，三道硬闸：板块、市值、趋势）。"
                        "不重新打分，所以两条护栏已经挡在前面"),
        "as_of": True,
        "required_columns": ["date", "open", "high", "low", "close", "volume"],
        "required_rows": serving.WARMUP_DAYS,
        "params": {
            "min_float_mv_yi": pref.MIN_FLOAT_MV,
            "top_boards": pref.TOP_BOARDS,
            "min_board_stocks": pref.MIN_BOARD_STOCKS,
            "weights": pref.WEIGHTS,
            "topk": pref.DEFAULT_TOPK,
            # 数据时点如实写：17:30 选股时今天的板块热度（18:30）和新闻（22:00）都还没落，
            # 用的必然是前一天的——这一栏不写，页面就会把昨天的热度说成今天的。
            "heat_source": "data/sector_heat/board.parquet（每日 18:30 覆盖，无历史）",
            "news_source": "data/news_analysis/（每日 22:00，服务次日）",
            "mv_source": "data/market_snapshot/（每日 15:30，2026-10-12 起）",
            "backtestable": False,
        },
        "na_fields": list(_NA_FIELDS),
    }]


@app.post("/api/selector/run")
def selector_run(req: RunRequest):
    """出最新一个交易日的名单。分数只由落盘模型 + 短窗因子决定，不在此处训练。

    `qlib_alpha158` 是默认榜；`hot_pref` 默认下线，只有 `PREF_LIST=on` 时才参与。
    两只票可以在两条榜里各出现一次（`strategy` 不同），页面按策略分开展示。
    """
    names = set(req.strategies or [])
    able = _strategies()
    want = [s for s in able if not names or s in names]
    if not want:
        # 未知策略不能回 200 空名单：后端会把它当成「今天没有票」落库。
        # 明确 400，调用方才知道是名字下线/拼错，而不是市场没机会。
        raise HTTPException(status_code=400, detail=f"策略不可用：{', '.join(sorted(names))}")
    try:
        art = artifacts.load()
    except Exception as exc:  # noqa: BLE001 - 模型加载失败必须是 HTTP 错误，不能被当成「今天没票」
        logger.exception("qlib 模型加载失败")
        raise HTTPException(status_code=502, detail=f"qlib 打分失败：{exc}") from exc

    out: list[dict] = []
    if STRATEGY in want:
        try:
            rows = serving.latest_scores(topk=req.topk or serving.DEFAULT_TOPK)
        except Exception as exc:  # noqa: BLE001
            logger.exception("qlib 打分失败")
            raise HTTPException(status_code=502, detail=f"qlib 打分失败：{exc}") from exc
        out += _model_rows(art, rows)
    if STRATEGY_BOARD in want:
        try:
            brows = serving.board_scores(topk=req.topk or serving.DEFAULT_TOPK)
        except Exception as exc:  # noqa: BLE001 - 板切片失败不能拖累主榜照常出票
            logger.exception("板后低吸榜失败")
            raise HTTPException(status_code=502, detail=f"板后低吸榜打分失败：{exc}") from exc
        out += _model_rows(art, brows, board_slice=True)
    if STRATEGY_DIP in want:
        dip_art, dip_source = serving.dip_artifact()
        if dip_art is not None:
            try:
                drows = serving.dip_scores(topk=req.topk or serving.DEFAULT_TOPK)
            except Exception as exc:  # noqa: BLE001 - 事件榜失败不能拖垮主榜
                logger.exception("低开买入榜失败")
                raise HTTPException(status_code=502,
                                    detail=f"低开买入榜打分失败：{exc}") from exc
            out += _model_rows(dip_art, drows, dip_slice=True)
    if pref.STRATEGY in want:
        try:
            prows = serving.latest_pref(topk=req.topk)
        except Exception as exc:  # noqa: BLE001 - 偏好榜失败同样不能变成「今天没票」
            logger.exception("热门偏好榜失败")
            raise HTTPException(status_code=502, detail=f"热门偏好榜失败：{exc}") from exc
        out += _pref_rows(prows)
    return {"code": 0, "data": out}


def _names_for(rows: list[dict]) -> dict[str, str]:
    try:
        return serving.stock_names([r["code"] for r in rows])
    except Exception:  # noqa: BLE001 - 名字是展示字段，取不到不能打断出票
        return {}


def _dip_levels(row: dict, pct: float | None) -> dict | None:
    """板票行把筹码价位块换成「板后低吸」规则价（2026-10-28 用户拍板的交易语义）。

    买线=今收×0.95：明早开盘 ≤ 买线（低开 5% 起）→ 挂开盘价买，开盘高于它不追；
    卖出无目标价：涨了就卖，收盘无条件出。**没有失效线**——2026-10-29 用户拍板
    「跌停买不进这条不适应」，开盘 ≤ 今收×0.905（跌停一字）照旧挂单，回测也按能成交算。
    三个键复用 App 已消费的扁平键，板票行用本块**替换**筹码块（板票的交易语义是低吸，
    不是筹码主峰回踩）。
    """
    price = row.get("price")
    if not price or pct is None or not is_limit_up(row["code"], pct * 100):
        return None
    buy = round(price * 0.95, 2)
    return {
        "buy_ref_price": buy,
        "sell_target_price": None,
        "buy_invalid_price": None,
        "level_note": (f"今日涨停（{pct * 100:.1f}%）→ 板后低吸规则价（非模型预测）："
                       f"明早开盘≤{buy}（低开5%起）挂开盘价买、开盘高于它不追；"
                       f"不设失效线（跌停一字也照样挂单买）；买入后涨了就卖，收盘无条件出。"
                       f"历史回放：同带不设下界 毛+1.53%/笔（300 票池 2024→2026-10 共 24 笔，样本薄）"),
    }


def _dip_entry_levels(row: dict, art=None) -> dict:
    """低开买入榜的价位块：买线就是**今收**（明早开盘 ≤ 今收即触发），不是 ×0.95。

    与板后低吸那条（`_dip_levels`，买线=今收×0.95）差别的来源是口径：这一臂的低点判据是
    `gap < 0`（低开即事件），买线跟着放在今收上，两个数说的是同一件事。
    同样**不设失效线**（2026-10-29 用户拍板取消涨跌停挡单）。
    卖出侧跟着臂走（与 `dip_detail` 同一套判据）：事件臂当天收盘无条件出，
    低吸持有臂「哪天收盘高过买入价哪天卖、跌 5% 止损、上限 10 个交易日」。
    """
    price, pct = row.get("price"), row.get("chg")
    if not price or pct is None:
        return {"buy_ref_price": None, "sell_target_price": None, "buy_invalid_price": None,
                "level_note": "收盘价或当日涨幅缺数，价位判不出（不是「没有价」）"}
    buy = round(float(price), 2)
    if serving.arm_kind(art) == "rule":
        spec = serving.arm_spec(art)
        stop = float(spec.get("stop_pct", -0.05)) * 100
        hold = int(spec.get("max_hold", 10))
        exit_rule = (f"买入后哪天收盘价高过 {buy} 就哪天卖；一直不涨则跌 {abs(stop):.0f}% 止损；"
                     f"最多持 {hold} 个交易日")
        history = ("历史回放（本臂样本外 2024-01→2026-10-16，13,566 笔事件/665 日）："
                   "规则本身扣费 +0.053%/笔、胜率 64.9%；模型同日头 5 扣费 +0.335%/笔、"
                   "配对 +0.176pp、t +3.04")
    else:
        exit_rule = "买入后涨了就卖，收盘无条件出"
        history = ("历史回放（事件臂样本外 2024-01→2026-10-16，14.3 万笔事件/671 日）："
                   "同日头 5 毛 +0.852%、扣费 +0.652%/笔，同日配对 +0.657pp、t +7.68")
    return {
        "buy_ref_price": buy,
        "sell_target_price": None,
        "buy_invalid_price": None,
        "level_note": (f"今日涨 {pct * 100:+.1f}% → 低开买入规则价（非模型预测）："
                       f"明早开盘≤{buy}（低开即触发）挂开盘价买、开盘高于今收不追；"
                       f"不设失效线（跌停一字也照样挂单买）；{exit_rule}。{history}"),
    }


def arm_signal(art) -> str:
    """臂榜的 signal：两条臂卖法不同，话也不能说成同一句。"""
    if serving.arm_kind(art) == "rule":
        return "低吸持有：明天低开买入、哪天收盘高过买价哪天卖的期望收益排名"
    return "明天低开买入的期望收益排名"


def _model_rows(art, rows: list[dict], board_slice: bool = False,
                dip_slice: bool = False) -> list[dict]:
    label_map = _names_for(rows)
    chip, chip_missing = _chip_levels(rows)
    try:
        pcts = serving.pct_of_day([r["qlib_symbol"] for r in rows], rows[0]["date"])
    except Exception:  # noqa: BLE001 - 涨幅判不出就按非板走筹码块，不能打断出票
        logger.warning("pct_of_day 失败，板票行退回筹码价位块", exc_info=True)
        pcts = {}
    h = _label_horizon(art.meta.get("label"))
    # 线上模型一旦是臂（§46 主榜也换），day_ranking 已经在打分前按人群闸筛过窄，
    # 主榜说的主语就从「全市场谁最强」换成「今天这批上涨票里买哪只」——文案必须跟着换。
    gate = bool(serving.arm_spec(art))
    arm_main = gate and not board_slice and not dip_slice
    signal = f"预期{h}日收益排名" if h else "预期收益排名"
    if board_slice:
        signal = f"板后低吸·{signal}"
    if dip_slice or arm_main:
        signal = arm_signal(art)
    out = []
    for row in rows:
        raw_rank = int(row.get("raw_rank") or row["rank"])
        if board_slice:
            # 「全榜」只在主榜没有人群闸门时才成立；闸一开，榜本身就是上涨票榜
            board_note = "上涨票榜" if gate else "全榜"
            rank_text = f"板内第 {row['rank']} / {row['universe']} 只当日板票（{board_note}第 {raw_rank}）"
        elif dip_slice:
            rank_text = f"低开榜第 {row['rank']} / {row['universe']} 只今日上涨票"
        elif arm_main:
            rank_text = f"今日上涨票第 {row['rank']} / {row['universe']} 只"
        else:
            rank_text = (f"护栏后第 {row['rank']}" if raw_rank == row["rank"]
                         else f"原始第 {raw_rank}、护栏后第 {row['rank']}")
        pool_note = "" if board_slice or dip_slice or arm_main else f"（护栏后票池 {row['universe']} 只）"
        reason = (f"Alpha158+LightGBM 原始分 {row['score_raw']:.4f}，"
                  f"{rank_text}{pool_note}；"
                  f"模型 {art.run_id}（训练止于 {art.meta.get('train_end')}，名单日为样本外）")
        blocked_top = row.get("guard_blocked_top")
        if blocked_top:
            reason += (f"；原始第 1 的 {blocked_top['code']} 被"
                       f"{blocked_top['reason']}挡下")
        dip = _dip_levels(row, pcts.get(row["qlib_symbol"]))
        if dip:
            reason += f"；价位块为板后低吸规则价（今日涨停 {pcts.get(row['qlib_symbol']) * 100:.1f}%）"
        if dip_slice or arm_main:
            levels = _dip_entry_levels(row, art)
            reason += "；价位块为低开买入规则价（明开≤今收才买）"
        else:
            levels = dip or _level_fields(chip.get(row["qlib_symbol"]), chip_missing)
        out.append(SelectResult(
            code=row["code"],
            name=label_map.get(row["code"], ""),
            signal=signal,
            reason=reason,
            strategy=STRATEGY_DIP if dip_slice else (STRATEGY_BOARD if board_slice else STRATEGY),
            asof=row["date"],
            price=row["price"],
            score=round(row["score"], 6),
            rank=row["rank"],
            extra={
                "qlib_symbol": row["qlib_symbol"],
                "score_raw": round(row["score_raw"], 6),
                "raw_rank": raw_rank,
                "guard_blocked_top": blocked_top,
                "model_id": art.run_id,
                "universe": row["universe"],
                "feature_count": art.meta.get("feature_count"),
                "na_fields": list(_NA_FIELDS),
                **levels,
            },
        ).to_dict())
    return out


def _pref_rows(rows: list[dict]) -> list[dict]:
    """热门偏好榜的行：分数是四路合成（0~1，和模型榜同口径），理由把四路各说一遍。"""
    label_map = _names_for(rows)
    chip, chip_missing = _chip_levels(rows)
    out = []
    for row in rows:
        ex = row.get("extra") or {}
        out.append(SelectResult(
            code=row["code"],
            name=row.get("name") or label_map.get(row["code"], ""),
            signal="热门板块+利好+大市值+趋势向上",
            reason=row["reason"],
            strategy=pref.STRATEGY,
            asof=row["asof"],
            price=row["price"],
            score=round(row["score"], 6),
            rank=row["rank"],
            extra={**ex, "qlib_symbol": row["qlib_symbol"], "na_fields": list(_NA_FIELDS),
                   **_level_fields(chip.get(row["qlib_symbol"]), chip_missing)},
        ).to_dict())
    return out


@app.get("/api/selector/rank")
def selector_rank(codes: str = Query(default="", description="逗号分隔的 6 位代码，最多 60 只")):
    """持仓页要的「当日榜内名次」：名次来自当日全票池打分，池外的票不给名次。

    这条路由不在 8002 的三条契约里，是 qlib 独有的读接口（自研内核没有"榜"这个概念）。
    池外持仓返回 `in_pool=false` + 一句原因，绝不返回一个补出来的名次。

    **这条是同步接口，所以它绝不等打分**：放开全市场之后第一次冷打分要几百秒，
    把它放进请求里只会被 App/后端的超时打断，打断后页面拿到的是「查询失败」或空名单，
    而真实情况是「还在算」。未就绪就立刻返回 `ready=false` 并起一个后台预热线程。
    """
    picked = [c.strip() for c in codes.split(",") if c.strip()][:60]
    if not picked:
        return {"code": 0, "data": {"ready": True, "date": "", "pool_size": 0,
                                    "universe": 0, "items": []}}
    try:
        return {"code": 0, "data": serving.ranks_by_code(picked, wait=False)}
    except serving.RankingNotReady as exc:
        started = serving.warm_ranking()
        logger.info("榜单未就绪，后台预热%s：%s", "已起" if started else "已在跑", exc)
        return {"code": 0, "data": {"ready": False, "warming": started, "date": "",
                                    "pool_size": 0, "universe": 0, "items": [],
                                    "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001 - 查不到名次必须报错，不能返回「都没上榜」
        logger.exception("qlib 名次查询失败")
        raise HTTPException(status_code=502, detail=f"qlib 名次查询失败：{exc}") from exc


@app.post("/api/admin/refresh")
def admin_refresh():
    """收盘后 bin 面板写完时由 scripts/qlib_update_bin.py 调用：清掉进程内的 qlib 日历/因子
    缓存并认账新的交易日。为什么要这么一个口：根调度器以普通用户跑，重启不了本进程
    （supervisorctl 要 root），而 qlib 把 D.calendar() 的结果在进程内缓存一小时，
    不刷新的话这一小时里它会坚持说「今天还不是交易日」。
    对外查看走 nginx 的 /qlib/，那里已把 /qlib/api/admin/ 挡成 403。"""
    try:
        return {"code": 0, "data": serving.refresh()}
    except Exception as exc:  # noqa: BLE001 - 刷新失败必须让更新脚本非零退出，不能默默回去
        logger.exception("qlib 缓存刷新失败")
        raise HTTPException(status_code=500, detail=f"qlib 缓存刷新失败：{exc}") from exc


@app.get("/api/health")
def health():
    status = {"status": "ok", "engine": STRATEGY, "strategies": _strategies()}
    try:
        status["model"] = artifacts.load().describe()
        status["scoring"] = serving.last_score_stats()
        # 偏好榜的筛数与数据时点：哪一路挡了多少、热度/新闻取的是哪天，都在这里看得见。
        # 空表 = 今天还没出过这条榜（不是「挡了 0 只」）。
        status["pref"] = serving.last_pref_stats()
    except Exception as exc:  # noqa: BLE001
        status["status"] = "no_model"
        status["message"] = str(exc)
    return status
