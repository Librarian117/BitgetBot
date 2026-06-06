#!/usr/bin/env python3
"""
trade_logger.py — 交易日志持久化 (Phase 1 提取自 deepseek_quant_bot.py)

所有信号、AI 决策、交易执行记录写入 logs/trades_YYYY-MM-DD.jsonl (按日分割)
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from time_utils import now_iso, today_str

logger = logging.getLogger("QuantBot")


class TradeLogger:
    """所有信号、AI 决策、交易执行记录写入 logs/trades.jsonl (v2: 扩展字段)"""

    def __init__(self, log_dir: str = "logs"):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.log_dir = os.path.join(script_dir, log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        # v3.6: 按日分割日志，防止单文件无限膨胀
        self.today = today_str()
        self.log_path = os.path.join(self.log_dir, f"trades_{self.today}.jsonl")
        logger.info(f"📝 交易日志: {self.log_path}")

    def _write(self, record: Dict[str, Any]):
        """追加一行 JSON 到日志文件（按日分割）"""
        record.setdefault("timestamp", now_iso())
        # v3.6: 检查日期是否变更，自动切换文件
        today = today_str()
        if today != self.today:
            self.today = today
            self.log_path = os.path.join(self.log_dir, f"trades_{today}.jsonl")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"写入日志失败: {e}")

    def log_signal(self, sig: Dict[str, Any]):
        """记录候选信号"""
        self._write({
            "event": "SIGNAL",
            "symbol": sig["symbol"],
            "direction": sig["direction"],
            "price": sig["price"],
            "ema": sig["ema"],
            "rsi": sig["rsi"],
            "atr": sig["atr"],
            "adx": sig.get("adx"),
            "recent_closes": sig.get("recent_closes", [])[-5:],
        })

    def log_ai_decision(self, symbol: str, direction: str,
                        decision: str, reason: str):
        """记录 DeepSeek 决策"""
        self._write({
            "event": "AI_DECISION",
            "symbol": symbol,
            "direction": direction,
            "decision": decision,
            "reason": reason,
        })

    def log_trade(self, symbol: str, direction: str, price: float,
                  sl: float, tp_info: Any, amount: int,
                  order_id: Optional[str], success: bool,
                  entry_fee: float = 0.0, total_estimated_fee: float = 0.0,
                  margin_ratio_used: float = 0.0):
        """记录交易执行 (v2: 含手续费 & 部分TP信息)"""
        self._write({
            "event": "TRADE",
            "symbol": symbol,
            "direction": direction,
            "entry_price": price,
            "sl": sl,
            "tp_info": tp_info,
            "amount_contracts": amount,
            "order_id": order_id,
            "success": success,
            "entry_fee_usdt": round(entry_fee, 4),
            "total_estimated_fee_usdt": round(total_estimated_fee, 4),
            "margin_ratio_used": round(margin_ratio_used, 4),
        })

    def log_risk(self, event_type: str, details: str):
        """记录风控事件"""
        self._write({
            "event": "RISK",
            "risk_type": event_type,
            "details": details,
        })

    def log_cycle(self, cycle: int, candidates: int, trades: int,
                  balance: float, pnl: float = 0.0,
                  cumulative_fees: float = 0.0,
                  market_regime: str = "",
                  equity: float = 0.0, unrealized_pnl: float = 0.0,
                  initial_equity: float = 0.0, all_time_pnl: float = 0.0):
        """记录每轮扫描摘要 (v3.1: 初始资金 + 总盈亏)"""
        self._write({
            "event": "CYCLE",
            "cycle": cycle,
            "candidates": candidates,
            "trades_this_cycle": trades,
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "daily_pnl_pct": round(pnl, 4),
            "cumulative_fees": round(cumulative_fees, 4),
            "market_regime": market_regime,
            "initial_equity": round(initial_equity, 2),
            "all_time_pnl": round(all_time_pnl, 2),
        })

    def log_adx_skip(self, symbol: str, adx: float):
        """记录因 ADX 过低被过滤的信号"""
        self._write({
            "event": "ADX_SKIP",
            "symbol": symbol,
            "adx": round(adx, 2),
        })

    def log_tf_skip(self, symbol: str, direction: str, reason: str,
                    tf_context: Optional[Dict[str, Any]] = None):
        """记录因多TF趋势不一致被过滤的信号 (v2 新增)"""
        self._write({
            "event": "TF_SKIP",
            "symbol": symbol,
            "direction": direction,
            "reason": reason,
            "tf_context": (
                {k: v.get("trend") for k, v in tf_context.items()}
                if tf_context else {}
            ),
        })

    def log_concurrent_skip(self, num_existing: int, max_allowed: int):
        """记录因并发持仓满被跳过的轮次 (v2 新增)"""
        self._write({
            "event": "CONCURRENT_SKIP",
            "num_existing": num_existing,
            "max_allowed": max_allowed,
        })

    def log_vol_skip(self, symbol: str, vol_ratio: float):
        """记录因成交量不足被过滤的信号 (v2.1 新增)"""
        self._write({
            "event": "VOL_SKIP",
            "symbol": symbol,
            "vol_ratio": round(vol_ratio, 2),
        })

    def log_direction_skip(self, symbol: str, direction: str, reason: str):
        """v3.7: 记录因方向胜率过低被跳过的信号"""
        self._write({
            "event": "DIRECTION_SKIP",
            "symbol": symbol,
            "direction": direction,
            "reason": reason,
        })

    def log_btc_filter(self, symbol: str, direction: str,
                       btc_change: float, reason: str):
        """记录因 BTC 联动被拦截的信号 (v2.1 新增)"""
        self._write({
            "event": "BTC_FILTER",
            "symbol": symbol,
            "direction": direction,
            "btc_change_pct": round(btc_change * 100, 2),
            "reason": reason,
        })

    # ── v3.5: 结构化日志 — 支持自学习复盘 ──

    def log_position_close(self, symbol: str, direction: str, strategy: str,
                           entry_price: float, exit_price: float,
                           pnl: float, pnl_pct: float, close_reason: str,
                           holding_hours: float = 0):
        """记录平仓事件（自学习核心数据源）"""
        self._write({
            "event": "POSITION_CLOSE",
            "symbol": symbol,
            "direction": direction,
            "strategy": strategy,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "close_reason": close_reason,  # TP / SL / MANUAL / ROTATION
            "holding_hours": round(holding_hours, 1),
        })

    def log_signal_full(self, symbol: str, direction: str, strategy: str,
                        price: float, ema: float, rsi: float, atr: float,
                        adx: float, confidence: int, bonuses: list,
                        market_regime: str):
        """记录完整信号上下文（消融测试数据源）"""
        self._write({
            "event": "SIGNAL_FULL",
            "symbol": symbol,
            "direction": direction,
            "strategy": strategy,
            "price": round(price, 2),
            "ema": round(ema, 2),
            "rsi": round(rsi, 2),
            "atr": round(atr, 4),
            "adx": round(adx, 2),
            "confidence": confidence,
            "bonuses": bonuses,
            "market_regime": market_regime,
        })

    def log_cycle_positions(self, cycle: int, positions: list):
        """记录每轮持仓快照（PnL轨迹追踪）"""
        self._write({
            "event": "CYCLE_POSITIONS",
            "cycle": cycle,
            "positions": positions,  # [{symbol, side, entry, mark, upl, margin}]
        })

    def log_rule_exit_review(self, symbol: str, direction: str,
                             decision: str, reason: str,
                             entry_price: float, current_price: float,
                             roi: float, holding_hours: float):
        """v4.1: 记录规则引擎持仓审查决策 (替代旧AI_EXIT_DECISION)"""
        self._write({
            "event": "RULE_EXIT_REVIEW",
            "symbol": symbol,
            "direction": direction,
            "decision": decision,  # HOLD or CLOSE
            "reason": reason,
            "entry_price": round(entry_price, 4),
            "current_price": round(current_price, 4),
            "roi": round(roi * 100, 1),
            "holding_hours": round(holding_hours, 1),
        })

    def log_sl_attribution(self, symbol: str, direction: str,
                           exit_price: float, pnl: float, pnl_pct: float,
                           strategy: str = "pullback"):
        """v4.1: Stop Loss Attribution — 记录止损出场信息, 供后续分析"""
        self._write({
            "event": "SL_ATTRIBUTION",
            "symbol": symbol,
            "direction": direction,
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "strategy": strategy,
        })

    def log_filter_reject(self, symbol: str, reason: str, detail: str = "",
                          direction: str = "", strategy: str = ""):
        """v4.1: 统一过滤器拒绝日志，用于统计各过滤器拦截次数"""
        self._write({
            "event": "FILTER_REJECT",
            "symbol": symbol,
            "reason": reason,  # KALMAN_CONFLICT / ADX_TOO_LOW / COOLDOWN / DIRECTION_BLOCK / HURST / STRATEGY_ROUTE / TF_MISMATCH
            "detail": detail,
            "direction": direction,
            "strategy": strategy,
        })
