#!/usr/bin/env python3
"""
trailing_sl.py — 移动止损管理器 v1.0
====================================
价格朝有利方向移动时，自动跟紧止损位，锁定利润。

激活条件: 浮盈 > activation_atr × ATR
移动间距: 保持 distance_atr × ATR 的距离跟随最高/最低价
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("QuantBot")


class TrailingStopManager:
    """移动止损管理器"""

    def __init__(self, config, exchange, tlogger):
        self.config = config
        self.exchange = exchange
        self.tlogger = tlogger
        # {symbol: {"highest_price": float, "lowest_price": float, "sl_price": float}}
        self._trails: Dict[str, Dict[str, float]] = {}

    def check_and_update(
        self, symbol: str, side: str, mark_price: float,
        current_sl: float, atr: float, entry_price: float = 0,
    ) -> Optional[float]:
        """
        检查是否需要移动止损。返回新的 SL 价格，或 None（无需移动）。

        - LONG: 价格上涨 → SL 跟着上移
        - SHORT: 价格下跌 → SL 跟着下移

        激活条件: 价格朝有利方向移动 >= activation (activation_atr × ATR)
        之后才启动移动止损，避免微幅波动就把好单子扫出去。
        """
        activation = self.config.trailing_sl_activation_atr * atr
        distance = self.config.trailing_sl_distance_atr * atr

        trail = self._trails.setdefault(symbol, {
            "highest_price": mark_price,
            "lowest_price": mark_price,
            "sl_price": current_sl,
            "entry_price": entry_price if entry_price > 0 else mark_price,
        })

        if side == "LONG":
            # 追踪最高价
            if mark_price > trail["highest_price"]:
                trail["highest_price"] = mark_price

            ref_entry = trail.get("entry_price", trail["highest_price"])
            # 激活条件: 最高价必须超过入场价 + 激活距离，否则不移动
            if trail["highest_price"] - ref_entry < activation:
                return None

            new_sl = round(trail["highest_price"] - distance, 2)
            # 只有当新 SL > 当前 SL 时才移动（只朝有利方向移）
            if new_sl > current_sl:
                logger.info(
                    f"📈 {symbol} 移动止损: SL {current_sl}→{new_sl} "
                    f"(最高={trail['highest_price']:.2f}, 入场={ref_entry:.2f}, "
                    f"激活={activation:.2f}, 距离={distance:.2f})"
                )
                trail["sl_price"] = new_sl
                return new_sl

        else:  # SHORT
            if mark_price < trail["lowest_price"]:
                trail["lowest_price"] = mark_price

            ref_entry = trail.get("entry_price", trail["lowest_price"])
            # 激活条件: 入场价必须超过最低价 + 激活距离，否则不移动
            if ref_entry - trail["lowest_price"] < activation:
                return None

            new_sl = round(trail["lowest_price"] + distance, 2)
            if new_sl < current_sl:
                logger.info(
                    f"📉 {symbol} 移动止损: SL {current_sl}→{new_sl} "
                    f"(最低={trail['lowest_price']:.2f}, 入场={ref_entry:.2f}, "
                    f"激活={activation:.2f}, 距离={distance:.2f})"
                )
                trail["sl_price"] = new_sl
                return new_sl

        return None

    def remove(self, symbol: str):
        """仓位平仓后清除追踪数据"""
        self._trails.pop(symbol, None)
