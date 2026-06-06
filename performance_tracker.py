#!/usr/bin/env python3
"""
performance_tracker.py — 绩效追踪器 v3.6
========================================
多维绩效追踪：策略/方向/时段拆分 + 滚动窗口 + 高级指标。

指标:
  - 胜率, 盈亏比, 期望值
  - 夏普 (Sharpe), 索提诺 (Sortino), 卡玛 (Calmar)
  - 最大回撤, 最大连胜/连败
  - 按策略/方向/币种拆分
  - 滚动窗口 (最近 20 笔)
"""

import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from time_utils import today_str, now_iso

logger = logging.getLogger("QuantBot")


class PerformanceTracker:
    """多维交易绩效追踪器 v3.6"""

    PERFORMANCE_FILE = "performance.json"
    MAX_TRADES_KEPT = 200

    def __init__(self, log_dir: str = "logs"):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._path = os.path.join(script_dir, self.PERFORMANCE_FILE)
        self.trades: List[Dict[str, Any]] = []
        self._load()

    # ════════════════════════════════════════════
    # 记录
    # ════════════════════════════════════════════

    def record_trade(
        self, symbol: str, direction: str, entry: float, exit_price: float,
        pnl: float, pnl_pct: float, strategy: str, duration_minutes: float = 0,
    ):
        trade = {
            "timestamp": now_iso(),
            "date": today_str(),
            "symbol": symbol,
            "direction": direction,
            "entry": round(entry, 4),
            "exit": round(exit_price, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "strategy": strategy,
            "duration_min": round(duration_minutes, 1),
            "win": pnl > 0,
        }
        self.trades.append(trade)
        # 保留最近 N 笔，超出截断
        if len(self.trades) > self.MAX_TRADES_KEPT:
            self.trades = self.trades[-self.MAX_TRADES_KEPT:]
        self._save()
        logger.info(
            f"📊 {symbol} {direction} pnl={pnl:+.2f} ({pnl_pct:+.2f}%) "
            f"{duration_minutes:.0f}min [{strategy}]"
        )

    # ════════════════════════════════════════════
    # 核心指标
    # ════════════════════════════════════════════

    def get_metrics(self, trades: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """计算核心绩效指标。传入 trades 子集可得到子集指标。"""
        tlist = trades or self.trades
        if not tlist:
            return self._empty_metrics()

        wins = [t for t in tlist if t["win"]]
        losses = [t for t in tlist if not t["win"]]
        total = len(tlist)
        win_rate = len(wins) / total if total > 0 else 0
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["pnl"] for t in losses)) / len(losses) if losses else 0
        total_pnl = sum(t["pnl"] for t in tlist)

        # Profit Factor
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Expectancy
        expectancy = (win_rate * avg_win - (1 - win_rate) * avg_loss)

        pnls = [t["pnl"] for t in tlist]
        mean_pnl = sum(pnls) / len(pnls) if pnls else 0

        # Sharpe (简化)
        std_pnl = self._stddev(pnls)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(len(pnls)) if std_pnl > 0 else 0

        # Sortino (只惩罚下行波动)
        downside = [p for p in pnls if p < 0]
        if downside:
            down_mean = sum(downside) / len(downside)
            down_std = math.sqrt(sum((p - down_mean) ** 2 for p in downside) / len(downside))
        else:
            down_std = 0
        sortino = (mean_pnl / down_std) * math.sqrt(total) if down_std > 0 else 0

        # Max Drawdown
        max_dd, dd_pct = self._max_drawdown(pnls)

        # Calmar (年化收益 / 最大回撤)
        annual_return = mean_pnl * total / (total or 1) * 252
        calmar = annual_return / max(1, max_dd) if max_dd > 0 else 0

        # 连胜/连败
        max_consecutive_wins = max_consecutive_losses = 0
        cur_wins = cur_losses = 0
        for t in tlist:
            if t["win"]:
                cur_wins += 1
                cur_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, cur_losses)

        return {
            "total_trades": total,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "profit_factor": round(profit_factor, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "calmar_ratio": round(calmar, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(dd_pct, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "total_pnl": round(total_pnl, 2),
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
        }

    # ════════════════════════════════════════════
    # 多维拆分
    # ════════════════════════════════════════════

    def get_strategy_metrics(self) -> Dict[str, Dict]:
        """按策略拆分"""
        result = {}
        for strat in set(t.get("strategy", "pullback") for t in self.trades):
            subset = [t for t in self.trades if t.get("strategy") == strat]
            result[strat] = self.get_metrics(subset)
        return result

    def get_direction_metrics(self) -> Dict[str, Dict]:
        """按方向拆分"""
        result = {}
        for d in set(t.get("direction", "SHORT") for t in self.trades):
            subset = [t for t in self.trades if t.get("direction") == d]
            result[d] = self.get_metrics(subset)
        return result

    def get_symbol_metrics(self) -> Dict[str, Dict]:
        """按币种拆分"""
        result = {}
        for sym in set(t.get("symbol", "?") for t in self.trades):
            subset = [t for t in self.trades if t.get("symbol") == sym]
            result[sym] = self.get_metrics(subset)
        return result

    def get_rolling_metrics(self, window: int = 20) -> Dict[str, Any]:
        """最近 N 笔滚动窗口指标"""
        return self.get_metrics(self.trades[-window:])

    def get_today_metrics(self) -> Dict[str, Any]:
        """今日指标"""
        today = today_str()
        subset = [t for t in self.trades if t.get("date") == today]
        return self.get_metrics(subset)

    # ════════════════════════════════════════════
    # 工具
    # ════════════════════════════════════════════

    @staticmethod
    def _stddev(values: List[float]) -> float:
        if not values or len(values) < 2:
            return 1.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))

    @staticmethod
    def _max_drawdown(pnls: List[float]) -> Tuple[float, float]:
        """返回 (最大回撤绝对值, 回撤百分比)"""
        cum = 0
        peak = -float("inf")
        max_dd = 0
        max_dd_pct = 0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = peak - cum
            dd_pct = (dd / peak * 100) if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        return max_dd, max_dd_pct

    @staticmethod
    def _empty_metrics() -> Dict[str, Any]:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "profit_factor": 0, "sharpe_ratio": 0,
            "sortino_ratio": 0, "calmar_ratio": 0,
            "max_drawdown": 0, "max_drawdown_pct": 0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "avg_win": 0, "avg_loss": 0, "expectancy": 0,
            "total_pnl": 0, "best_trade": 0, "worst_trade": 0,
        }

    def _save(self):
        try:
            data = {
                "updated_at": now_iso(),
                "overall": self.get_metrics(),
                "rolling_20": self.get_rolling_metrics(20),
                "trades": self.trades[-100:],
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error(f"📊 保存绩效失败: {e}")

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trades = data.get("trades", [])
            if self.trades:
                m = data.get("overall", {})
                logger.info(
                    f"📊 已加载绩效: {len(self.trades)}笔, "
                    f"WR={m.get('win_rate',0):.0f}% PnL={m.get('total_pnl',0):+.1f}"
                )
        except Exception:
            self.trades = []
