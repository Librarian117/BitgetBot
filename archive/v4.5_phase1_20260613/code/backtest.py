#!/usr/bin/env python3
"""
backtest.py — DeepSeekQuantBot 回测引擎 v1.0
=============================================
独立脚本，使用 ccxt 拉取历史 OHLCV，walk-forward 重放策略逻辑。
不导入 live bot，独立复制信号生成和交易模拟。

用法:
  python backtest.py --symbols BTC/USDT:USDT ETH/USDT:USDT --start 2026-01-01 --end 2026-06-01
  python backtest.py --symbols BTC/USDT:USDT --start 2026-01-01 --end 2026-06-01 --timeframe 1h
  python backtest.py --output logs/my_backtest

输出:
  - 控制台打印汇总指标（夏普、最大回撤、胜率、盈亏比、周内/周末分拆）
  - CSV 逐笔交易明细写入 logs/backtest_results/
"""

import argparse
import json
import logging
import math
import os
import time
from time_utils import now
from typing import Any, Dict, List, Optional

import ccxt
import numpy as np
import pandas as pd

# ============================================================================
# 日志
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] Backtest | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Backtest")

# ============================================================================
# 默认策略参数（与 v2.7 ConfigManager 保持一致）
# ============================================================================
DEFAULT_CONFIG = {
    "timeframe": "15m",
    "ema_period": 200,
    "rsi_period": 14,
    "atr_period": 14,
    "adx_period": 14,
    "adx_threshold": 20,
    "rsi_oversold": 40,
    "rsi_overbought": 60,
    "vol_lookback": 20,
    "vol_ratio_threshold": 1.5,
    "leverage": 5,
    "sl_atr_mult": 1.5,
    "tp_atr_mults": [2.0, 3.0],
    "tp_split_ratios": [0.5, 0.5],
    "slippage_buffer": 0.0005,
    "taker_fee": 0.0006,
    "initial_balance": 10000.0,
    "margin_ratio": 0.05,
    "max_hold_bars": 80,  # 最长持仓 80 根 K 线 (15m × 80 = 20h)
    # 动量策略
    "momentum_enabled": True,
    "momentum_adx_threshold": 25,
    "momentum_breakout_bars": 20,
    "momentum_rsi_min": 55,
    "momentum_rsi_max": 78,
    "momentum_ema_period": 50,
    "momentum_sl_mult": 2.5,
    "momentum_margin_ratio": 0.03,
    # 时段参数（周末）
    "weekend_adx_threshold": 16,
    "weekend_vol_ratio": 0.3,
    "weekend_margin_mult": 0.6,
    "weekend_sl_mult": 2.0,
}

# ============================================================================
# 指标计算（复制 IndicatorCalculator 核心逻辑）
# ============================================================================
class IndicatorCalculator:
    """纯本地计算，与 live bot 完全一致"""

    def __init__(self, config: Dict[str, Any]):
        self.ema_period = config["ema_period"]
        self.rsi_period = config["rsi_period"]
        self.atr_period = config["atr_period"]
        self.adx_period = config["adx_period"]

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = df["close"]
        df["ema"] = close.ewm(span=self.ema_period, adjust=False).mean()
        df["rsi"] = self._wilder_rsi(close, self.rsi_period)
        df["atr"] = self._wilder_atr(df, self.atr_period)
        df["adx"] = self._wilder_adx(df, self.adx_period)
        return df

    @staticmethod
    def _wilder_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[avg_loss == 0] = 100.0
        return rsi

    @staticmethod
    def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _wilder_adx(df: pd.DataFrame, period: int) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
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
        mask_plus = (up_move > down_move) & (up_move > 0)
        mask_minus = (down_move > up_move) & (down_move > 0)
        plus_dm[mask_plus] = up_move[mask_plus]
        minus_dm[mask_minus] = down_move[mask_minus]
        smooth_plus = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        smooth_minus = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        plus_di = 100.0 * smooth_plus / atr.replace(0, np.nan)
        minus_di = 100.0 * smooth_minus / atr.replace(0, np.nan)
        di_sum = plus_di + minus_di
        dx = (plus_di - minus_di).abs() / di_sum.replace(0, np.nan) * 100.0
        return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# ============================================================================
# 时段判断（复制 SessionManager）
# ============================================================================
def is_weekend(ts: pd.Timestamp) -> bool:
    """判断时间戳是否为周末 (weekday >= 5)"""
    return ts.weekday() >= 5


