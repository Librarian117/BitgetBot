#!/usr/bin/env python3
"""
BitgetSimpleStrategy — 精简版混合策略 v4
=========================================
基于 v1-v3 回测迭代结果:
  - bollinger: 唯一持续盈利策略 (54-58% 胜率)
  - pullback: 原 bot 主力，在 Freqtrade 中需参数调优
  - ema_cross/momentum: 过度交易，删除
  - counter_trend: 逆势风险高，删除

改进:
  - 1h K线: 减少噪音，降低交易频率
  - 入场需多重确认 (ADX+RSI+布林带/MACD)
  - 激进止损 + 快出僵尸仓
"""

from datetime import datetime
from functools import reduce

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import (BooleanParameter, DecimalParameter, IntParameter,
                                 IStrategy)


class BitgetSimpleStrategy(IStrategy):
    """精简混合策略 — 1h 框架，只做高胜率信号"""

    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True
    position_adjustment_enable = False

    startup_candle_count = 200

    trading_mode = "futures"
    margin_mode = "isolated"

    # ── 止损/止盈 ──
    stoploss = -0.12

    # Hyperopt 优化空间
    buy_rsi_oversold = IntParameter(30, 48, default=38, space="buy")
    buy_rsi_overbought = IntParameter(52, 72, default=62, space="buy")
    buy_adx_threshold = IntParameter(12, 28, default=18, space="buy")

    # 追踪止损
    trailing_stop = True
    trailing_stop_positive = 0.03
    trailing_stop_positive_offset = 0.06
    trailing_only_offset_is_reached = False

    minimal_roi = {
        "0":    0.15,
        "120":  0.08,
        "480":  0.04,
    }

    use_custom_stoploss = True
    use_custom_exit = True

    # ── 可优化参数 ──
    rsi_oversold = IntParameter(30, 45, default=38, space="buy")
    rsi_overbought = IntParameter(55, 70, default=62, space="buy")
    adx_threshold = IntParameter(14, 25, default=18, space="buy")

    # ═══════════════════════════════════════
    # 指标
    # ═══════════════════════════════════════
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()

        # 均线
        df["ema9"] = ta.EMA(df, timeperiod=9)
        df["ema21"] = ta.EMA(df, timeperiod=21)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["ema200"] = ta.EMA(df, timeperiod=200)

        # RSI
        df["rsi"] = ta.RSI(df, timeperiod=14)

        # ADX
        df["adx"] = ta.ADX(df, timeperiod=14)

        # ATR
        df["atr"] = ta.ATR(df, timeperiod=14)

        # MACD
        macd = ta.MACD(df, fastperiod=12, slowperiod=26, signalperiod=9)
        df["macd"] = macd["macd"]
        df["macd_signal"] = macd["macdsignal"]
        df["macd_hist"] = macd["macdhist"]

        # 布林带
        bb = ta.BBANDS(df, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        df["bb_lower"] = bb["lowerband"]
        df["bb_mid"] = bb["middleband"]
        df["bb_upper"] = bb["upperband"]
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

        # 趋势判断
        df["ema50_rising"] = df["ema50"] > df["ema50"].shift(5)
        df["ema200_rising"] = df["ema200"] > df["ema200"].shift(5)
        df["ema200_falling"] = df["ema200"] < df["ema200"].shift(5)
        df["is_bullish_trend"] = df["ema200_rising"] & (df["ema50"] > df["ema200"])
        df["is_bearish_trend"] = df["ema200_falling"] & (df["ema50"] < df["ema200"])

        # 量
        df["volume_mean_20"] = df["volume"].rolling(20).mean()

        # 近期高低
        df["recent_high_20"] = df["high"].rolling(20).max()
        df["recent_low_20"] = df["low"].rolling(20).min()

        return df

    # ═══════════════════════════════════════
    # 入场 (仅 pullback + bollinger)
    # ═══════════════════════════════════════
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["enter_long"] = 0
        df["enter_short"] = 0
        df["enter_tag"] = ""

        adx_min = int(self.buy_adx_threshold.value)
        rsi_os = int(self.buy_rsi_oversold.value)
        rsi_ob = int(self.buy_rsi_overbought.value)

        # ── pullback LONG: 价格在EMA9上方, RSI回调到超卖区 ──
        pullback_long = (
            (df["close"] > df["ema9"]) &
            (df["rsi"] < rsi_os) &
            (df["adx"] > adx_min) &
            ~df["is_bearish_trend"] &
            (df["macd_hist"] > df["macd_hist"].shift(2))  # MACD 动能回升
        )

        # ── pullback SHORT ──
        pullback_short = (
            (df["close"] < df["ema9"]) &
            (df["rsi"] > rsi_ob) &
            (df["adx"] > adx_min) &
            ~df["is_bullish_trend"] &
            (df["macd_hist"] < df["macd_hist"].shift(2))
        )

        # ── bollinger LONG: 触下轨反弹 ──
        bollinger_long = (
            (df["close"] <= df["bb_lower"] * 1.005) &
            (df["rsi"] < 45) & (df["rsi"] > 20) &
            (df["bb_width"] > 0.02) &
            (df["volume"] > df["volume_mean_20"] * 0.9)
        )

        # ── bollinger SHORT: 触上轨回落 ──
        bollinger_short = (
            (df["close"] >= df["bb_upper"] * 0.995) &
            (df["rsi"] > 55) & (df["rsi"] < 80) &
            (df["bb_width"] > 0.02) &
            (df["volume"] > df["volume_mean_20"] * 0.9)
        )

        # ── 合并 (pullback 优先) ──
        df.loc[pullback_long, "enter_long"] = 1
        df.loc[pullback_long, "enter_tag"] = "pullback"

        df.loc[~pullback_long & bollinger_long, "enter_long"] = 1
        df.loc[~pullback_long & bollinger_long, "enter_tag"] = "bollinger"

        df.loc[pullback_short, "enter_short"] = 1
        # 不覆盖已有 LONG tag
        untagged = pullback_short & (df["enter_tag"] == "")
        df.loc[untagged, "enter_tag"] = "pullback"

        untagged = ~pullback_short & bollinger_short & (df["enter_tag"] == "")
        df.loc[untagged, "enter_short"] = 1
        df.loc[untagged, "enter_tag"] = "bollinger"

        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["exit_long"] = 0
        df["exit_short"] = 0
        df["exit_tag"] = ""

        # RSI 极端反转 (辅助出场, 非主要)
        exit_long = (df["rsi"] > 78) & (df["macd_hist"] < 0)
        df.loc[exit_long, "exit_long"] = 1
        df.loc[exit_long, "exit_tag"] = "rsi_extreme"

        exit_short = (df["rsi"] < 22) & (df["macd_hist"] > 0)
        df.loc[exit_short, "exit_short"] = 1
        df.loc[exit_short, "exit_tag"] = "rsi_extreme"

        return df

    # ═══════════════════════════════════════
    # 自定义出场
    # ═══════════════════════════════════════
    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs) -> str | None:
        hours = (current_time - trade.open_date_utc).total_seconds() / 3600

        # 浮亏止损: ROI < -15% + >2h
        if current_profit < -0.15 and hours > 2.0:
            return "floating_loss"

        # 僵尸仓: > 6h + |ROI| < 1%
        if hours > 6.0 and abs(current_profit) < 0.01:
            return "stale_zombie"

        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs) -> float | None:
        if after_fill:
            return None

        # 盈利中: 收紧止损到 -5%
        if current_profit > 0.05:
            return -0.05

        return None

    # ═══════════════════════════════════════
    # 仓位管理
    # ═══════════════════════════════════════
    def custom_stake_amount(self, pair: str, current_time: datetime,
                            current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float,
                            leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        if entry_tag == "bollinger":
            # 布林带信号偏弱 → 0.7x 仓位
            return max(min_stake or 0, min(proposed_stake * 0.7, max_stake))
        return proposed_stake
