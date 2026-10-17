"""回测模拟账户：资金/持仓/T+1/手续费/整手/仓位上限。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 费率真源：任何按「比例」算成本的地方（逐笔扫描、判定台账、动作模型的分支差额）都从这里取，
# 别在脚本里再抄一遍万 2.5 / 0.05%——抄错不会报错，只会让两边的钱不一样。
# commission_min 是「单笔最低 5 元」，不按比例走，所以只适用于账户，不适用于 pp 口径。
COMMISSION_RATE = 0.00025
STAMP_TAX_RATE = 0.0005
SELL_FEE_RATE = STAMP_TAX_RATE + COMMISSION_RATE      # 卖出单边费率（佣金 + 印花税）


@dataclass
class Position:
    shares: int = 0
    avg_price: float = 0.0
    cost: float = 0.0


@dataclass
class Trade:
    task_id: Optional[str] = None
    code: str = ""
    name: str = ""
    side: str = ""
    date: str = ""
    price: float = 0.0
    shares: int = 0
    amount: float = 0.0
    fee: float = 0.0
    pnl: Optional[float] = None


class Broker:
    """A股模拟账户：收盘价成交、T+1 资金、佣金最低5元、卖出印花税、整手100股。"""

    LOT = 100

    def __init__(
        self,
        capital: float = 100000.0,
        max_positions: int = 5,
        position_cap_pct: float = 0.2,
        commission_rate: float = COMMISSION_RATE,
        commission_min: float = 5.0,
        stamp_tax_rate: float = STAMP_TAX_RATE,
    ) -> None:
        self.cash = float(capital)
        self.settling = 0.0
        self.max_positions = max_positions
        self.position_cap_pct = position_cap_pct
        self.commission_rate = commission_rate
        self.commission_min = commission_min
        self.stamp_tax_rate = stamp_tax_rate
        # 组合层现金下限（批次 E）：这部分现金今天不许用来买/加。默认 0 = 现有行为。
        # 由引擎逐日设置，只卡买入与加仓，从不因为下限而强制卖。
        self.cash_floor = 0.0
        self.floor_blocked = 0
        # 被单票仓位上限拒掉的单数。以前超限只 raise、引擎 `except ValueError` 静默跳过，
        # 于是批次 E 的基准在净值缩水后整年买不动也没人报警——这个计数就是那件事的探针。
        self.cap_blocked = 0
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []

    # ---- 费用 ----
    def commission(self, amount: float) -> float:
        return max(amount * self.commission_rate, self.commission_min)

    def stamp_tax(self, amount: float) -> float:
        return amount * self.stamp_tax_rate

    def available_cash(self) -> float:
        """扣掉预留下限后今天真正能动用的现金。"""
        return max(0.0, self.cash - self.cash_floor)

    # ---- 校验 ----
    def _check_cash(self, code: str, total_need: float) -> None:
        """钱够不够：先看扣掉预留下限之后能动用的，再看账面现金。

        「本来买得起、是被预留挡下来的」单独计数——批次 E 要知道少买的那些笔
        到底是没钱还是被调度拦住的，不然「预留」和「没子弹」在两笔账上混成一团。
        """
        available = self.available_cash()
        if total_need <= available:
            return
        if total_need <= self.cash:
            self.floor_blocked += 1
            raise ValueError(f"{code} 动用预留下限外现金: 需 {total_need:.2f}, "
                             f"可动用 {available:.2f}（现金 {self.cash:.2f}，下限 {self.cash_floor:.2f}）")
        raise ValueError(f"{code} 资金不足: 需 {total_need:.2f}, 可用 {available:.2f}")

    def _check_single_stock_cap(self, code: str, add_value: float) -> None:
        """校验加仓/买入后该票成本不超单票仓位上限（按当前总资产口径）。"""
        current = self.positions.get(code, Position()).cost
        total = self.total_present_value({})
        cap_value = total * self.position_cap_pct
        if current + add_value > cap_value:
            self.cap_blocked += 1
            raise ValueError(
                f"{code} 超出单票仓位上限 {cap_value:.2f}"
            )

    # ---- 成交 ----
    def buy(self, code: str, name: str, date: str, price: float, amount: float) -> int:
        """以目标金额买入，返回成交股数（100股整数倍）。"""
        shares = int(amount / price // self.LOT) * self.LOT
        if shares < self.LOT:
            raise ValueError(f"{code} 买入不足一手({self.LOT}股)")
        gross = shares * price
        fee = self.commission(gross)
        total_need = gross + fee
        self._check_cash(code, total_need)
        if code not in self.positions and len(self.positions) >= self.max_positions:
            raise ValueError(f"已达持仓只数上限 {self.max_positions}")
        self._check_single_stock_cap(code, gross)

        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = Position(shares=shares, avg_price=price, cost=gross)
        else:
            new_shares = pos.shares + shares
            pos.cost += gross
            pos.avg_price = pos.cost / new_shares
            pos.shares = new_shares

        self.cash -= total_need
        self.trades.append(Trade(code=code, name=name, side="buy", date=date,
                                 price=price, shares=shares, amount=gross, fee=fee))
        return shares

    def add(self, code: str, name: str, date: str, price: float, amount: float) -> int:
        """加仓（不占持仓只数配额）。"""
        if code not in self.positions:
            raise KeyError(f"{code} 无持仓可加")
        shares = int(amount / price // self.LOT) * self.LOT
        if shares < self.LOT:
            raise ValueError(f"{code} 加仓不足一手")
        gross = shares * price
        fee = self.commission(gross)
        total_need = gross + fee
        self._check_cash(code, total_need)
        self._check_single_stock_cap(code, gross)

        pos = self.positions[code]
        pos.cost += gross
        pos.shares += shares
        pos.avg_price = pos.cost / pos.shares

        self.cash -= total_need
        self.trades.append(Trade(code=code, name=name, side="add", date=date,
                                 price=price, shares=shares, amount=gross, fee=fee))
        return shares

    def sell(self, code: str, date: str, price: float, shares: Optional[int] = None) -> float:
        """卖出，返回净到账（成交额-佣金-印花税），资金进 settling(T+1)。"""
        pos = self.positions.get(code)
        if pos is None:
            raise KeyError(f"{code} 无持仓可卖")
        sell_shares = pos.shares if shares is None else shares
        if sell_shares > pos.shares or sell_shares <= 0:
            raise ValueError(f"{code} 卖出股数非法")

        gross = sell_shares * price
        fee = self.commission(gross) + self.stamp_tax(gross)
        proceeds = gross - fee
        self.settling += proceeds

        # 已实现盈亏（含费用）：按均价成本
        cost_part = pos.avg_price * sell_shares
        pnl = gross - cost_part - fee
        self.trades.append(Trade(code=code, side="sell", date=date, price=price,
                                 shares=sell_shares, amount=gross, fee=fee, pnl=pnl))

        if sell_shares >= pos.shares:
            del self.positions[code]
        else:
            pos.shares -= sell_shares
            pos.cost -= cost_part
            if pos.shares > 0:
                pos.avg_price = pos.cost / pos.shares
        return proceeds

    # ---- 资金到账 ----
    def settle(self) -> None:
        """T+1：昨日 settling 并入 cash。"""
        if self.settling:
            self.cash += self.settling
            self.settling = 0.0

    # ---- 估值 ----
    def positions_value(self, prices: dict[str, float]) -> float:
        val = 0.0
        for code, pos in self.positions.items():
            px = prices.get(code, pos.avg_price)
            val += pos.shares * px
        return val

    def total_present_value(self, prices: dict[str, float]) -> float:
        return self.cash + self.settling + self.positions_value(prices)

    def total_value(self, prices: dict[str, float]) -> float:
        return self.total_present_value(prices)
