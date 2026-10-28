# db/sql/selection.py
from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.sql.base import Base, IdMixin, TimestampMixin, new_uuid


class SelectJob(Base, TimestampMixin):
    __tablename__ = "select_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategies: Mapped[str] = mapped_column(String(256), default="")   # JSON 数组字符串
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued/running/done/failed
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    error: Mapped[str] = mapped_column(String(512), default="")
    result_count: Mapped[int] = mapped_column(Integer, default=0)


class SelectResult(Base, IdMixin, TimestampMixin):
    __tablename__ = "select_results"

    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    signal: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(1024), default="")
    # 选股插件 P1 富化字段。rank 是 SQL 保留字（窗口函数），列名用 rank_no，
    # API 原样返回 rank_no，由客户端 logic/view.js 归一为 rank。
    strategy: Mapped[str] = mapped_column(String(64), default="")
    asof: Mapped[str] = mapped_column(String(16), default="")      # 信号基准交易日
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 买入风险分（百分点）：插件的风险剔除层给的历史证据见 README《涨停与风险分》。
    # 只有"过了闸门"的票才有值，闸门关闭或历史太短无法判分时为 NULL。
    risk_ex10_pp: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 买点参考位/失效位：来自策略 signal_at 里已经算出的均线、箱体上沿、6 日低点
    buy_ref_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_invalid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 第一止盈价 + 价位口径句子：买入=近 60 日筹码主峰下沿、止盈=主峰上沿、止损=买入−2×ATR。
    # 卖出价必须是独立列——藏在 reason 里，页面就得从中文句子抠数字，抠不出来就是空值。
    sell_target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    level_note: Mapped[str] = mapped_column(String(160), default="")
    # 风控后的最终决策（信号 ≠ 下单）：内核逐条过风控规则，被拦下的票根本不会落库，
    # 所以这里的 verdict 只有 allow（原样放行）与 adjust（放行但改了下单量）两种。
    # 数值口径全部由风控层算好，后端只搬运、App 只展示。
    risk_verdict: Mapped[str] = mapped_column(String(8), default="")     # allow|adjust，空=这批结果没开风控
    risk_reason: Mapped[str] = mapped_column(String(512), default="")    # adjust 时说明改了什么
    risk_shares: Mapped[int | None] = mapped_column(Integer, nullable=True)      # 建议下单股数（整手）
    risk_amount: Mapped[float | None] = mapped_column(Float, nullable=True)      # 建议下单金额（元）
    risk_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # 含滑点的预估成交价
    risk_slippage_bp: Mapped[float | None] = mapped_column(Float, nullable=True)  # 预估滑点（万分比）
    # 这条判据本身的历史后验（scripts/build_signal_posterior.py 落盘的表，按策略名查）。
    # 存的是「判据值多少钱」而不是「这只票打了多少分」，且 verdict 已经把
    # 「样本够不够」和「换时代/换抽样是否同号」合成一句话——页面只该显示 verdict，
    # 单独显示 excess_pp 会把翻号的抽样噪声说成结论。全为 NULL = 后验表还没建。
    posterior_verdict: Mapped[str] = mapped_column(String(64), default="")
    posterior_excess_pp: Mapped[float | None] = mapped_column(Float, nullable=True)
    posterior_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # adx14 闸门（第六条风控规则）在 shadow 期的留痕：当日全市场分位 + 放行/剔除。
    # 闸门没开或分位取不到时两列都是空——shadow 模式本来就不拦，落空是正常状态。
    adx14_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    adx14_gate: Mapped[str] = mapped_column(String(8), default="")
    # 引擎显式声明「这一项我算不出来」的字段名（逗号分隔，来自插件 extra.na_fields）。
    # 空串 = 引擎没做这个声明（自研内核走的就是这条：它那些列本来就有值）。
    # 为什么要有这一列：上面那些数值列的空值分不出「引擎根本不算」和「算了但这只判不出」，
    # 而 App 把空白渲染成「—」，用户会读成「没有风险」——这两件事在 qlib 引擎下必须分开说。
    na_fields: Mapped[str] = mapped_column(String(128), default="")
