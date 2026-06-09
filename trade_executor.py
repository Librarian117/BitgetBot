#!/usr/bin/env python3
"""trade_executor.py — 仓位计算 → 杠杆设置 → 止盈止损计算 → 下单"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, TYPE_CHECKING

from config_manager import ConfigManager
from exchange_interface import ExchangeInterface
from session_manager import SessionManager

if TYPE_CHECKING:
    from deepseek_quant_bot import TradeLogger

logger = logging.getLogger("QuantBot")

class TradeExecutor:
    """封装：仓位计算 → 杠杆设置 → 止盈止损计算 → 下单 (v2: 全面增强)"""

    def __init__(self, config: ConfigManager, exchange: ExchangeInterface, tlogger: TradeLogger):
        self.config = config
        self.exchange = exchange
        self.logger = tlogger
        # ── v3.0: 安全层引用（由 DeepSeekQuantBot 在加载 safety 后设置） ──
        self.safety: Any = None
        self._skip_safety: bool = True  # 默认跳过安全校验，由 bot 启用
        # ── v4.4: R:R 影子模式存储 — 开仓时记录, 平仓时回填 RR_OUTCOME ──
        self._rr_shadow_store: Dict[str, dict] = {}

    # v4.0: 市场状态 → 仓位/SL 乘数
    REGIME_MULTIPLIERS = {
        "strong_trend":          {"pos": 1.20, "sl": 1.00, "tp": 1.10},
        "weak_trend":            {"pos": 1.00, "sl": 1.00, "tp": 1.00},
        "ranging":               {"pos": 0.70, "sl": 0.80, "tp": 1.20},
        "low_vol_ranging":       {"pos": 0.80, "sl": 0.70, "tp": 1.30},
        "high_vol_strong_trend": {"pos": 0.80, "sl": 1.30, "tp": 0.80},
        "low_vol_weak_trend":    {"pos": 1.00, "sl": 0.85, "tp": 1.15},
        "conflicting":           {"pos": 0.50, "sl": 1.00, "tp": 1.00},
    }

    def execute(self, symbol: str, direction: str, price: float,
                atr: float, adx: float = 25.0,
                strategy: str = "pullback",
                position_multiplier: float = 1.0,
                market_regime: str = "unknown",
                vol_cone_sl_mult: float = 1.0) -> Dict[str, Any]:
        """
        执行完整交易流程 (v4.0: 状态驱动仓位/SL/TP)。

        v4.0: market_regime - 市场状态标签，控制仓位乘数和 SL/TP 宽度
        v4.0: vol_cone_sl_mult - 波动率锥动态SL倍率
        v2.0: position_multiplier - 组合排名仓位系数
        """
        result = {
            "success": False, "order_id": None,
            "sl": 0.0, "tp_info": [],
            "amount": 0, "entry_fee": 0.0,
            "total_fee": 0.0, "margin_ratio_used": 0.0,
        }

        # ── 1. 保证金比例 (v3.2: 联动config调参) ──
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)
        if strategy == "momentum":
            margin_ratio = self.config.momentum_margin_ratio * session["margin_mult"]
        else:
            s_margin_min = self.config.adx_margin_min * session["margin_mult"]
            s_margin_max = self.config.adx_margin_max * session["margin_mult"]
            adx_clamped = max(20.0, min(40.0, adx))
            adx_ratio = (adx_clamped - 20.0) / (40.0 - 20.0)
            margin_ratio = s_margin_min + adx_ratio * (s_margin_max - s_margin_min)
        result["margin_ratio_used"] = margin_ratio

        # ── 2. 仓位计算 ──
        balance = self.exchange.fetch_usdt_balance()
        if balance <= 0:
            logger.warning("⚠️  余额为 0，跳过下单")
            return result

        # ── v3.6: Kelly 仓位系数 ──
        kelly_mult = 1.0
        try:
            if self.safety is not None and not self.safety._skip_safety:
                # 实盘模式: 从自学习引擎获取 Kelly 系数
                # 通过 config 的 kelly_multiplier 属性获取 (由 learner 更新)
                kelly_mult = getattr(self.config, 'kelly_multiplier', 1.0)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
        # v4.0: 市场状态仓位/SL 调整
        regime_cfg = self.REGIME_MULTIPLIERS.get(market_regime, {"pos": 1.0, "sl": 1.0, "tp": 1.0})
        regime_pos_mult = regime_cfg.get("pos", 1.0)
        regime_sl_mult = regime_cfg.get("sl", 1.0)
        regime_tp_mult = regime_cfg.get("tp", 1.0)

        # v4.0: ADX 动态 TP — 强趋势放远止盈让利润跑, 弱趋势收紧早锁利
        if adx > 35:
            adx_tp_mult = 1.5
        elif adx > 25:
            adx_tp_mult = 1.2
        elif adx < 15:
            adx_tp_mult = 0.6
        elif adx < 20:
            adx_tp_mult = 0.8
        else:
            adx_tp_mult = 1.0
        effective_tp_mult = regime_tp_mult * adx_tp_mult

        if regime_pos_mult != 1.0 or regime_sl_mult != 1.0 or adx_tp_mult != 1.0:
            logger.info(
                f"📊 状态驱动: {market_regime} → "
                f"仓位{regime_pos_mult:.1f}x SL{regime_sl_mult:.1f}x "
                f"TP{regime_tp_mult:.1f}x ADX-TP{adx_tp_mult:.1f}x"
            )

        margin = balance * margin_ratio * kelly_mult * position_multiplier * regime_pos_mult
        # v4.0: 单仓硬上限 8% → v4.1 降到 5% (防止单币反转造成大亏)
        MAX_MARGIN_PCT = 0.05
        if margin > balance * MAX_MARGIN_PCT:
            logger.warning(
                f"⚠️  仓位超限: {margin:.0f}U ({margin/balance*100:.1f}%) "
                f"→ 截断至 {balance*MAX_MARGIN_PCT:.0f}U ({MAX_MARGIN_PCT*100:.0f}%)"
            )
            margin = balance * MAX_MARGIN_PCT

        # v4.0: 总保证金上限 (防止多仓叠加耗尽余额)
        MAX_TOTAL_MARGIN_PCT = 0.50
        try:
            acct = self.exchange.get_account_summary()
            existing_margin = acct.get("used_margin", 0)
            if existing_margin + margin > balance * MAX_TOTAL_MARGIN_PCT:
                old_margin = margin
                margin = max(0, balance * MAX_TOTAL_MARGIN_PCT - existing_margin)
                if margin <= 0:
                    logger.error(
                        f"❌ 总保证金已超限: 已用{existing_margin:.0f}U "
                        f"+ 新仓{margin:.0f}U > {balance*MAX_TOTAL_MARGIN_PCT:.0f}U → 拒绝开仓"
                    )
                    result["success"] = False
                    return result
                logger.warning(
                    f"⚠️  总保证金超限: 已用{existing_margin:.0f}U "
                    f"+ 新仓{old_margin:.0f}U > {balance*MAX_TOTAL_MARGIN_PCT:.0f}U "
                    f"→ 缩减至{margin:.0f}U"
                )
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

        # v4.0: 先用默认杠杆，后续验证后可能修正
        effective_leverage = self.config.leverage
        position_value = margin * effective_leverage
        contract_size = self.exchange.get_contract_size(symbol)
        min_amount = self.exchange.get_min_amount(symbol)

        amount_contracts = position_value / (price * contract_size)
        # v3.2: 沙箱合约面值可能偏大（BTC contractSize=1），ceil 到最小单位
        if amount_contracts < min_amount:
            amount_contracts = min_amount  # 用最小可开张数
            # 重新计算实际所需保证金和仓位价值
            position_value = amount_contracts * price * contract_size
            margin = position_value / effective_leverage  # v4.0: 用实际杠杆
            # v4.0 fix: 用 8% 上限替代 50%, 防止 min_amount 撑破仓位上限
            if margin > balance * MAX_MARGIN_PCT:
                logger.warning(
                    f"⚠️  最少需要 {amount_contracts} 张, 保证金={margin:.0f}U "
                    f"({margin/balance*100:.1f}%) > {MAX_MARGIN_PCT*100:.0f}%上限, 余额={balance:.0f}U → 拒绝 {symbol}"
                )
                return result
            result["margin_ratio_used"] = margin / balance
            logger.info(
                f"📐 {symbol} 仓位调整至最小: 张数={amount_contracts} "
                f"保证金={margin:.2f} 仓位={position_value:.2f}"
            )
        else:
            amount_contracts = math.floor(amount_contracts)
            # 如果 floor 归零但原始值 > 0，至少用 min_amount
            if amount_contracts == 0 and min_amount > 0:
                amount_contracts = min_amount
                position_value = amount_contracts * price * contract_size
                margin = position_value / effective_leverage  # v4.0: 用实际杠杆
                # v4.0 fix: 用 8% 上限替代 50%
                if margin > balance * MAX_MARGIN_PCT:
                    logger.warning(
                        f"⚠️  floor归零, 最少{min_amount}张需保证金={margin:.0f}U "
                        f"({margin/balance*100:.1f}%) > {MAX_MARGIN_PCT*100:.0f}%上限 → 拒绝 {symbol}"
                    )
                    return result
                result["margin_ratio_used"] = margin / balance
                logger.info(
                    f"📐 {symbol} floor归零, 调整为最小张数={amount_contracts} "
                    f"保证金={margin:.2f}"
                )

        result["amount"] = amount_contracts

        # ── 3. 手续费估算 ──
        taker_fee = self.exchange.get_taker_fee(symbol)
        entry_fee = position_value * taker_fee
        # 平仓费预估 (SL或TP触发，均为taker；最坏情况2次成交)
        exit_fee = position_value * taker_fee * len(self.config.tp_split_ratios)
        total_estimated_fee = entry_fee + exit_fee
        result["entry_fee"] = entry_fee
        result["total_fee"] = total_estimated_fee

        logger.info(
            f"📐 {symbol} 仓位计算: 余额={balance:.2f} | "
            f"ADX={adx:.2f} → 保证金率={margin_ratio*100:.1f}% | "
            f"保证金={margin:.2f} | 仓位价值={position_value:.2f} | "
            f"张数={amount_contracts} | "
            f"预估总手续费={total_estimated_fee:.4f} USDT"
        )

        # ── v4.4: 最小风险回报比检查 (影子模式 — 只记录不拦截) ──
        _rr_ratio = 0.0
        _rr_expected_profit = 0.0
        if not self._skip_safety or self.config.sandbox_safety:
            tp1_mult = self.config.tp_atr_mults[0] if self.config.tp_atr_mults else 2.0
            tp_dist = tp1_mult * atr * effective_tp_mult
            _rr_expected_profit = tp_dist * amount_contracts * contract_size
            total_fee_cost = total_estimated_fee
            if total_fee_cost > 0 and _rr_expected_profit > 0:
                _rr_ratio = _rr_expected_profit / total_fee_cost
                if _rr_ratio < self.config.min_rr_ratio:
                    # 影子模式: 结构化记录, 不拒绝 (48h后数据驱动校准阈值)
                    logger.warning(
                        f"👻 {symbol} R:R影子: {expected_profit:.4f}/{total_fee_cost:.4f}"
                        f" = {rr_ratio:.1f}x < {self.config.min_rr_ratio}x (放行)"
                    )
                    self.logger.log_rr_shadow(
                        symbol=symbol.split(':')[0].split('/')[0],
                        direction=direction, strategy=strategy,
                        rr_ratio=_rr_ratio,
                        expected_profit=_rr_expected_profit,
                        fee=total_fee_cost,
                        confidence=0,  # 由调用方回填
                        order_id="",   # 下单后回填
                    )
                else:
                    logger.debug(
                        f"📊 {symbol} R:R通过: {expected_profit:.4f}/{total_fee_cost:.4f}"
                        f" = {rr_ratio:.1f}x >= {self.config.min_rr_ratio}x"
                    )

        # ── v3.0: 实盘安全检查 (v4.4: 沙箱也启用) ──
        if (not self._skip_safety or self.config.sandbox_safety) and self.safety:
            # 最小仓位
            ok, reason = self.safety.check_min_position_value(position_value, symbol)
            if not ok:
                logger.warning(f"⛔ 安全校验未通过 {symbol}: {reason}")
                return result
            # 费后利润
            tp1_mult = self.config.tp_atr_mults[0] if self.config.tp_atr_mults else 2.0
            ok, reason = self.safety.check_profit_after_fees(
                position_value, atr, taker_fee, tp1_mult, price
            )
            if not ok:
                logger.warning(f"⛔ 利润不足 {symbol}: {reason}")
                return result

        # ── 4. 设置杠杆 + 验证 ──
        self.exchange.set_leverage(symbol, self.config.leverage)
        # v4.0: 验证杠杆是否生效 (沙箱可能拒绝, 默认只有10x)
        effective_leverage = self.config.leverage
        try:
            pos_check = self.exchange.fetch_position(symbol)
            actual_lev = float(pos_check.get("leverage") or pos_check.get("info", {}).get("leverage", 0) or 0)
            if actual_lev > 0 and actual_lev != self.config.leverage:
                logger.warning(
                    f"⚠️  杠杆设置失败: 请求{self.config.leverage}x → 实际{actual_lev:.0f}x"
                )
                effective_leverage = actual_lev
                # v4.0 fix: 杠杆变了，重算仓位价值和张数
                position_value = margin * effective_leverage
                amount_contracts = position_value / (price * contract_size)
                if amount_contracts < min_amount:
                    amount_contracts = min_amount
                    position_value = amount_contracts * price * contract_size
                    margin = position_value / effective_leverage
                else:
                    amount_contracts = math.floor(amount_contracts)
                    if amount_contracts == 0 and min_amount > 0:
                        amount_contracts = min_amount
                        position_value = amount_contracts * price * contract_size
                        margin = position_value / effective_leverage
                if margin > balance * MAX_MARGIN_PCT:
                    margin = balance * MAX_MARGIN_PCT
                    position_value = margin * effective_leverage
                    amount_contracts = max(min_amount, math.floor(position_value / (price * contract_size)))
                # v4.0 fix: 用最终合约数反算真实保证金，防止 min_amount 撑破上限
                real_margin = amount_contracts * price * contract_size / effective_leverage
                if real_margin > balance * MAX_MARGIN_PCT:
                    logger.warning(
                        f"⚠️  杠杆修正后保证金{real_margin:.0f}U({real_margin/balance*100:.1f}%) > "
                        f"{MAX_MARGIN_PCT*100:.0f}%上限, min={min_amount}张 — 拒绝开仓"
                    )
                    return result
                margin = real_margin
                result["amount"] = amount_contracts
                result["margin_ratio_used"] = margin / balance if balance > 0 else 0
                logger.info(
                    f"📐 杠杆修正重算: margin={margin:.2f} position_value={position_value:.2f} "
                    f"contracts={amount_contracts} lev={effective_leverage}x"
                )
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

        # ── 5. 止损 & 部分止盈计算 ──
        if strategy == "momentum":
            sl_dist = self.config.momentum_sl_mult * atr
        else:
            sl_dist = self.config.sl_atr_mult * session["sl_mult"] * atr
        # v4.0: 市场状态 SL 宽度调整
        sl_dist *= regime_sl_mult
        # v4.0: 波动率锥动态SL — 极端波动收紧, 低波动放宽
        sl_dist *= vol_cone_sl_mult
        # v4.0: SL 宽度硬上限 (防止多乘数叠加后 SL 过宽, 如 4.29x ATR)
        max_sl_dist = price * 0.15  # 最大 15% 价格
        min_sl_dist = price * 0.005  # 最小 0.5% (防止过于紧)
        if sl_dist > max_sl_dist:
            logger.warning(f"⚠️  SL距离{sl_dist/price*100:.1f}% > 15% → 截断")
            sl_dist = max_sl_dist
        elif sl_dist < min_sl_dist:
            sl_dist = min_sl_dist
        # 滑点缓冲: SL 向不利方向偏移
        slippage = price * self.config.slippage_buffer

        if direction == "LONG":
            side = "buy"
            sl_price = price - sl_dist - slippage
        else:
            side = "sell"
            sl_price = price + sl_dist + slippage

        if sl_price <= 0:
            logger.error(f"❌ SL 价格异常: {sl_price:.4f}")
            return result

        result["sl"] = sl_price

        # 部分止盈价格 (v4.0: 市场状态缩放TP)
        tp_parts = []
        for i, (ratio, mult) in enumerate(
            zip(self.config.tp_split_ratios, self.config.tp_atr_mults)
        ):
            tp_dist = mult * atr * effective_tp_mult  # v4.0: 趋势+ADX双因子缩放TP
            if direction == "LONG":
                tp_price = price + tp_dist
            else:
                tp_price = price - tp_dist
            if tp_price > 0:
                tp_parts.append((tp_price, ratio))
                logger.info(
                    f"📏  TP{i+1}: {tp_price:.4f} ({ratio*100:.0f}%) @ {mult}×ATR"
                )

        result["tp_info"] = [
            {"price": round(p, 4), "ratio": r} for p, r in tp_parts
        ]

        logger.info(
            f"📏 {symbol} {direction}: 入场≈{price:.4f} | "
            f"SL={sl_price:.4f} (距离={sl_dist:.4f} + 滑点={slippage:.4f}) | "
            f"盈亏比≈1:{self.config.tp_atr_mults[-1]/self.config.sl_atr_mult:.1f} | "
            f"预估手续费={total_estimated_fee:.4f}"
        )

        # ── 6. 下单 (部分止盈) ──
        # ── v3.0: 带限价后备的市价单 (v4.4: 沙箱也启用) ──
        if (not self._skip_safety or self.config.sandbox_safety) and self.safety and self.config.limit_fallback_enabled:
            order = self.safety.place_with_limit_fallback(
                symbol, side, amount_contracts, sl_price, tp_parts
            )
        else:
            order = self.exchange.create_market_order_with_partial_tp(
                symbol=symbol,
                side=side,
                amount=amount_contracts,
                sl_price=sl_price,
                tp_parts=tp_parts,
            )

        if order:
            # ── v3.0: 订单成交确认 (v4.4: 沙箱也启用) ──
            if (not self._skip_safety or self.config.sandbox_safety) and self.safety and self.config.order_confirmation_enabled:
                confirmed, _ = self.safety.confirm_order_fill(
                    str(order.get("id", "")), symbol,
                    self.config.order_confirmation_timeout
                )
                if not confirmed:
                    logger.error(f"⛔ 订单 {order.get('id')} 未确认成交，标记为失败")
                    result["order_id"] = str(order.get("id", "N/A"))
                    return result

            result["success"] = True
            result["order_id"] = str(order.get("id", "N/A"))
            # v4.4: 回传 RR 数据到结果, 供 ENTRY_SNAPSHOT 持久化 (跨重启安全)
            result["rr_ratio"] = _rr_ratio
            result["rr_expected_profit"] = _rr_expected_profit
            # v4.4: 内存存储 RR 数据, 供平仓时回填 RR_OUTCOME (重启后丢失但可从日志恢复)
            symbol_short = symbol.split(':')[0].split('/')[0]
            self._rr_shadow_store[symbol_short + '_' + direction] = {
                'rr_ratio': _rr_ratio,
                'expected_profit': _rr_expected_profit,
                'strategy': strategy,
            }
            self.logger.log_trade(
                symbol=symbol, direction=direction, price=price,
                sl=sl_price, tp_info=result["tp_info"],
                amount=amount_contracts,
                order_id=result["order_id"], success=True,
                entry_fee=entry_fee, total_estimated_fee=total_estimated_fee,
                margin_ratio_used=margin_ratio,
            )
        else:
            self.logger.log_trade(
                symbol=symbol, direction=direction, price=price,
                sl=sl_price, tp_info=result["tp_info"],
                amount=amount_contracts,
                order_id=None, success=False,
                entry_fee=entry_fee, total_estimated_fee=total_estimated_fee,
                margin_ratio_used=margin_ratio,
            )

        return result

    def pop_rr_shadow(self, symbol: str, direction: str) -> dict:
        """v4.4: 平仓时取出 RR 影子数据并发射 RR_OUTCOME"""
        key = symbol + '_' + direction
        data = self._rr_shadow_store.pop(key, None)
        if data and data.get('rr_ratio', 0) > 0:
            self.logger.log_rr_outcome(
                symbol=symbol, direction=direction,
                rr_ratio=data['rr_ratio'],
                actual_pnl=0,  # 由调用方回填
                exit_reason="",
            )
        return data or {}


# ============================================================================
# 9. DeepSeekQuantBot — 主循环编排器 (v2: 全面升级)
# ============================================================================
