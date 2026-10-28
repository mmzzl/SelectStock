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
from stock_selector.models.result import SelectResult
from stock_selector_qlib import artifacts, pref, serving

logger = logging.getLogger("qlib_plugin")

STRATEGY = "qlib_alpha158"

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


_STRATEGY_DETAIL = {
    "name": STRATEGY,
    "description": "qlib Alpha158 因子 + LightGBM：预测未来 N 日横截面收益，按分数排名出名单（N 随线上模型 label，见 params.label_horizon）",
    "as_of": True,
    # 与 8002 不同：qlib 读自己的 bin 面板，不吃后端送来的日线，这两项只是元信息
    "required_columns": ["date", "open", "high", "low", "close", "volume"],
    "required_rows": serving.WARMUP_DAYS,
    "params": {"universe": 300, "warmup_days": serving.WARMUP_DAYS, "label": "由线上模型 meta 提供"},
    # 本引擎不产这几项，逐条随名单一起声明（口径见 _NA_FIELDS）
    "na_fields": list(_NA_FIELDS),
}


@app.get("/api/strategy/list")
def strategy_list():
    return {"code": 0, "data": _strategies()}


@app.get("/api/strategy/detail")
def strategy_detail():
    # universe 不写死：放开全市场之后它是每天变的，报成 300 会把口径说错
    art = artifacts.load()
    h = _label_horizon(art.meta.get("label"))
    _STRATEGY_DETAIL["params"]["universe"] = {
        "source": serving.POOL_SOURCE,
        "pool_size": len(serving.score_pool(art)),
    }
    _STRATEGY_DETAIL["params"]["label"] = art.meta.get("label")
    _STRATEGY_DETAIL["params"]["label_horizon"] = h
    _STRATEGY_DETAIL["params"]["model"] = art.run_id
    return {"code": 0, "data": [_STRATEGY_DETAIL] + _pref_details()}


def _strategies() -> list[str]:
    """本插件能出票的策略名。热门偏好榜默认下线，只有 `PREF_LIST=on` 才出现。"""
    return [STRATEGY] + ([pref.STRATEGY] if serving.pref_enabled() else [])


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


def _model_rows(art, rows: list[dict]) -> list[dict]:
    label_map = _names_for(rows)
    chip, chip_missing = _chip_levels(rows)
    h = _label_horizon(art.meta.get("label"))
    signal = f"预期{h}日收益排名" if h else "预期收益排名"
    out = []
    for row in rows:
        raw_rank = int(row.get("raw_rank") or row["rank"])
        rank_text = (f"护栏后第 {row['rank']}" if raw_rank == row["rank"]
                     else f"原始第 {raw_rank}、护栏后第 {row['rank']}")
        reason = (f"Alpha158+LightGBM 原始分 {row['score_raw']:.4f}，"
                  f"{rank_text}（护栏后票池 {row['universe']} 只）；"
                  f"模型 {art.run_id}（训练止于 {art.meta.get('train_end')}，名单日为样本外）")
        blocked_top = row.get("guard_blocked_top")
        if blocked_top:
            reason += (f"；原始第 1 的 {blocked_top['code']} 被"
                       f"{blocked_top['reason']}挡下")
        out.append(SelectResult(
            code=row["code"],
            name=label_map.get(row["code"], ""),
            signal=signal,
            reason=reason,
            strategy=STRATEGY,
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
                **_level_fields(chip.get(row["qlib_symbol"]), chip_missing),
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
