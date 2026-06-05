#!/usr/bin/env python3
"""
safety_manager.py — 实盘交易安全层 v3.6
========================================
根据 Bitget v2 API 文档重构。沙盒/实盘通用安全检查。

检查项:
  1. 最小仓位价值
  2. 费后利润门槛
  3. 总风险敞口 (保证金/权益)
  4. 最大并发持仓
  5. API 连续错误熔断
  6. 紧急回撤停止
  7. 紧急全平 (使用 close_position API)
  8. 限价单后备 (API 文档合规)
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from time_utils import now

logger = logging.getLogger("QuantBot")


class SafetyManager:
    """实盘交易安全层 v3.6"""

    # v3.6: 内部熔断
    MAX_CONSECUTIVE_API_ERRORS = 5
    MAX_TOTAL_EXPOSURE_RATIO = 0.8  # 总保证金不超过权益 80%

    def __init__(self, config, exchange, tlogger):
        self.config = config
        self.exchange = exchange
        self.tlogger = tlogger
        self._is_sandbox = self._detect_sandbox()
        self._consecutive_api_errors = 0
        self._api_circuit_open = False

        mode = "沙箱" if self._is_sandbox else "实盘"
        logger.info(f"🛡️  安全层已加载 ({mode}模式)")

    def _detect_sandbox(self) -> bool:
        try:
            return getattr(self.exchange.exchange, "sandbox_mode", False)
        except Exception:
            return False

    # ════════════════════════════════════════════
    # 仓位检查
    # ════════════════════════════════════════════

    def check_min_position_value(
        self, position_value: float, symbol: str
    ) -> Tuple[bool, str]:
        """拒绝小于最低价值的仓位"""
        min_val = self.config.min_position_value
        if position_value < min_val:
            return False, f"仓位价值 {position_value:.2f} < 最低 {min_val} USDT"
        return True, ""

    def check_profit_after_fees(
        self, position_value: float, atr: float, fee_rate: float,
        tp_atr_mult: float, entry_price: float,
    ) -> Tuple[bool, str]:
        """检查潜在利润是否覆盖手续费"""
        if entry_price <= 0:
            return False, "entry_price 无效"
        tp_dist = tp_atr_mult * atr
        est_profit = position_value * (tp_dist / entry_price)
        total_fee = position_value * fee_rate * 2
        min_profit = total_fee * self.config.profit_fee_multiplier

        if est_profit < min_profit:
            return False, (
                f"估算利润 {est_profit:.4f} < {min_profit:.4f} "
                f"({self.config.profit_fee_multiplier}x 手续费)"
            )
        return True, ""

    # ════════════════════════════════════════════
    # v3.6: 风控检查 (新增)
    # ════════════════════════════════════════════

    def check_total_exposure(
        self, positions_detail: List[Dict], equity: float
    ) -> Tuple[bool, str]:
        """总风险敞口 = 所有仓位保证金 / 权益，不超过 80%"""
        if not positions_detail or equity <= 0:
            return True, ""
        total_margin = sum(abs(p.get("margin", 0) or 0) for p in positions_detail)
        ratio = total_margin / equity
        if ratio > self.MAX_TOTAL_EXPOSURE_RATIO:
            return False, f"总保证金 {total_margin:.0f}/{equity:.0f} = {ratio*100:.0f}% > {self.MAX_TOTAL_EXPOSURE_RATIO*100:.0f}%"
        return True, ""

    def check_max_positions(self, current_count: int) -> Tuple[bool, str]:
        """防止超过最大并发持仓"""
        max_pos = self.config.max_concurrent_positions
        if current_count > max_pos:
            return False, f"持仓数 {current_count} > 上限 {max_pos}"
        return True, ""

    def record_api_error(self):
        """v3.6: 记录一次 API 错误，连续 N 次则熔断"""
        self._consecutive_api_errors += 1
        if self._consecutive_api_errors >= self.MAX_CONSECUTIVE_API_ERRORS:
            self._api_circuit_open = True
            logger.error(f"🛡️ API 连续 {self._consecutive_api_errors} 次错误，安全熔断!")
            self.tlogger.log_risk("SAFETY_CIRCUIT", "API 连续错误触发安全熔断")

    def record_api_success(self):
        """重置 API 错误计数器"""
        if self._consecutive_api_errors > 0:
            self._consecutive_api_errors = 0
            if self._api_circuit_open:
                self._api_circuit_open = False
                logger.info("🛡️ API 恢复，安全熔断解除")

    def is_circuit_open(self) -> bool:
        return self._api_circuit_open

    # ════════════════════════════════════════════
    # 订单执行
    # ════════════════════════════════════════════

    def place_with_limit_fallback(
        self, symbol: str, side: str, amount: float,
        sl_price: float, tp_parts: list,
    ) -> Optional[Dict[str, Any]]:
        """先市价单，失败降级限价单 (v3.6: API 文档合规)"""
        try:
            order = self.exchange.create_market_order_with_partial_tp(
                symbol=symbol, side=side, amount=amount,
                sl_price=sl_price, tp_parts=tp_parts,
            )
            if order:
                return order
        except Exception as e:
            logger.warning(f"⚠️  市价单失败 {symbol}: {e}")

        if not self.config.limit_fallback_enabled:
            return None

        logger.info(f"🔄 {symbol} 降级为限价单 …")
        try:
            ticker = self.exchange.exchange.fetch_ticker(symbol)
            mark_price = float(ticker.get("last", 0))
            if mark_price <= 0:
                return None

            offset = self.config.limit_fallback_offset
            limit_price = (
                mark_price * (1 + offset) if side == "buy"
                else mark_price * (1 - offset)
            )

            # v3.6: place-order API — 只用 tradeSide: open, 不传 holdSide
            order = self.exchange.create_limit_order(
                symbol=symbol, side=side, amount=amount, price=limit_price,
                params={"tradeSide": "open"},
            )
            if order:
                logger.info(f"✅ 限价单成交 {symbol} {side} @{limit_price:.4f}")
                if tp_parts:
                    tp_price = tp_parts[0][0]
                    self.exchange.set_position_sl_tp(symbol, side, sl_price, tp_price)
            return order
        except Exception as e:
            logger.error(f"❌ 限价单后备失败 {symbol}: {e}")
            return None

    def confirm_order_fill(
        self, order_id: str, symbol: str, timeout: int = 15
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """轮询确认订单成交"""
        if self._is_sandbox:
            return True, None

        if not self.config.order_confirmation_enabled:
            return True, None

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = self.exchange.fetch_order(order_id, symbol)
                if order:
                    status = str(order.get("status", "")).lower()
                    if status in ("closed", "filled"):
                        return True, order
                    elif status in ("canceled", "expired", "rejected"):
                        return False, order
            except Exception as e:
                logger.debug(f"订单确认轮询: {e}")
            time.sleep(1)

        logger.warning(f"⚠️  订单确认超时 {order_id} (>{timeout}s)")
        return False, None

    # ════════════════════════════════════════════
    # 紧急停止
    # ════════════════════════════════════════════

    def check_emergency_stop(
        self, current_equity: float,
        equity_history: List[Tuple[float, float]],
    ) -> Tuple[bool, str]:
        """检查是否需要紧急停止"""
        if self._is_sandbox:
            return False, ""

        if not self.config.emergency_stop_enabled:
            return False, ""

        if len(equity_history) < 2:
            return False, ""

        window_start = time.time() - self.config.emergency_window
        window_entries = [(ts, eq) for ts, eq in equity_history if ts >= window_start]
        if len(window_entries) < 2:
            return False, ""

        peak = max(eq for _, eq in window_entries)
        if peak <= 0:
            return False, ""

        drawdown = (peak - current_equity) / peak
        if drawdown >= self.config.emergency_drawdown:
            return True, (
                f"{self.config.emergency_window}s 内回撤 {drawdown*100:.1f}% >= "
                f"{self.config.emergency_drawdown*100:.0f}%"
            )
        return False, ""

    def emergency_close_all(self, positions: Dict[str, Any]) -> int:
        """
        v3.6: 使用 close_position API 平掉所有持仓。
        根据 position info holdSide 判断方向。
        """
        if self._is_sandbox:
            logger.warning("🚨 沙箱模式：跳过紧急平仓")
            return 0

        closed = 0
        for sym, pos in positions.items():
            try:
                contracts = abs(float(pos.get("contracts", 0) or 0))
                if contracts <= 0:
                    continue

                # v3.6: 从 position info 获取 holdSide
                info = pos.get("info", {})
                hold_side = info.get("holdSide", "")
                if not hold_side:
                    # fallback: infer from side
                    side = pos.get("side", "long")
                    hold_side = "short" if side == "SHORT" else "long"

                order = self.exchange.create_market_order_close(
                    symbol=sym, amount=contracts,
                    side="buy" if hold_side == "short" else "sell",
                    pos_side=hold_side,
                )
                if order:
                    closed += 1
                    self.tlogger.log_risk("EMERGENCY_CLOSE", f"{sym}: {contracts}张平仓")
                    logger.error(f"🚨 紧急平仓 {sym}: {contracts}张")
            except Exception as e:
                logger.error(f"❌ 紧急平仓失败 {sym}: {e}")

        return closed
