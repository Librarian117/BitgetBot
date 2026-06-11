#!/usr/bin/env python3
"""
risk_monitor.py — 日内风控监控器 (Phase 1 提取自 deepseek_quant_bot.py)

v4.0 核心: 连续亏损 N 次 → 暂停 M 小时 (非百分比一刀切)
  - 连续亏损 5 次 → 暂停 2 小时
  - 连续亏损 8 次 → 暂停 12 小时
  - 币种越多, 阈值越宽 (动态调整)
  - 日亏损硬止损 (从 .env MAX_DAILY_LOSS_PCT 读取, 默认 3%)
"""

import logging
import time
from typing import Any, Dict, List

from config_manager import ConfigManager
from exchange_interface import ExchangeInterface
from time_utils import today_str

logger = logging.getLogger("QuantBot")


class RiskMonitor:
    """
    风控监控器 v4.0 — 连续亏损冷却 + 百分比硬止损
    ===============================================
    v4.0 核心: 连续亏损 N 次 → 暂停 M 小时 (非百分比一刀切)
      - 连续亏损 3 次 → 暂停 4 小时
      - 连续亏损 5 次 → 暂停 24 小时
      - 币种越多, 阈值越宽 (动态调整)
      - 日亏损硬止损 (从 .env MAX_DAILY_LOSS_PCT 读取, 默认 3%)
    """

    # v4.0: 基础阈值 (10 币种)
    # v4.1 fix: 10币种从3→5, 减少误触发 (正常波动不应锁死4小时)
    BASE_STREAK_WARN = 5      # 连续亏损此次数 → 暂停
    BASE_STREAK_HARD = 8      # 连续亏损此次数 → 长暂停
    BASE_COOLDOWN_WARN_H = 2  # 暂停小时 (从4h降到2h, 够冷静但不过度)
    BASE_COOLDOWN_HARD_H = 12 # 长暂停小时 (从24h降到12h)

    def __init__(self, config: ConfigManager, exchange: ExchangeInterface):
        self.config = config
        self.exchange = exchange
        self.day_start_equity: float = 0.0
        self.current_equity: float = 0.0
        self.daily_pnl_pct: float = 0.0
        self._current_date: str = ""
        self.cumulative_fees: float = 0.0
        self.initial_equity: float = config.initial_equity

        # v4.0: 从配置读取硬止损百分比 (默认 3%)
        self.hard_loss_pct = -abs(getattr(config, 'daily_loss_limit', 0.05))

        # v4.0: 连续亏损追踪
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.recent_pnls: List[float] = []  # 最近 20 笔 PnL (USDT)
        self.cooldown_until: float = 0.0     # 冷却结束时间戳
        self.cooldown_reason: str = ""

        self._refresh()
        if self.initial_equity <= 0 and self.current_equity > 0:
            self.initial_equity = self.current_equity
            logger.info(f"💰 初始资金自动检测: {self.initial_equity:.2f} USDT")

    # ════════════════════════════════════════════
    # v4.0: 动态阈值 (基于币种数)
    # ════════════════════════════════════════════

    def _get_thresholds(self) -> tuple:
        """根据监控币种数返回 (streak_warn, streak_hard, cooldown_warn_h, cooldown_hard_h)"""
        symbol_count = len(getattr(self.config, 'SYMBOLS', self.config.DEFAULT_SYMBOLS))
        if symbol_count <= 10:
            mult = 1.0
        elif symbol_count <= 20:
            mult = 1.3   # 币种多 → 阈值放宽 30%
        elif symbol_count <= 50:
            mult = 1.6   # 放宽 60%
        else:
            mult = 2.0   # 放宽 100%

        return (
            max(2, round(self.BASE_STREAK_WARN * mult)),
            max(3, round(self.BASE_STREAK_HARD * mult)),
            self.BASE_COOLDOWN_WARN_H,
            self.BASE_COOLDOWN_HARD_H,
        )

    # ════════════════════════════════════════════
    # 核心 API
    # ════════════════════════════════════════════

    def _refresh(self):
        acct = self.exchange.get_account_summary()
        self._refresh_with_data(acct)

    def _refresh_with_data(self, acct: Dict[str, Any]):
        today = today_str()
        equity = acct["equity"]
        if today != self._current_date:
            self._current_date = today
            self.cumulative_fees = 0.0
            self.day_start_equity = equity
            self.current_equity = equity
            self.daily_pnl_pct = 0.0
            logger.info(f"🌅 新交易日 {today} | 起始权益: {self.day_start_equity:.2f} USDT")
        else:
            self.current_equity = equity
            if self.day_start_equity > 0:
                self.daily_pnl_pct = (
                    (self.current_equity - self.day_start_equity) / self.day_start_equity
                )

    def record_trade_fees(self, fees: float):
        self.cumulative_fees += fees

    def record_closed_trade(self, pnl: float, symbol: str = ""):
        """v4.0: 记录已平仓交易 PnL, 追踪连续亏损"""
        is_loss = pnl < 0

        if is_loss:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        else:
            self.consecutive_wins += 1
            self.consecutive_losses = 0  # 盈利 → 重置

        self.recent_pnls.append(pnl)
        if len(self.recent_pnls) > 20:
            self.recent_pnls = self.recent_pnls[-20:]

        # 检查是否触发冷却
        self._check_streak_cooldown(symbol)

    def _check_streak_cooldown(self, symbol: str = ""):
        """v4.0: 连续亏损触发冷却"""
        now = time.time()
        streak_warn, streak_hard, cooldown_warn, cooldown_hard = self._get_thresholds()

        if self.consecutive_losses >= streak_hard:
            until = now + cooldown_hard * 3600
            if until > self.cooldown_until:
                self.cooldown_until = until
                self.cooldown_reason = (
                    f"连续亏损{self.consecutive_losses}次 "
                    f"(阈值{streak_hard}) → 暂停{cooldown_hard}h"
                )
                logger.error(f"🚨 {self.cooldown_reason}")

        elif self.consecutive_losses >= streak_warn and self.consecutive_losses < streak_hard:
            until = now + cooldown_warn * 3600
            if until > self.cooldown_until:
                self.cooldown_until = until
                self.cooldown_reason = (
                    f"连续亏损{self.consecutive_losses}次 "
                    f"(阈值{streak_warn}) → 暂停{cooldown_warn}h"
                )
                logger.error(f"⚠️  {self.cooldown_reason}")

    def can_trade(self) -> bool:
        """检查是否允许交易 (v4.0: 连续亏损冷却 + 百分比硬兜底)"""
        self._refresh()
        return self._check_trade_allowed()

    def can_trade_with_data(self, acct: Dict[str, Any]) -> bool:
        """v4.5: 使用同一份账户快照判断，不再重复 _refresh()"""
        self._refresh_with_data(acct)
        return self._check_trade_allowed()

    def _check_trade_allowed(self) -> bool:
        """v4.5: 不含 _refresh 的纯判断逻辑 — can_trade 和 can_trade_with_data 共用"""
        now = time.time()

        # 1. 冷却检查: 冷却期未过?
        if now < self.cooldown_until:
            remaining_h = (self.cooldown_until - now) / 3600
            logger.warning(
                f"⛔ 冷却中: {self.cooldown_reason} | "
                f"剩余 {remaining_h:.1f}h"
            )
            return False

        # 2. 冷却期已过 → 自动恢复
        if self.cooldown_until > 0 and now >= self.cooldown_until:
            logger.info(f"🟢 冷却期结束, 恢复交易 (连胜{self.consecutive_wins}笔)")
            self.cooldown_until = 0.0
            self.cooldown_reason = ""
            self.consecutive_losses = 0  # 重新计数

        # 3. 百分比硬兜底 (从 .env 读取, 默认 3%)
        if self.daily_pnl_pct < self.hard_loss_pct:
            # v4.5: 每小时只报一次，减少日志噪声
            if now - getattr(self, '_last_hard_loss_log', 0) > 3600:
                logger.error(
                    f"🚨 日内亏损 {self.daily_pnl_pct*100:.2f}% > "
                    f"{abs(self.hard_loss_pct)*100:.0f}%，硬止损！"
                )
                self._last_hard_loss_log = now
            return False

        return True

    def get_status(self) -> Dict[str, Any]:
        all_time_pnl = (self.current_equity - self.initial_equity) if self.initial_equity > 0 else 0.0
        all_time_pnl_pct = (all_time_pnl / self.initial_equity * 100) if self.initial_equity > 0 else 0.0
        streak_warn, streak_hard, _, _ = self._get_thresholds()
        cooldown_remaining = max(0, (self.cooldown_until - time.time()) / 3600) if self.cooldown_until > 0 else 0

        blocked = time.time() < self.cooldown_until or self.daily_pnl_pct < self.hard_loss_pct
        return {
            "date": self._current_date,
            "initial_equity": self.initial_equity,
            "start_equity": self.day_start_equity,
            "current_equity": self.current_equity,
            "pnl_pct": round(self.daily_pnl_pct * 100, 3),
            "all_time_pnl": round(all_time_pnl, 2),
            "all_time_pnl_pct": round(all_time_pnl_pct, 2),
            "blocked": blocked,
            "cumulative_fees": round(self.cumulative_fees, 4),
            # v4.0
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "streak_warn": streak_warn,
            "streak_hard": streak_hard,
            "cooldown_until": self.cooldown_until if self.cooldown_until > 0 else None,
            "cooldown_remaining_h": round(cooldown_remaining, 1),
            "cooldown_reason": self.cooldown_reason,
        }
