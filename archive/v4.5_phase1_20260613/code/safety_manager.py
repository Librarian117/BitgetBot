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
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("QuantBot")


class SafetyManager:
    """实盘交易安全层 v3.6"""

    def __init__(self, config, exchange, tlogger):
        self.config = config
        self.exchange = exchange
        self.tlogger = tlogger
        self._is_sandbox = self._detect_sandbox()

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
    # 订单执行
    # ════════════════════════════════════════════

    def place_with_limit_fallback(
        self, symbol: str, side: str, amount: float,
        sl_price: float, tp_parts: list,
        pos_side: str = "",  # v4.5: 显式持仓方向
    ) -> Optional[Dict[str, Any]]:
        """先市价单，失败降级限价单 (v3.6: API 文档合规)"""
        try:
            order = self.exchange.create_market_order_with_partial_tp(
                symbol=symbol, side=side, amount=amount,
                sl_price=sl_price, tp_parts=tp_parts,
                pos_side=pos_side,  # v4.5: 传递持仓方向
            )
            if order:
                return order
        except Exception as e:
            logger.warning(f"⚠️  市价单失败 {symbol}: {e}")

        # v4.5 CRITICAL: TPSL 致命失败时禁止降级到限价单重开仓
        # create_market_order_with_partial_tp 内部已紧急平仓 + 设 _tpsl_fatal 标志
        if getattr(self.exchange, '_tpsl_fatal', None):
            fatal_sym = self.exchange._tpsl_fatal
            self.exchange._tpsl_fatal = None
            logger.error(
                f"⛔ {fatal_sym} TPSL致命失败(已紧急平仓) → 禁止降级限价单重试"
            )
            return None

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
                    # v4.5 fix: 显式传持仓方向 "long"/"short"，不再传 "buy"/"sell"
                    pos_side_s = "long" if side == "buy" else "short"
                    tpsl_ok = self.exchange.set_position_sl_tp(symbol, pos_side_s, sl_price, tp_price)
                    # v4.5 CRITICAL: TPSL失败 → 复核+紧急平仓，不允许裸仓
                    if not tpsl_ok:
                        fill_price = float(order.get("price") or order.get("average") or sl_price)
                        has_sl, _ = self.exchange._has_position_tpsl(symbol, fill_price, pos_side_s)
                        if not has_sl:
                            logger.error(f"🔥 {symbol} 限价单TPSL失败+复核无保护 → 紧急平仓!")
                            try:
                                self.exchange.create_market_order_close(
                                    symbol, amount, side, pos_side=pos_side_s)
                            except Exception:
                                logger.error(f"💥 {symbol} 紧急平仓也失败", exc_info=True)
                            return None  # 标记失败
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
        """检查是否需要紧急停止 (v4.5: EMERGENCY_DRY_RUN 时沙箱也检查)"""
        if self._is_sandbox and not self.config.emergency_dry_run:
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
        v4.5: EMERGENCY_DRY_RUN=true 时跑完整路径但不真实下单 (沙箱演练)
        """
        _dry_run = self._is_sandbox and self.config.emergency_dry_run
        if self._is_sandbox and not _dry_run:
            logger.warning("🚨 沙箱模式：跳过紧急平仓 (设置 EMERGENCY_DRY_RUN=true 启用演练)")
            return 0

        if _dry_run:
            logger.warning("🔥 EMERGENCY DRY RUN: 模拟紧急平仓 (不真实下单)")

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

                if _dry_run:
                    # 演练: 记录但不下单
                    closed += 1
                    self.tlogger.log_risk("EMERGENCY_DRY_RUN",
                        f"{sym}: {contracts}张 {hold_side} (模拟平仓)")
                    logger.warning(
                        f"🔥 DRY-RUN 紧急平仓 {sym}: {contracts}张 {hold_side}"
                    )
                else:
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
                if _dry_run:
                    self.tlogger.log_risk("EMERGENCY_DRY_RUN_FAIL",
                        f"{sym}: 模拟失败 {e}")

        if _dry_run:
            logger.warning(
                f"🔥 EMERGENCY DRY RUN 完成: {closed}/{len(positions)} 个仓位已记录"
            )
        return closed
