"""Strategy definitions for multi-leg option combos.

Pure data module — no Qt or IBKR imports.
"""

from dataclasses import dataclass, field
from enum import Enum


class StrategyType(Enum):
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    BULL_PUT_SPREAD = "bull_put_spread"
    LONG_CALL_BUTTERFLY = "long_call_butterfly"
    LONG_PUT_BUTTERFLY = "long_put_butterfly"
    IRON_CONDOR = "iron_condor"
    IRON_BUTTERFLY = "iron_butterfly"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    CALENDAR_SPREAD = "calendar_spread"
    CUSTOM = "custom"


@dataclass
class LegTemplate:
    """Template for a single leg in a strategy."""
    strike_param: str      # e.g. "strike1", "strike2", "strike_mid"
    right: str             # "C" or "P"
    action: str            # "BUY" or "SELL"
    ratio: int = 1         # Number of contracts per unit
    expiry_param: str = "expiry"  # Which expiry param to use


@dataclass
class StrategyTemplate:
    """Template for a multi-leg option strategy."""
    strategy_type: StrategyType
    display_name: str
    legs: list[LegTemplate]
    strike_params: list[str]       # e.g. ["strike1", "strike2"]
    expiry_params: list[str] = field(default_factory=lambda: ["expiry"])
    description: str = ""
    is_debit: bool = True


