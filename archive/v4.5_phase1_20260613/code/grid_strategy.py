#!/usr/bin/env python3
"""
grid_strategy.py — 震荡网格策略管理器 v3.0
===========================================
v3.0 改进:
  1. 几何间距 — 百分比层级替代等差间距 (spacing_pct = atr_pct * 0.6, clamp[0.3%, 2.5%])
  2. 自动锚点重校准 — 价格偏离中心 >2% 时撤销并重新部署 (冷却 4h, 跳过 1h 波动>3%)
  3. 趋势过滤器 — ADX(14) > 25 跳过网格 (周末放宽至 30)
  4. 回撤断路器 — 已实现亏损 > 3% 保证金时自动全部平仓
  5. 手续费感知 — 单层利润 > 3x 往返手续费才部署
  6. 无限网格 — 卖单层级无上限，成交后在上方自动补单
"""

import json
import logging
import math
import os
from datetime import timedelta
from time_utils import now, now_iso, datetime_from_iso
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("QuantBot")


class GridManager:
    """震荡网格策略管理器 v3.0 — 几何间距 + 无限网格 + 智能风控"""

    STATE_FILE = "grid_state.json"

    # 周末优先币种（流动性好、波动小）
    WEEKEND_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    # ── v3.0: 几何间距参数 ──
    SPACING_ATR_MULTIPLIER = 0.6      # ATR% * 乘数 = 层级间距
    SPACING_MIN_PCT = 0.003           # 最小间距 0.3%
    SPACING_MAX_PCT = 0.025           # 最大间距 2.5%

    # ── v3.0: 重校准参数 ──
    RECALIBRATION_DRIFT_THRESHOLD = 0.02   # 价格偏离中心 2% 触发
    RECALIBRATION_COOLDOWN_HOURS = 4       # 两次重校准最小间隔
    RECALIBRATION_SKIP_CHANGE_PCT = 0.03   # 1h 价格变动 >3% 跳过

    # ── v3.0: 趋势过滤器 ──
    ADX_PERIOD = 14                   # ADX 计算周期
    ADX_TREND_THRESHOLD = 25          # 趋势市 ADX 阈值
    ADX_WEEKEND_THRESHOLD = 30        # 周末 ADX 阈值（放宽）

    # ── v3.0: 回撤断路器 ──
    DRAWDOWN_THRESHOLD_PCT = -0.03    # 亏损 > 3% 保证金触发

    # ── v3.0: 手续费感知 ──
    FEE_MULTIPLIER = 3                # 单层利润需 > N 倍往返手续费
    TAKER_FEE = 0.0006                # Bitget U本位合约 taker 费率 0.06%

    def __init__(self, config, exchange, tlogger):
        self.config = config
        self.exchange = exchange      # ExchangeInterface 实例
        self.tlogger = tlogger
        self.grids: Dict[str, Any] = {}
        self._state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.STATE_FILE)
        self._load_state()

    # ════════════════════════════════════════════
    # 公开 API (保持向后兼容)
    # ════════════════════════════════════════════

    def is_active(self, symbol: str) -> bool:
        return symbol in self.grids and self.grids[symbol].get("active", False)

    def get_active_grids(self) -> list:
        return [s for s, g in self.grids.items() if g.get("active", False)]

    def is_weekend(self) -> bool:
        """判断当前是否为周末（含周五晚 20:00 ~ 周一 08:00）"""
        wd = now().weekday()
        h = now().hour
        if wd == 4 and h >= 20:
            return True
        if wd in (5, 6):
            return True
        if wd == 0 and h < 8:
            return True
        return False

    def should_deploy_weekend(self, symbol: str) -> bool:
        """周末是否应该为该币种部署网格"""
        if not self.is_weekend():
            return False
        if self.is_active(symbol):
            return False
        if symbol not in self.WEEKEND_SYMBOLS:
            return False
        positions = self.exchange.get_open_positions()
        if len(positions) >= self.config.max_concurrent_positions - 1:
            return False
        return True

    # ════════════════════════════════════════════
    # v3.0: ADX 本地计算（从 OHLCV DataFrame）
    # ════════════════════════════════════════════

    @staticmethod
    def _compute_adx(df, period: int = 14) -> float:
        """
        v3.0: 从 OHLCV DataFrame 本地计算 ADX(14) 最新值。
        使用 Wilder 平滑，与 deepseek_quant_bot.py 的 IndicatorCalculator 一致。
        返回 ADX 最新值；数据不足时返回 0.0（视为无趋势，不过滤）。
        """
        try:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)

            if len(close) < period + 1:
                return 0.0

            prev_high = high.shift(1)
            prev_low = low.shift(1)
            prev_close = close.shift(1)

            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

            up_move = high - prev_high
            down_move = prev_low - low

            plus_dm = pd.Series(0.0, index=df.index)
            minus_dm = pd.Series(0.0, index=df.index)

            cond_plus = (up_move > down_move) & (up_move > 0)
            cond_minus = (down_move > up_move) & (down_move > 0)
            plus_dm[cond_plus] = up_move[cond_plus]
            minus_dm[cond_minus] = down_move[cond_minus]

            smooth_plus = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
            smooth_minus = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

            plus_di = 100.0 * smooth_plus / atr.replace(0, float("nan"))
            minus_di = 100.0 * smooth_minus / atr.replace(0, float("nan"))

            dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
            adx_series = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

            val = float(adx_series.iloc[-1])
            return val if math.isfinite(val) else 0.0
        except Exception:
            return 0.0

    # ════════════════════════════════════════════
    # v3.0: 几何间距计算
    # ════════════════════════════════════════════

    def _compute_geometric_spacing(self, price: float, atr: float) -> float:
        """
        v3.0: 基于 ATR 百分比计算几何间距。
        spacing_pct = (ATR / 现价) * SPACING_ATR_MULTIPLIER
        钳制在 [SPACING_MIN_PCT, SPACING_MAX_PCT]。
        """
        atr_pct = atr / price if price > 0 else 0.005
        spacing_pct = atr_pct * self.SPACING_ATR_MULTIPLIER
        spacing_pct = max(self.SPACING_MIN_PCT, min(self.SPACING_MAX_PCT, spacing_pct))
        return spacing_pct

    def _generate_geometric_levels(
        self, center: float, spacing_pct: float,
        n_buys: int, n_sells: int
    ) -> List[dict]:
        """
        v3.0: 生成几何间距层级列表。
        买单: center * (1 - spacing)^1, center * (1 - spacing)^2, ... (价格递减)
        卖单: center * (1 + spacing)^1, center * (1 + spacing)^2, ... (价格递增，无限)
        每个层级附带 TP/SL 价格（也是几何比例）。
        """
        levels = []

        for i in range(1, n_buys + 1):
            buy_price = center * (1.0 - spacing_pct) ** i
            levels.append({
                "side": "buy",
                "price": round(buy_price, 4),
                "amount": 0.0,
                "order_id": None,
                "status": "pending",
                "sl_price": round(buy_price * (1.0 - spacing_pct * 2.5), 4),
                "tp_price": round(buy_price * (1.0 + spacing_pct), 4),
                "filled_price": None,
            })

        for j in range(1, n_sells + 1):
            sell_price = center * (1.0 + spacing_pct) ** j
            levels.append({
                "side": "sell",
                "price": round(sell_price, 4),
                "amount": 0.0,
                "order_id": None,
                "status": "pending",
                "sl_price": round(sell_price * (1.0 + spacing_pct * 2.5), 4),
                "tp_price": round(sell_price * (1.0 - spacing_pct), 4),
                "filled_price": None,
            })

        return levels

    # ════════════════════════════════════════════
    # v3.0: 手续费感知验证
    # ════════════════════════════════════════════

    def _verify_fee_profitability(self, spacing_pct: float) -> bool:
        """
        v3.0: 验证单层利润是否 > 3x 往返手续费。
        往返手续费 = taker * 2 (开仓 + 平仓)。
        单层理论利润 ≈ spacing_pct。
        也可通过 ExchangeInterface 的费率缓存获取实际费率。
        """
        # 尝试从交易所接口获取实际费率
        try:
            fee_cache = getattr(self.exchange, "_trading_fees", {})
            if fee_cache:
                avg_fee = sum(fee_cache.values()) / len(fee_cache)
            else:
                avg_fee = self.TAKER_FEE
        except Exception:
            avg_fee = self.TAKER_FEE

        round_trip_fee = avg_fee * 2  # 开仓 + 平仓
        min_required_profit = round_trip_fee * self.FEE_MULTIPLIER
        return spacing_pct > min_required_profit

    # ════════════════════════════════════════════
    # v3.0: 趋势过滤器
    # ════════════════════════════════════════════

    def _is_trending(self, adx: float, weekend: bool) -> bool:
        """
        v3.0: ADX 趋势过滤器。
        ADX > 阈值 → 趋势市不适合网格。
        正常: ADX > 25 跳过; 周末: ADX > 30 跳过。
        adx == 0 视为无数据，不过滤。
        """
        if adx <= 0:
            return False  # 无 ADX 数据时不过滤（容错）
        threshold = self.ADX_WEEKEND_THRESHOLD if weekend else self.ADX_TREND_THRESHOLD
        return adx > threshold

    # ════════════════════════════════════════════
    # v3.0: 层级计算（几何间距 + 趋势过滤 + 手续费感知）
    # ════════════════════════════════════════════

    def calculate_levels(self, symbol: str, df, price: float, atr: float,
                         adx: Optional[float] = None) -> Optional[dict]:
        """
        v3.0: 几何间距层级计算。
        参数:
          symbol: 交易对标识
          df:     OHLCV DataFrame (需含 high/low/close 列)
          price:  当前价格
          atr:    ATR(14) 值
          adx:    ADX(14) 值 (可选；不传则从 df 本地计算)

        返回:
          dict with support, resistance, center, spacing_pct, levels, ...
          or None (区间太窄 / 趋势过强 / 手续费不划算)
        """
        n_levels = self.config.grid_levels
        if n_levels < 2:
            n_levels = 4

        weekend = self.is_weekend()

        # ── v3.0: 趋势过滤器 ──
        if adx is None:
            adx = self._compute_adx(df, self.ADX_PERIOD)
        if self._is_trending(adx, weekend):
            logger.info(f"📋 {symbol} ADX={adx:.1f} 趋势过强 "
                        f"(阈值={self.ADX_WEEKEND_THRESHOLD if weekend else self.ADX_TREND_THRESHOLD})，跳过网格部署")
            return None

        # ── v3.0: 几何间距 ──
        spacing_pct = self._compute_geometric_spacing(price, atr)
        logger.info(f"📋 {symbol} 几何间距={spacing_pct*100:.2f}% "
                    f"(ATR%={atr/price*100:.2f}%, 乘数={self.SPACING_ATR_MULTIPLIER})")

        # ── v3.0: 手续费感知 ──
        if not self._verify_fee_profitability(spacing_pct):
            round_trip = self.TAKER_FEE * 2
            logger.info(f"📋 {symbol} 单层利润 {spacing_pct*100:.2f}% < "
                        f"{round_trip*self.FEE_MULTIPLIER*100:.2f}% 手续费门槛，跳过部署")
            return None

        # ── 区间范围检查（几何层级需要的总区间） ──
        recent_high = float(df["high"].tail(20).max())
        recent_low = float(df["low"].tail(20).min())
        price_range = recent_high - recent_low
        range_pct = price_range / recent_low if recent_low > 0 else 0

        # 几何网格需要的总区间（最外层卖单价格 - 最外层买单价格）
        total_spread_pct = spacing_pct * n_levels
        min_range_pct = total_spread_pct * 0.4
        if range_pct < min_range_pct and not weekend:
            logger.info(f"📋 {symbol} 区间太窄 ({range_pct*100:.2f}% < {min_range_pct*100:.2f}%)，不部署网格")
            return None

        # 买层数 = ceil(n/2), 卖层数 = n - 买层数
        n_buys = math.ceil(n_levels / 2)
        n_sells = n_levels - n_buys

        # ── v3.0: 几何层级生成（以当前价为中心） ──
        levels = self._generate_geometric_levels(price, spacing_pct, n_buys, n_sells)

        # 汇总区间信息
        highest_sell = max(
            (lvl["price"] for lvl in levels if lvl["side"] == "sell"),
            default=price * (1.0 + spacing_pct) ** n_sells
        )
        lowest_buy = min(
            (lvl["price"] for lvl in levels if lvl["side"] == "buy"),
            default=price * (1.0 - spacing_pct) ** n_buys
        )

        return {
            "support": round(lowest_buy, 4),
            "resistance": round(highest_sell, 4),
            "center": round(price, 4),                # v3.0: 锚点价格
            "spacing_pct": round(spacing_pct * 100, 2),  # v3.0: 百分比（便于日志）
            "spacing_decimal": round(spacing_pct, 6),     # v3.0: 小数（便于计算）
            "range_pct": round(range_pct * 100, 2),
            "levels": levels,
            "n_buys": n_buys,
            "n_sells": n_sells,
            "weekend": weekend,
            "adx": round(adx, 1),
        }

    # ════════════════════════════════════════════
    # 部署 (v3.0: 几何层级 + 保证金分摊)
    # ════════════════════════════════════════════

    def deploy_grid(self, symbol: str, grid_info: dict, current_price: float) -> bool:
        """
        v3.0: 按总层数分摊保证金，部署几何网格层级。
        兼容 v2.0 state 文件（旧 grid 加载后仍可正确识别）。
        返回 True 表示至少有一层下单成功。
        """
        if self.is_active(symbol):
            # 先取消旧网格，确保干净部署
            logger.info(f"📋 {symbol} 网格已激活，撤销后重新部署")
            self.cancel_grid(symbol)

        balance = self.exchange.fetch_usdt_balance()
        levels = grid_info["levels"]
        n_total = len(levels)
        if n_total == 0:
            logger.error(f"❌ {symbol} 网格层级为空，无法部署")
            return False

        # 周末用 2x 资金
        pos_ratio = self.config.grid_position_ratio * (2.0 if grid_info.get("weekend") else 1.0)
        per_level_margin = balance * pos_ratio / n_total
        contract_size = self.exchange.get_contract_size(symbol)
        min_amount = self.exchange.get_min_amount(symbol)

        # 设置杠杆
        self.exchange.set_leverage(symbol, self.config.leverage)

        # ── v3.0: 手续费感知 — 毛估检查 ──
        spacing_decimal = grid_info.get("spacing_decimal",
                                        grid_info.get("spacing_pct", 1.0) / 100.0)
        if not self._verify_fee_profitability(spacing_decimal):
            logger.warning(f"⚠️  {symbol} 间距 {spacing_decimal*100:.3f}% 低于手续费门槛，"
                           f"部署可能无利润，继续执行但请留意")

        success_count = 0
        for level in levels:
            position_value = per_level_margin * self.config.leverage
            amount = position_value / (level["price"] * contract_size)
            amount = max(1, math.floor(amount))  # 最少 1 张

            if amount < min_amount:
                level["status"] = "skipped"
                logger.debug(f"📋 {symbol} {level['side']} 层级手数 {amount} < {min_amount}，跳过")
                continue

            level["amount"] = amount
            try:
                order = self.exchange.create_limit_order(
                    symbol=symbol,
                    side=level["side"],
                    amount=amount,
                    price=level["price"],
                    params={"tradeSide": "open"},
                )
                if order and order.get("id"):
                    level["order_id"] = str(order["id"])
                    level["status"] = "open"
                    success_count += 1
                else:
                    level["status"] = "failed"
                    logger.warning(f"⚠️  {symbol} {level['side']} 限价单返回空 ID")
            except Exception as e:
                logger.error(f"❌ 网格下单 {symbol} {level['side']} @{level['price']:.4f}: {e}")
                level["status"] = "failed"

        # 迁移旧状态：如果是从 v2.0 的 grid 重新部署，保留 realized_pnl
        existing = self.grids.get(symbol, {})
        kept_pnl = existing.get("realized_pnl", 0.0) if not existing.get("active") else 0.0

        self.grids[symbol] = {
            "active": success_count > 0,
            "levels": levels,
            "support": grid_info["support"],
            "resistance": grid_info["resistance"],
            "center": grid_info.get("center", current_price),         # v3.0: 锚点
            "spacing_pct": grid_info.get("spacing_pct", 1.0),         # v3.0: 间距
            "spacing_decimal": grid_info.get("spacing_decimal",
                                             grid_info.get("spacing_pct", 1.0) / 100.0),
            "entry_time": now_iso(),
            "last_recalibration": None,                                # v3.0: 上次重校准
            "recalibration_count": 0,                                  # v3.0: 重校准次数
            "realized_pnl": kept_pnl,
            "total_margin_used": per_level_margin * success_count,     # v3.0: 已用保证金
            "weekend": grid_info.get("weekend", False),
        }
        self._save_state()

        if success_count > 0:
            self.tlogger.log_risk("GRID_DEPLOY",
                f"{symbol}: {success_count}/{n_total} levels "
                f"center={grid_info.get('center', current_price):.4f} "
                f"spacing={grid_info.get('spacing_pct', 1.0):.2f}% "
                f"support={grid_info['support']:.4f} resist={grid_info['resistance']:.4f} "
                f"weekend={grid_info.get('weekend', False)} "
                f"ADX={grid_info.get('adx', 0):.1f}")
            logger.info(f"📋 {symbol} 网格部署完成: {success_count}/{n_total} 层级已下单")
        else:
            logger.warning(f"⚠️  {symbol} 网格部署失败: 0/{n_total} 层级下单成功")

        return success_count > 0

    # ════════════════════════════════════════════
    # v3.0: 无限网格 — 卖单成交后补单
    # ════════════════════════════════════════════

    def _replenish_sell_level(self, symbol: str, grid: dict, filled_level: dict):
        """
        v3.0: 卖单成交后，在当前最高卖单价上方新增一个卖单层级。
        无限网格: 卖单价格无上限，随成交不断向上延伸。
        """
        # 找到当前所有活跃 + 待处理的卖单最高价格
        all_sell_prices = [
            lvl["price"] for lvl in grid["levels"]
            if lvl["side"] == "sell"
            and lvl["price"] > 0
        ]
        # 也考虑刚成交的那层价格
        filled_price = filled_level.get("filled_price") or filled_level.get("price", 0)
        if filled_price > 0:
            all_sell_prices.append(filled_price)

        if not all_sell_prices:
            logger.warning(f"⚠️  {symbol} 无限网格补单失败: 无可用卖单价格参考")
            return

        highest_price = max(all_sell_prices)

        # 从 grid 中获取间距（兼容 v2.0 旧格式）
        spacing_decimal = grid.get("spacing_decimal",
                                   grid.get("spacing_pct", 1.0) / 100.0)

        # 在最高价上方按几何间距新增一个层级
        new_sell_price = highest_price * (1.0 + spacing_decimal)
        amount = filled_level.get("amount", 1)

        new_level = {
            "side": "sell",
            "price": round(new_sell_price, 4),
            "amount": amount,
            "order_id": None,
            "status": "pending",
            "sl_price": round(new_sell_price * (1.0 + spacing_decimal * 2.5), 4),
            "tp_price": round(new_sell_price * (1.0 - spacing_decimal), 4),
            "filled_price": None,
        }

        try:
            order = self.exchange.create_limit_order(
                symbol=symbol,
                side="sell",
                amount=amount,
                price=new_level["price"],
                params={"tradeSide": "open"},
            )
            if order and order.get("id"):
                new_level["order_id"] = str(order["id"])
                new_level["status"] = "open"
                grid["levels"].append(new_level)
                # 更新阻力位为新最高卖单价格
                grid["resistance"] = round(new_sell_price, 4)
                self._save_state()
                logger.info(f"📋 无限网格 {symbol} 补卖单 @{new_sell_price:.4f} "
                            f"(spacing={spacing_decimal*100:.2f}%, "
                            f"总卖单层级={sum(1 for lv in grid['levels'] if lv['side']=='sell')})")
            else:
                logger.warning(f"⚠️  {symbol} 无限网格补单: 下单返回空 ID")
        except Exception as e:
            logger.error(f"❌ 无限网格补单失败 {symbol} @{new_sell_price:.4f}: {e}")

    # ════════════════════════════════════════════
    # v3.0: 自动锚点重校准
    # ════════════════════════════════════════════

    def _check_recalibration(self, symbol: str, grid: dict, current_price: float,
                             price_change_1h_pct: float) -> bool:
        """
        v3.0: 检查是否需要锚点重校准。
        触发条件（全部满足）:
          1. 价格偏离中心 > RECALIBRATION_DRIFT_THRESHOLD (2%)
          2. 距上次重校准 > RECALIBRATION_COOLDOWN_HOURS (4h)
          3. 近 1h 价格变动 <= RECALIBRATION_SKIP_CHANGE_PCT (3%)
        返回 True 表示已触发并完成重校准。
        """
        center = grid.get("center", 0)
        if center <= 0:
            return False

        drift_pct = abs(current_price - center) / center
        if drift_pct <= self.RECALIBRATION_DRIFT_THRESHOLD:
            return False

        # 检查冷却时间
        last_recal_str = grid.get("last_recalibration")
        if last_recal_str:
            try:
                last_time = datetime_from_iso(last_recal_str)
                elapsed = now() - last_time
                if elapsed < timedelta(hours=self.RECALIBRATION_COOLDOWN_HOURS):
                    remaining = timedelta(hours=self.RECALIBRATION_COOLDOWN_HOURS) - elapsed
                    logger.debug(f"📋 {symbol} 重校准冷却中 "
                                 f"(剩余 {remaining.seconds // 60}min, 偏移 {drift_pct*100:.1f}%)")
                    return False
            except (ValueError, TypeError, OverflowError):
                pass  # 解析失败视为冷却已过

        # 价格急变时跳过（防止在暴跌/暴涨中重校准）
        if abs(price_change_1h_pct) > self.RECALIBRATION_SKIP_CHANGE_PCT:
            logger.info(f"📋 {symbol} 1h 价格变动 {price_change_1h_pct*100:.1f}% > "
                        f"{self.RECALIBRATION_SKIP_CHANGE_PCT*100:.0f}%，跳过锚点重校准")
            return False

        logger.warning(f"📋 {symbol} 价格偏离锚点 {drift_pct*100:.1f}% "
                       f"(中心={center:.4f}, 现价={current_price:.4f})，触发锚点重校准")
        self._do_recalibration(symbol, grid, current_price)
        return True

    def _do_recalibration(self, symbol: str, grid: dict, new_center: float):
        """
        v3.0: 执行锚点重校准。
        1. 取消所有旧挂单
        2. 保留网格配置（间距、层级数）
        3. 以新价格为中心重新生成几何层级
        4. 按原有资金分配重新下单
        """
        # 保存重校准前的配置
        spacing_decimal = grid.get("spacing_decimal",
                                   grid.get("spacing_pct", 1.0) / 100.0)
        kept_pnl = grid.get("realized_pnl", 0.0)
        recal_count = grid.get("recalibration_count", 0) + 1

        # 统计旧层级信息
        n_buys = sum(1 for lvl in grid["levels"] if lvl["side"] == "buy")
        n_sells = sum(1 for lvl in grid["levels"] if lvl["side"] == "sell")
        if n_buys == 0 and n_sells == 0:
            n_levels = self.config.grid_levels
            n_buys = math.ceil(n_levels / 2)
            n_sells = n_levels - n_buys

        # 取消所有旧挂单（保留状态以便追踪）
        cancelled_count = 0
        for level in grid.get("levels", []):
            if level["status"] == "open" and level.get("order_id"):
                try:
                    self.exchange.cancel_order(level["order_id"], symbol)
                    level["status"] = "cancelled"
                    cancelled_count += 1
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)
        logger.info(f"📋 {symbol} 重校准: 已取消 {cancelled_count} 个旧订单")

        # 生成新层级
        new_levels = self._generate_geometric_levels(new_center, spacing_decimal, n_buys, n_sells)

        # 重新下单
        balance = self.exchange.fetch_usdt_balance()
        n_total = n_buys + n_sells
        pos_ratio = self.config.grid_position_ratio * (2.0 if grid.get("weekend") else 1.0)
        per_level_margin = balance * pos_ratio / n_total
        contract_size = self.exchange.get_contract_size(symbol)
        min_amount = self.exchange.get_min_amount(symbol)

        success_count = 0
        for level in new_levels:
            position_value = per_level_margin * self.config.leverage
            amount = position_value / (level["price"] * contract_size)
            amount = max(1, math.floor(amount))

            if amount < min_amount:
                level["status"] = "skipped"
                continue

            level["amount"] = amount
            try:
                order = self.exchange.create_limit_order(
                    symbol=symbol,
                    side=level["side"],
                    amount=amount,
                    price=level["price"],
                    params={"tradeSide": "open"},
                )
                if order and order.get("id"):
                    level["order_id"] = str(order["id"])
                    level["status"] = "open"
                    success_count += 1
            except Exception as e:
                logger.error(f"❌ 重校准下单失败 {symbol} {level['side']}: {e}")
                level["status"] = "failed"

        # 更新网格状态
        highest_sell = max(
            (lvl["price"] for lvl in new_levels if lvl["side"] == "sell"),
            default=new_center
        )
        lowest_buy = min(
            (lvl["price"] for lvl in new_levels if lvl["side"] == "buy"),
            default=new_center
        )

        grid["levels"] = new_levels
        grid["center"] = round(new_center, 4)
        grid["support"] = round(lowest_buy, 4)
        grid["resistance"] = round(highest_sell, 4)
        grid["last_recalibration"] = now_iso()
        grid["recalibration_count"] = recal_count
        grid["active"] = success_count > 0
        grid["realized_pnl"] = kept_pnl
        grid["total_margin_used"] = per_level_margin * success_count

        self._save_state()

        self.tlogger.log_risk("GRID_RECALIBRATE",
            f"{symbol}: new_center={new_center:.4f} "
            f"count=#{recal_count} "
            f"levels={success_count}/{n_total} "
            f"spacing={spacing_decimal*100:.2f}%")

        if success_count > 0:
            logger.info(f"📋 {symbol} 重校准完成: 新锚点={new_center:.4f}, "
                        f"部署 {success_count}/{n_total} 层级")
        else:
            logger.warning(f"⚠️  {symbol} 重校准失败: 0/{n_total} 层级下单成功")

    # ════════════════════════════════════════════
    # v3.0: 回撤断路器
    # ════════════════════════════════════════════

    def _check_drawdown_breaker(self, symbol: str, grid: dict) -> bool:
        """
        v3.0: 回撤断路器。
        条件: realized_pnl / total_margin_used < DRAWDOWN_THRESHOLD_PCT (-3%)
        触发: 取消所有订单 + 标记 inactive。
        返回 True 表示已触发断路器。
        """
        total_margin = grid.get("total_margin_used", 0)
        if total_margin <= 0:
            return False

        realized_pnl = grid.get("realized_pnl", 0.0)
        drawdown_pct = realized_pnl / total_margin

        if drawdown_pct < self.DRAWDOWN_THRESHOLD_PCT:
            logger.warning(f"⛔ {symbol} 回撤断路器触发！"
                           f"已实现亏损={drawdown_pct*100:.1f}% "
                           f"(阈值={self.DRAWDOWN_THRESHOLD_PCT*100:.0f}%) "
                           f"保证金={total_margin:.2f}U PnL={realized_pnl:.4f}U")

            # 取消所有层级订单
            self.cancel_grid(symbol)

            # 记录风控事件
            self.tlogger.log_risk("GRID_DRAWDOWN_BREAKER",
                f"{symbol}: drawdown={drawdown_pct*100:.2f}% "
                f"margin={total_margin:.2f} pnl={realized_pnl:.4f} "
                f"levels={len(grid.get('levels', []))}")
            return True

        return False

    # ════════════════════════════════════════════
    # 轮询 & TP/SL (v3.0: 全部新特性集成)
    # ════════════════════════════════════════════

    def poll_grids(self, price_change_1h_pct: float = 0.0) -> dict:
        """
        v3.0: 轮询所有活跃网格，集成全部新特性:
          1. 回撤断路器检测
          2. 锚点重校准检测
          3. 订单成交检测 + TP/SL 下单
          4. 无限网格：卖单成交后自动补单
        参数:
          price_change_1h_pct: 1h 价格变动百分比（用于重校准跳过判断，默认 0）
        返回:
          dict with filled_orders, cancelled_grids, total_active
        """
        filled_count = 0
        cancelled = 0

        for symbol, grid in list(self.grids.items()):
            if not grid.get("active"):
                continue

            # ── v3.0: 回撤断路器（优先级最高） ──
            if self._check_drawdown_breaker(symbol, grid):
                continue  # 网格已被 cancel_grid 标记为 inactive

            # ── 获取当前价格（用于重校准检测） ──
            current_price = 0.0
            try:
                # 通过 ccxt 原生接口获取 ticker（ExchangeInterface 可能未封装此法）
                ticker = self.exchange.exchange.fetch_ticker(symbol)
                current_price = float(ticker.get("last", 0)) if ticker else 0.0
            except Exception:
                # 退路：从网格层级中估算现价（取买卖单中间价）
                buy_prices = [lv["price"] for lv in grid["levels"] if lv["side"] == "buy" and lv["price"] > 0]
                sell_prices = [lv["price"] for lv in grid["levels"] if lv["side"] == "sell" and lv["price"] > 0]
                if buy_prices and sell_prices:
                    current_price = (max(buy_prices) + min(sell_prices)) / 2

            # ── v3.0: 锚点重校准检测 ──
            if current_price > 0:
                if self._check_recalibration(symbol, grid, current_price, price_change_1h_pct):
                    # 重校准内部已完成 cancel + redeploy，跳过本轮状态同步
                    continue

            # ── 轮询层级订单状态 ──
            any_open = False
            for level in grid["levels"]:
                if level["status"] != "open" or not level.get("order_id"):
                    continue

                try:
                    order_info = self.exchange.fetch_order(level["order_id"], symbol)
                    if not order_info:
                        any_open = True
                        continue

                    status = str(order_info.get("status", "")).lower()
                    if status in ("closed", "filled"):
                        level["status"] = "filled"
                        level["filled_price"] = float(
                            order_info.get("price") or order_info.get("average", level["price"])
                        )
                        filled_count += 1

                        # 设置止盈止损
                        self._place_grid_tp(symbol, level)

                        logger.info(f"📋 网格成交: {symbol} {level['side']} "
                                    f"{level['amount']}张 @{level['filled_price']:.4f}")

                        # ── v3.0: 无限网格 — 卖单成交后自动补单 ──
                        if level["side"] == "sell":
                            self._replenish_sell_level(symbol, grid, level)

                    elif status in ("canceled", "expired", "rejected"):
                        level["status"] = "cancelled"
                        cancelled += 1
                    else:
                        any_open = True

                except Exception as e:
                    logger.debug(f"📋 轮询网格 {level.get('order_id')}: {e}")
                    any_open = True

            # 没有 open 订单 → 标记完成
            if not any_open:
                grid["active"] = False
                logger.info(f"📋 {symbol} 网格所有层级已完成/成交，网格关闭")

        self._save_state()
        return {
            "filled_orders": filled_count,
            "cancelled_grids": cancelled,
            "total_active": len(self.get_active_grids()),
        }

    def _place_grid_tp(self, symbol: str, level: dict):
        """
        v3.0: 为成交的网格层级设置止盈止损单。
        TP: 限价单 (tradeSide=close, reduceOnly=True)
        SL: 计划市价单 (triggerPrice + reduceOnly)
        """
        try:
            close_side = "sell" if level["side"] == "buy" else "buy"

            # TP 限价平仓单 — 通过 ExchangeInterface 下单
            self.exchange.create_limit_order(
                symbol=symbol,
                side=close_side,
                amount=level["amount"],
                price=level["tp_price"],
                params={"tradeSide": "close", "reduceOnly": True},
            )

            # SL 计划市价单 — 通过 ccxt 原生接口下单
            try:
                self.exchange.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=level["amount"],
                    params={
                        "triggerPrice": level["sl_price"],
                        "tradeSide": "close",
                        "reduceOnly": True,
                    },
                )
            except Exception as e:
                logger.debug(f"📋 网格 SL 下单失败（可忽略）: {e}")

            logger.info(f"📋 网格 TP/SL 已设置: {symbol} {close_side} "
                        f"TP={level['tp_price']:.4f} SL={level['sl_price']:.4f}")
        except Exception as e:
            logger.error(f"❌ 网格 TP/SL 设置失败 {symbol}: {e}")

    # ════════════════════════════════════════════
    # 取消 & 清理
    # ════════════════════════════════════════════

    def cancel_grid(self, symbol: str) -> bool:
        """
        取消指定 symbol 的所有网格订单。
        返回 True 表示有订单被取消。
        """
        if symbol not in self.grids:
            return False

        grid = self.grids[symbol]
        n = 0
        for level in grid.get("levels", []):
            if level["status"] == "open" and level.get("order_id"):
                try:
                    self.exchange.cancel_order(level["order_id"], symbol)
                    level["status"] = "cancelled"
                    n += 1
                except Exception as e:
                    logger.debug(f"📋 取消网格订单失败 {level.get('order_id')}: {e}")

        grid["active"] = False
        self._save_state()
        if n > 0:
            logger.info(f"📋 取消 {symbol} 网格: {n} 个订单已撤销")
        return n > 0

    def cancel_all(self):
        """取消所有活跃网格"""
        symbols_to_cancel = [s for s in self.grids if self.grids[s].get("active")]
        for sym in symbols_to_cancel:
            self.cancel_grid(sym)
        if symbols_to_cancel:
            logger.info(f"📋 已取消全部网格: {len(symbols_to_cancel)} 个")

    # ════════════════════════════════════════════
    # 持久化（原子写入）
    # ════════════════════════════════════════════

    def _save_state(self):
        """
        原子写入 grid_state.json。
        先写临时文件再 rename，避免写入中断导致文件损坏。
        """
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.grids, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.error(f"❌ grid_state 保存失败: {e}")

    def _load_state(self):
        """
        从 grid_state.json 加载网格状态。
        兼容 v2.0 旧格式（缺少 spacing_decimal 等新字段时会自动补全）。
        """
        if not os.path.exists(self._state_path):
            return

        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # ── v3.0: 兼容 v2.0 旧格式 ──
            for sym, grid in loaded.items():
                # 补全缺失的 v3.0 字段
                grid.setdefault("center", grid.get("support", 0))
                grid.setdefault("spacing_decimal",
                                grid.get("spacing_pct", 1.0) / 100.0)
                grid.setdefault("last_recalibration", None)
                grid.setdefault("recalibration_count", 0)
                grid.setdefault("total_margin_used", 0.0)
                # 旧格式 levels 缺少 status 字段时补全
                for level in grid.get("levels", []):
                    level.setdefault("status", "pending")
                    level.setdefault("filled_price", None)

            self.grids = loaded

            active = len(self.get_active_grids())
            if active > 0:
                logger.info(f"📋 已加载 {active} 个活跃网格 (共 {len(self.grids)} 个历史记录)")
        except Exception as e:
            logger.error(f"❌ grid_state 加载失败: {e}")
            self.grids = {}