def get_effective_params(config: Dict[str, Any], ts: pd.Timestamp) -> Dict[str, Any]:
    """根据时段返回有效参数"""
    if is_weekend(ts):
        return {
            "adx_threshold": config["weekend_adx_threshold"],
            "vol_ratio": config["weekend_vol_ratio"],
            "margin_mult": config["weekend_margin_mult"],
            "sl_mult": config["weekend_sl_mult"],
        }
    return {
        "adx_threshold": config["adx_threshold"],
        "vol_ratio": config["vol_ratio_threshold"],
        "margin_mult": 1.0,
        "sl_mult": 1.0,
    }


# ============================================================================
# 信号生成（复制 scan_single_symbol 逻辑）
# ============================================================================
def generate_signal(
    df_window: pd.DataFrame, config: Dict[str, Any], indicator: IndicatorCalculator
) -> Optional[Dict[str, Any]]:
    """
    给定一个包含足够历史数据的 DataFrame 窗口，检查最新 bar 是否产生信号。
    返回信号字典或 None。
    """
    min_bars = max(config["rsi_period"], config["atr_period"]) + 1
    if len(df_window) < min_bars:
        return None

    df = indicator.compute_all(df_window)
    latest = df.iloc[-1]
    close = float(latest["close"])
    ema = float(latest["ema"])
    rsi = float(latest["rsi"])
    atr = float(latest["atr"])
    adx = float(latest["adx"])

    if pd.isna(ema) or pd.isna(rsi) or pd.isna(atr) or pd.isna(adx):
        return None

    ts = df.index[-1]
    params = get_effective_params(config, ts)

    # ADX 过滤
    if adx < params["adx_threshold"]:
        return None

    # 成交量确认
    usd_vol = df["volume"] * df["close"]
    recent = usd_vol.tail(config["vol_lookback"])
    current_vol = float(recent.iloc[-1])
    median_vol = float(recent.median())
    if median_vol > 0:
        vol_ratio = current_vol / median_vol
        if vol_ratio < params["vol_ratio"]:
            return None
    else:
        vol_ratio = 1.0

    # 回调策略
    direction = None
    strategy = "pullback"
    if close > ema and rsi < config["rsi_oversold"]:
        direction = "LONG"
    elif close < ema and rsi > config["rsi_overbought"]:
        direction = "SHORT"

    # 动量策略
    if direction is None and config["momentum_enabled"]:
        if (
            close > ema
            and rsi > config["momentum_rsi_min"]
            and rsi < config["momentum_rsi_max"]
            and adx > config["momentum_adx_threshold"]
        ):
            mom_ema = float(
                df["close"].ewm(span=config["momentum_ema_period"], adjust=False)
                .mean()
                .iloc[-1]
            )
            if close > mom_ema:
                recent_high = float(
                    df["high"].tail(config["momentum_breakout_bars"]).max()
                )
                if close >= recent_high * 0.998:
                    direction = "LONG"
                    strategy = "momentum"

    if direction is None:
        return None

    return {
        "direction": direction,
        "strategy": strategy,
        "price": close,
        "ema": ema,
        "rsi": rsi,
        "atr": atr,
        "adx": adx,
        "vol_ratio": round(vol_ratio, 2),
        "timestamp": ts,
    }