STRATEGY_REGISTRY: dict[StrategyType, StrategyTemplate] = {

    StrategyType.BULL_CALL_SPREAD: StrategyTemplate(
        strategy_type=StrategyType.BULL_CALL_SPREAD,
        display_name="Bull Call Spread (牛市看涨价差)",
        legs=[
            LegTemplate("strike1", "C", "BUY"),
            LegTemplate("strike2", "C", "SELL"),
        ],
        strike_params=["strike1", "strike2"],
        description="买低行权价Call, 卖高行权价Call. 看涨, 有限风险/收益.",
        is_debit=True,
    ),

    # 约定: strike1 = 低行权价, strike2 = 高行权价 (UI 标签「低/高行权价」按此填)
    StrategyType.BEAR_PUT_SPREAD: StrategyTemplate(
        strategy_type=StrategyType.BEAR_PUT_SPREAD,
        display_name="Bear Put Spread (熊市看跌价差)",
        legs=[
            LegTemplate("strike2", "P", "BUY"),    # 买高行权价 Put
            LegTemplate("strike1", "P", "SELL"),   # 卖低行权价 Put
        ],
        strike_params=["strike1", "strike2"],
        description="买高行权价Put, 卖低行权价Put. 看跌, 有限风险/收益.",
        is_debit=True,
    ),

    StrategyType.BEAR_CALL_SPREAD: StrategyTemplate(
        strategy_type=StrategyType.BEAR_CALL_SPREAD,
        display_name="Bear Call Spread (熊市看涨信用价差)",
        legs=[
            LegTemplate("strike1", "C", "SELL"),
            LegTemplate("strike2", "C", "BUY"),
        ],
        strike_params=["strike1", "strike2"],
        description="卖低行权价Call, 买高行权价Call. 看跌, 收取权利金.",
        is_debit=False,
    ),

    StrategyType.BULL_PUT_SPREAD: StrategyTemplate(
        strategy_type=StrategyType.BULL_PUT_SPREAD,
        display_name="Bull Put Spread (牛市看跌信用价差)",
        legs=[
            LegTemplate("strike2", "P", "SELL"),   # 卖高行权价 Put
            LegTemplate("strike1", "P", "BUY"),    # 买低行权价 Put
        ],
        strike_params=["strike1", "strike2"],
        description="卖高行权价Put, 买低行权价Put. 看涨, 收取权利金.",
        is_debit=False,
    ),

    StrategyType.LONG_CALL_BUTTERFLY: StrategyTemplate(
        strategy_type=StrategyType.LONG_CALL_BUTTERFLY,
        display_name="Long Call Butterfly (蝶式看涨)",
        legs=[
            LegTemplate("strike1", "C", "BUY"),
            LegTemplate("strike2", "C", "SELL", ratio=2),
            LegTemplate("strike3", "C", "BUY"),
        ],
        strike_params=["strike1", "strike2", "strike3"],
        description="买低Call, 卖2x中间Call, 买高Call. 预期窄幅震荡.",
        is_debit=True,
    ),

    StrategyType.LONG_PUT_BUTTERFLY: StrategyTemplate(
        strategy_type=StrategyType.LONG_PUT_BUTTERFLY,
        display_name="Long Put Butterfly (蝶式看跌)",
        legs=[
            LegTemplate("strike1", "P", "BUY"),
            LegTemplate("strike2", "P", "SELL", ratio=2),
            LegTemplate("strike3", "P", "BUY"),
        ],
        strike_params=["strike1", "strike2", "strike3"],
        description="买低Put, 卖2x中间Put, 买高Put. 预期窄幅震荡.",
        is_debit=True,
    ),

    StrategyType.IRON_CONDOR: StrategyTemplate(
        strategy_type=StrategyType.IRON_CONDOR,
        display_name="Iron Condor (铁秃鹰)",
        legs=[
            LegTemplate("strike1", "P", "BUY"),
            LegTemplate("strike2", "P", "SELL"),
            LegTemplate("strike3", "C", "SELL"),
            LegTemplate("strike4", "C", "BUY"),
        ],
        strike_params=["strike1", "strike2", "strike3", "strike4"],
        description="买低Put, 卖中低Put, 卖中高Call, 买高Call. 预期窄幅震荡, 收取权利金.",
        is_debit=False,
    ),

    StrategyType.IRON_BUTTERFLY: StrategyTemplate(
        strategy_type=StrategyType.IRON_BUTTERFLY,
        display_name="Iron Butterfly (铁蝶式)",
        legs=[
            LegTemplate("strike1", "P", "BUY"),
            LegTemplate("strike2", "P", "SELL"),
            LegTemplate("strike2", "C", "SELL"),
            LegTemplate("strike3", "C", "BUY"),
        ],
        strike_params=["strike1", "strike2", "strike3"],
        description="买低Put, 卖ATM Put+Call, 买高Call. 预期窄幅震荡, 中间行权价共用.",
        is_debit=False,
    ),

    StrategyType.STRADDLE: StrategyTemplate(
        strategy_type=StrategyType.STRADDLE,
        display_name="Straddle (跨式)",
        legs=[
            LegTemplate("strike1", "C", "BUY"),
            LegTemplate("strike1", "P", "BUY"),
        ],
        strike_params=["strike1"],
        description="同一行权价同时买Call和Put. 预期大幅波动, 方向不明.",
        is_debit=True,
    ),

    StrategyType.STRANGLE: StrategyTemplate(
        strategy_type=StrategyType.STRANGLE,
        display_name="Strangle (宽跨式)",
        legs=[
            LegTemplate("strike1", "P", "BUY"),
            LegTemplate("strike2", "C", "BUY"),
        ],
        strike_params=["strike1", "strike2"],
        description="买低行权价Put + 买高行权价Call. 预期大幅波动, 成本低于跨式.",
        is_debit=True,
    ),

    StrategyType.CALENDAR_SPREAD: StrategyTemplate(
        strategy_type=StrategyType.CALENDAR_SPREAD,
        display_name="Calendar Spread (日历价差)",
        legs=[
            LegTemplate("strike1", "C", "SELL", expiry_param="expiry_near"),
            LegTemplate("strike1", "C", "BUY", expiry_param="expiry_far"),
        ],
        strike_params=["strike1"],
        expiry_params=["expiry_near", "expiry_far"],
        description="卖近月Call, 买远月Call, 同一行权价. 利用时间价值衰减差异.",
        is_debit=True,
    ),

    StrategyType.CUSTOM: StrategyTemplate(
        strategy_type=StrategyType.CUSTOM,
        display_name="Custom Combo (自定义组合)",
        legs=[],
        strike_params=[],
        description="自定义多腿组合. 手动添加每条腿.",
        is_debit=True,
    ),
}