# ============================================================================
# 交易模拟（复制 TradeExecutor SL/TP 逻辑）
# ============================================================================
def simulate_trade(
    signal: Dict[str, Any],
    df_forward: pd.DataFrame,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    模拟入场 → SL/TP 命中 → 退出。
    遍历未来 K 线，检查 OHLC 精度下最先触发的条件。
    """
    direction = signal["direction"]
    strategy = signal["strategy"]
    entry_price = signal["price"]
    atr = signal["atr"]

    # ── 保证金计算 ──
    params = get_effective_params(config, signal["timestamp"])
    if strategy == "momentum":
        margin_ratio = config["momentum_margin_ratio"] * params["margin_mult"]
    else:
        margin_ratio = config["margin_ratio"] * params["margin_mult"]

    # ── SL 计算 ──
    if strategy == "momentum":
        sl_dist = config["momentum_sl_mult"] * atr
    else:
        sl_dist = config["sl_atr_mult"] * params["sl_mult"] * atr

    slippage = entry_price * config["slippage_buffer"]

    if direction == "LONG":
        sl_price = entry_price - sl_dist - slippage
    else:
        sl_price = entry_price + sl_dist + slippage

    # ── TP 计算 ──
    tp_prices = []
    for mult in config["tp_atr_mults"]:
        tp_dist = mult * atr
        if direction == "LONG":
            tp_prices.append(entry_price + tp_dist)
        else:
            tp_prices.append(entry_price - tp_dist)

    # ── 模拟执行 ──
    # TP 按距入场价从远到近排序 (远=更优价格，应优先完全退出)
    if direction == "LONG":
        tp_sorted = sorted(tp_prices, reverse=True)  # 高价 → 低价
    else:
        tp_sorted = sorted(tp_prices)  # 低价 → 高价 (SHORT 低价更优)
    tp_ratio_map = dict(zip(tp_prices, config["tp_split_ratios"]))

    remaining_ratio = 1.0
    # 加权平均出场价：跟踪部分止盈的头寸
    weighted_exit = 0.0  # sum(ratio * tp_price) for partial exits
    final_exit_price = 0.0
    exit_reason = "timeout"
    bars_held = 0

    for i, (_, bar) in enumerate(df_forward.iterrows()):
        bars_held = i + 1
        high = float(bar["high"])
        low = float(bar["low"])
        close_val = float(bar["close"])

        # ── 先检查止损 ──
        sl_hit = False
        if direction == "LONG":
            if low <= sl_price:
                sl_hit = True
        else:
            if high >= sl_price:
                sl_hit = True

        if sl_hit:
            # 止损：剩余全部仓位在 SL 价退出
            weighted_exit += remaining_ratio * sl_price
            remaining_ratio = 0.0
            exit_reason = "sl"
            break

        # ── 止盈：从最优价格到最差价格检查 ──
        for tp_price in tp_sorted:
            hit = (direction == "LONG" and high >= tp_price) or (
                direction == "SHORT" and low <= tp_price
            )
            if hit and remaining_ratio > 0.001:
                tp_ratio = tp_ratio_map.get(tp_price, 1.0)
                taken = min(tp_ratio, remaining_ratio)
                weighted_exit += taken * tp_price
                remaining_ratio -= taken
                if remaining_ratio <= 0.001:
                    exit_reason = f"tp@{tp_price:.4f}"
                    break
        if remaining_ratio <= 0.001:
            break

        # 超时：按收盘价退出
        if bars_held >= config["max_hold_bars"]:
            weighted_exit += remaining_ratio * close_val
            remaining_ratio = 0.0
            exit_reason = "timeout"
            break

    # 未触发任何条件 → 剩余仓位以最后收盘价退出
    if remaining_ratio > 0.001:
        last_close = float(df_forward.iloc[-1]["close"])
        weighted_exit += remaining_ratio * last_close
        remaining_ratio = 0.0
        exit_reason = "timeout"
        bars_held = len(df_forward)

    # 加权平均出场价
    final_exit_price = weighted_exit  # total_realized = 1.0

    # ── PnL 计算 ──
    # position_value 已含杠杆，pnl_pct 不再乘杠杆避免重复计算
    position_value = config["initial_balance"] * margin_ratio * config["leverage"]
    fee = position_value * config["taker_fee"] * 2  # 开平各一次

    if direction == "LONG":
        price_change_pct = (final_exit_price - entry_price) / entry_price
    else:
        price_change_pct = (entry_price - final_exit_price) / entry_price

    pnl_pct = price_change_pct  # 基础涨跌幅 (未杠杆)
    pnl_usdt = position_value * price_change_pct - fee

    return {
        "entry_time": signal["timestamp"],
        "exit_time": df_forward.index[0] if len(df_forward) > 0 else signal["timestamp"],
        "direction": direction,
        "strategy": strategy,
        "entry_price": round(entry_price, 4),
        "exit_price": round(final_exit_price, 4),
        "exit_reason": exit_reason,
        "pnl_usdt": round(pnl_usdt, 4),
        "pnl_pct": round(pnl_pct * 100, 4),
        "fee_usdt": round(fee, 4),
        "bars_held": bars_held,
        "is_weekend": is_weekend(signal["timestamp"]),
    }


# ============================================================================
# 回测主引擎
# ============================================================================
class Backtester:
    """历史数据 Walk-forward 回测引擎"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.indicator = IndicatorCalculator(config)
        self.trades: List[Dict[str, Any]] = []

        # 初始化 ccxt（仅数据获取）
        self.exchange = ccxt.bitget({"enableRateLimit": True})
        # 用公共数据，不开沙箱模式

    def fetch_data(
        self, symbol: str, start: str, end: str, timeframe: str = "15m"
    ) -> pd.DataFrame:
        """使用 ccxt since 参数分批拉取历史 OHLCV"""
        since_ts = int(self.exchange.parse8601(f"{start}T00:00:00Z"))
        end_ts = int(self.exchange.parse8601(f"{end}T23:59:59Z"))

        all_candles = []
        current_since = since_ts
        batch = 0

        logger.info(f"📥 拉取 {symbol} {timeframe} 历史数据 {start} → {end} ...")

        while current_since < end_ts:
            batch += 1
            try:
                candles = self.exchange.fetch_ohlcv(
                    symbol, timeframe, since=current_since, limit=200
                )
                if not candles:
                    break

                all_candles.extend(candles)

                # 推进 since 到最后一根 K 线之后
                last_ts = candles[-1][0]
                if last_ts <= current_since:
                    break  # 防止死循环
                current_since = last_ts + 1

                if batch % 5 == 0:
                    logger.info(f"  📥 已拉取 {len(all_candles)} 根 K 线 ...")

                time.sleep(0.3)  # 避免触发限流
            except Exception as e:
                logger.warning(f"  ⚠️  批次 {batch} 失败: {e}")
                time.sleep(2)
                continue

        if not all_candles:
            raise RuntimeError(f"❌ 未能获取 {symbol} 任何历史数据")

        # 去重 & 排序
        seen = set()
        unique = []
        for c in sorted(all_candles, key=lambda x: x[0]):
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)

        df = pd.DataFrame(
            unique,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)

        logger.info(f"✅ {symbol}: {len(df)} 根 K 线 ({df.index[0]} → {df.index[-1]})")
        return df

    def run(self, symbols: List[str]) -> pd.DataFrame:
        """主入口：对每个币种依次回测"""
        all_trades = []

        for sym in symbols:
            logger.info(f"\n{'='*50}")
            logger.info(f"🔄 回测 {sym}")
            logger.info(f"{'='*50}")

            try:
                df = self.fetch_data(
                    sym,
                    start=self.config.get("_start", "2026-01-01"),
                    end=self.config.get("_end", "2026-05-31"),
                    timeframe=self.config["timeframe"],
                )
            except Exception as e:
                logger.error(f"❌ {sym} 数据获取失败: {e}")
                continue

            warmup = max(
                self.config["ema_period"],
                self.config["rsi_period"],
                self.config["atr_period"],
                self.config["vol_lookback"],
            ) + 10

            if len(df) < warmup + self.config["max_hold_bars"]:
                logger.warning(f"⚠️  {sym} K线不足 ({len(df)} < {warmup + self.config['max_hold_bars']})，跳过")
                continue

            # Walk-forward
            signals_count = 0
            for i in range(warmup, len(df) - 5):
                df_window = df.iloc[: i + 1]
                signal = generate_signal(df_window, self.config, self.indicator)

                if signal is None:
                    continue

                signals_count += 1

                # 模拟交易
                df_forward = df.iloc[i + 1 : i + 1 + self.config["max_hold_bars"]]
                if len(df_forward) == 0:
                    continue

                trade = simulate_trade(signal, df_forward, self.config)
                trade["symbol"] = sym.replace("/USDT:USDT", "")
                all_trades.append(trade)

            logger.info(f"  {sym}: {signals_count} 信号, {len([t for t in all_trades if t['symbol'] == sym.replace('/USDT:USDT', '')])} 交易")

        self.trades = all_trades
        return pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

    def calculate_metrics(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        """计算汇总指标，包含周内/周末分拆"""
        if trades_df.empty:
            return {"error": "无交易数据"}

        def _stats(subset: pd.DataFrame) -> Dict[str, Any]:
            if subset.empty:
                return {"trades": 0, "win_rate": 0, "total_pnl": 0.0, "sharpe": 0.0,
                        "max_drawdown": 0.0, "profit_factor": 0.0, "avg_pnl": 0.0}
            wins = subset[subset["pnl_usdt"] > 0]
            losses = subset[subset["pnl_usdt"] < 0]
            win_rate = len(wins) / len(subset) if len(subset) > 0 else 0
            total_pnl = subset["pnl_usdt"].sum()
            avg_pnl = subset["pnl_usdt"].mean()
            std_pnl = subset["pnl_usdt"].std()
            # 年化夏普（假设 15m K 线，每年约 35040 根）
            periods_per_year = 35040
            sharpe = (avg_pnl / std_pnl) * math.sqrt(periods_per_year) if std_pnl and std_pnl > 0 else 0.0
            # 最大回撤
            cumsum = subset["pnl_usdt"].cumsum()
            running_max = cumsum.cummax()
            drawdown = cumsum - running_max
            max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0.0
            # 盈亏比
            avg_win = wins["pnl_usdt"].mean() if len(wins) > 0 else 0.0
            avg_loss = abs(losses["pnl_usdt"].mean()) if len(losses) > 0 else 0.0
            profit_factor = (len(wins) * avg_win) / (len(losses) * avg_loss) if len(losses) > 0 and avg_loss > 0 else float("inf")
            return {
                "trades": len(subset),
                "win_rate": round(win_rate * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 4),
                "sharpe": round(sharpe, 2),
                "max_drawdown": round(max_dd, 2),
                "profit_factor": round(profit_factor, 2),
                "avg_win": round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
            }

        overall = _stats(trades_df)
        weekday = _stats(trades_df[~trades_df["is_weekend"]])
        weekend = _stats(trades_df[trades_df["is_weekend"]])

        # 按策略分拆
        pullback_df = trades_df[trades_df["strategy"] == "pullback"]
        momentum_df = trades_df[trades_df["strategy"] == "momentum"]

        return {
            "overall": overall,
            "weekday": weekday,
            "weekend": weekend,
            "pullback": _stats(pullback_df),
            "momentum": _stats(momentum_df),
        }

    def export_csv(self, trades_df: pd.DataFrame, output_dir: str):
        """导出逐笔交易 CSV"""
        os.makedirs(output_dir, exist_ok=True)
        ts = now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(output_dir, f"backtest_{ts}.csv")
        trades_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"📁 交易明细已导出: {csv_path}")

        # 汇总 JSON
        metrics = self.calculate_metrics(trades_df)
        json_path = os.path.join(output_dir, f"backtest_{ts}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        logger.info(f"📁 汇总指标已导出: {json_path}")


# ============================================================================
# CLI
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="DeepSeekQuantBot 回测引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backtest.py --symbols BTC/USDT:USDT ETH/USDT:USDT --start 2026-01-01 --end 2026-06-01
  python backtest.py --symbols BTC/USDT:USDT --start 2026-03-01 --end 2026-06-01 --timeframe 1h
  python backtest.py --symbols BTC/USDT:USDT --start 2026-01-01 --end 2026-06-01 --output logs/my_results
        """,
    )
    parser.add_argument(
        "--symbols", nargs="+",
        default=["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        help="回测币种列表 (默认: BTC ETH SOL)"
    )
    parser.add_argument("--start", default="2026-01-01", help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-31", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--timeframe", default="15m", help="K 线周期 (默认: 15m)")
    parser.add_argument("--output", default="logs/backtest_results", help="报告输出目录")
    parser.add_argument("--balance", type=float, default=10000.0, help="初始余额 (默认: 10000)")
    parser.add_argument("--no-momentum", action="store_true", help="禁用动量策略")

    args = parser.parse_args()

    config = DEFAULT_CONFIG.copy()
    config["timeframe"] = args.timeframe
    config["initial_balance"] = args.balance
    config["_start"] = args.start
    config["_end"] = args.end
    if args.no_momentum:
        config["momentum_enabled"] = False

    logger.info("🚀 DeepSeekQuantBot 回测引擎")
    logger.info(f"📋 币种: {args.symbols}")
    logger.info(f"📅 区间: {args.start} → {args.end}")
    logger.info(f"⏱  周期: {args.timeframe}")
    logger.info(f"💰 初始余额: {args.balance} USDT")

    bt = Backtester(config)
    trades_df = bt.run(args.symbols)

    if trades_df.empty:
        logger.warning("⚠️  未产生任何交易")
        return

    # 打印报告
    metrics = bt.calculate_metrics(trades_df)
    print("\n" + "=" * 60)
    print("  [Backtest Report]")
    print("=" * 60)

    for section, m in metrics.items():
        if isinstance(m, dict) and m.get("trades", 0) > 0:
            print(f"\n  ── {section} ──")
            print(f"  交易数: {m['trades']}")
            print(f"  胜率: {m['win_rate']}%")
            print(f"  总盈亏: {m['total_pnl']:+.2f} USDT")
            print(f"  平均盈亏: {m['avg_pnl']:+.4f} USDT")
            print(f"  年化夏普: {m['sharpe']}")
            print(f"  最大回撤: {m['max_drawdown']} USDT")
            print(f"  盈亏比: {m['profit_factor']}")

    print("\n  ── 逐笔概览 ──")
    print(f"  做多交易: {len(trades_df[trades_df['direction']=='LONG'])}")
    print(f"  做空交易: {len(trades_df[trades_df['direction']=='SHORT'])}")
    print(f"  回调策略: {len(trades_df[trades_df['strategy']=='pullback'])}")
    print(f"  动量策略: {len(trades_df[trades_df['strategy']=='momentum'])}")
    print(f"  周内: {len(trades_df[~trades_df['is_weekend']])}")
    print(f"  周末: {len(trades_df[trades_df['is_weekend']])}")
    print("=" * 60)

    # 导出
    bt.export_csv(trades_df, args.output)


if __name__ == "__main__":
    main()
