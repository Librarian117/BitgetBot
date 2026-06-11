#!/usr/bin/env python3
"""
BitgetHybridStrategy — DeepSeekQuantBot 6 策略 Freqtrade 移植版
================================================================
将原 bot 的 pullback / momentum / ema_cross / bollinger / counter_trend / grid
翻译为 Freqtrade IStrategy，利用框架层管理止损/止盈/风控。

原 bot 策略体系:
  - pullback:       趋势中 RSI 回调入场 (主力策略)
  - momentum:       ADX 强趋势 + 价格突破近期高/低
  - ema_cross:      EMA9 金叉/死叉 EMA21
  - bollinger:      触碰布林上下轨反弹
  - counter_trend:  深度超卖/超买逆势 (半仓)
  - grid:           低波动震荡网格 (独立策略文件)

架构:
  - 入场: 6 策略独立条件 → populate_entry_signals()
  - 出场: 止损/止盈/僵尸仓 → custom_exit() + stoploss + trailing
  - 保护: 日内亏损锁/冷却 → Freqtrade Protections
  - 时段: 7 时段波动模型 → custom_signal_filter()

Exchange: Bitget USDT-M Futures (One-way Mode, Isolated Margin)
"""

from datetime import datetime, timedelta, timezone
from functools import reduce

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.constants import Config
from freqtrade.persistence import Trade
from freqtrade.strategy import (BooleanParameter, DecimalParameter, IntParameter,
                                 IStrategy, stoploss_from_open, merge_informative_pair)
from freqtrade.strategy.informative_decorator import informative


class BitgetHybridStrategy(IStrategy):
    """
    DeepSeekQuantBot v4.1 策略体系 Freqtrade 移植版
    ===============================================
    适用: Bitget USDT-M 永续合约, One-way Mode, Isolated Margin, 5m K线
    """

    # ════════════════════════════════════════════
    # Freqtrade 元信息
    # ════════════════════════════════════════════
    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True
    position_adjustment_enable = False  # One-way 模式不需要调整仓位

    # 回测用
    startup_candle_count = 200

    # Bitget Futures 要求
    trading_mode = "futures"
    margin_mode = "isolated"

    # ════════════════════════════════════════════
    # 止损 / 止盈 (框架层) — v2: 回测调优
    # ════════════════════════════════════════════
    stoploss = -0.20          # 硬止损 -20% (v1 -30% → -20% 加速止损)
    trailing_stop = True
    trailing_stop_positive = 0.03    # 盈利 3% 后启动追踪止损 (v1 5% → 3%)
    trailing_stop_positive_offset = 0.05  # 盈利 5% 时追踪止损 price +3%
    trailing_only_offset_is_reached = False

    # ROI 出场表: (持仓分钟, 盈利率) — 达到即平仓
    minimal_roi = {
        "0":    0.30,   # 入场瞬间 ROI>30% → 止盈
        "30":   0.20,   # 30min 后 ROI>20%
        "60":   0.15,   # 1h 后 ROI>15%
        "120":  0.10,   # 2h 后 ROI>10%
        "240":  0.06,   # 4h 后 ROI>6%
    }

    # 启用自定义止损/出场
    use_custom_stoploss = True
    use_custom_exit = True

    # ════════════════════════════════════════════
    # 可超参优化的参数 (Hyperopt)
    # ════════════════════════════════════════════
    # -- 回调策略 (pullback) --
    rsi_oversold = IntParameter(25, 45, default=40, space="buy")
    rsi_overbought = IntParameter(55, 75, default=60, space="buy")

    # -- 动量策略 (momentum) --
    adx_momentum_threshold = IntParameter(20, 35, default=25, space="buy")
    momentum_rsi_min = IntParameter(50, 65, default=55, space="buy")
    momentum_rsi_max = IntParameter(70, 85, default=75, space="buy")
    momentum_breakout_bars = IntParameter(6, 20, default=10, space="buy")

    # -- EMA 交叉 --
    ema_fast = IntParameter(5, 15, default=9, space="buy")
    ema_slow = IntParameter(15, 30, default=21, space="buy")

    # -- 布林带 --
    bb_period = IntParameter(15, 30, default=20, space="buy")
    bb_std = DecimalParameter(1.5, 3.0, default=2.0, space="buy")

    # -- 风控 --
    max_daily_loss_pct = DecimalParameter(0.01, 0.08, default=0.03, space="sell")

    # ════════════════════════════════════════════
    # 交易对过滤 (白名单)
    # ════════════════════════════════════════════
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        计算所有技术指标 — 对应原 IndicatorCalculator.compute_all()
        """
        df = dataframe.copy()

        # ── EMA 系列 ──
        df["ema9"] = ta.EMA(df, timeperiod=9)
        df["ema21"] = ta.EMA(df, timeperiod=21)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["ema200"] = ta.EMA(df, timeperiod=200)

        # ── RSI ──
        df["rsi"] = ta.RSI(df, timeperiod=14)

        # ── ADX ──
        df["adx"] = ta.ADX(df, timeperiod=14)

        # ── ATR ──
        df["atr"] = ta.ATR(df, timeperiod=14)

        # ── MACD ──
        macd = ta.MACD(df, fastperiod=12, slowperiod=26, signalperiod=9)
        df["macd"] = macd["macd"]
        df["macd_signal"] = macd["macdsignal"]
        df["macd_hist"] = macd["macdhist"]

        # ── 布林带 ──
        bb = ta.BBANDS(df, timeperiod=int(self.bb_period.value),
                       nbdevup=float(self.bb_std.value),
                       nbdevdn=float(self.bb_std.value))
        df["bb_lower"] = bb["lowerband"]
        df["bb_mid"] = bb["middleband"]
        df["bb_upper"] = bb["upperband"]

        # ── K线特征 ──
        df["body"] = abs(df["close"] - df["open"])
        df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
        df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]

        # ── 成交量 ──
        df["volume_mean_20"] = df["volume"].rolling(20).mean()

        # ── EMA 趋势判断 ──
        df["ema50_rising"] = df["ema50"] > df["ema50"].shift(5)
        df["ema200_rising"] = df["ema200"] > df["ema200"].shift(5)
        df["ema200_falling"] = df["ema200"] < df["ema200"].shift(5)
        df["is_bullish_trend"] = df["ema200_rising"] & (df["ema50"] > df["ema200"])
        df["is_bearish_trend"] = df["ema200_falling"] & (df["ema50"] < df["ema200"])

        # ── 近期高低点 ──
        df["recent_high_10"] = df["high"].rolling(10).max()
        df["recent_low_10"] = df["low"].rolling(10).min()

        # ── ATR 变化率 ──
        df["atr_prev5_mean"] = df["atr"].shift(1).rolling(5).mean()

        # ── 3 连阴阳 ──
        df["three_green"] = (df["close"] > df["close"].shift(1)) & \
                            (df["close"].shift(1) > df["close"].shift(2))
        df["three_red"] = (df["close"] < df["close"].shift(1)) & \
                          (df["close"].shift(1) < df["close"].shift(2))

        # ── RSI 背离检测 (简化版: price vs RSI 方向不一致) ──
        df["price_vs_4"] = df["close"] - df["close"].shift(4)
        df["rsi_vs_4"] = df["rsi"] - df["rsi"].shift(4)
        df["rsi_bull_div"] = (df["price_vs_4"] < 0) & (df["rsi_vs_4"] > 0)
        df["rsi_bear_div"] = (df["price_vs_4"] > 0) & (df["rsi_vs_4"] < 0)

        # ── MACD 背离 ──
        df["macd_hist_vs_2"] = df["macd_hist"] - df["macd_hist"].shift(2)
        df["price_vs_5"] = df["close"] - df["close"].shift(5)
        df["macd_bull_div"] = (df["price_vs_5"] < 0) & (df["macd_hist_vs_2"] > 0)
        df["macd_bear_div"] = (df["price_vs_5"] > 0) & (df["macd_hist_vs_2"] < 0)

        # ── 布林带宽度 ──
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

        # ── EMA 交叉 ──
        ema_f = int(self.ema_fast.value)
        ema_s = int(self.ema_slow.value)
        df[f"ema{ema_f}"] = ta.EMA(df, timeperiod=ema_f)
        df[f"ema{ema_s}"] = ta.EMA(df, timeperiod=ema_s)
        df[f"ema{ema_f}_cross_up"] = (
            (df[f"ema{ema_f}"].shift(1) <= df[f"ema{ema_s}"].shift(1)) &
            (df[f"ema{ema_f}"] > df[f"ema{ema_s}"])
        )
        df[f"ema{ema_f}_cross_down"] = (
            (df[f"ema{ema_f}"].shift(1) >= df[f"ema{ema_s}"].shift(1)) &
            (df[f"ema{ema_f}"] < df[f"ema{ema_s}"])
        )

        # ── 动量 EMA ──
        df["momentum_ema"] = ta.EMA(df, timeperiod=20)

        return df

    # ════════════════════════════════════════════
    # 入场信号 (6 策略)
    # ════════════════════════════════════════════
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        生成入场信号 — 对应原 scan_single_symbol() 信号初筛
        6 策略按优先级: pullback > momentum > ema_cross > bollinger > counter_trend
        """
        df = dataframe.copy()

        # 初始化信号列
        df["enter_long"] = 0
        df["enter_short"] = 0
        df["enter_tag"] = ""

        # ═══════════════════════════════════════
        # 策略 1: pullback — 趋势回调 — v3: 平衡
        # v1=6笔(太少), v2=578笔(太多)
        # v3: RSI+3 微调 + 量确认 + 趋势强度
        # ═══════════════════════════════════════
        pullback_long = (
            (df["close"] > df["ema9"]) &
            (df["rsi"] < (self.rsi_oversold.value + 3)) &
            (df["adx"] > 12) &
            (df["volume"] > df["volume_mean_20"] * 0.8)  # 量不能太低
        )
        pullback_short = (
            (df["close"] < df["ema9"]) &
            (df["rsi"] > (self.rsi_overbought.value - 3)) &
            (df["adx"] > 12) &
            (df["volume"] > df["volume_mean_20"] * 0.8)
        )

        pullback_long &= ~df["is_bearish_trend"]
        pullback_short &= ~df["is_bullish_trend"]

        # ═══════════════════════════════════════
        # 策略 2: momentum — 动量突破 — v3: 平衡
        # v1=385笔(34.5%), v2=6笔(ADX>30太严)
        # v3: ADX>26 + 量确认 → 适中
        # ═══════════════════════════════════════
        momentum_rsi_min = int(self.momentum_rsi_min.value)
        momentum_rsi_max = int(self.momentum_rsi_max.value)

        momentum_long = (
            (df["close"] > df["ema50"]) &
            (df["rsi"] > momentum_rsi_min) &
            (df["rsi"] < momentum_rsi_max) &
            (df["adx"] > 26) &
            (df["close"] > df["momentum_ema"]) &
            (df["close"] >= df["recent_high_10"] * 0.999) &
            (df["volume"] > df["volume_mean_20"])
        )
        momentum_short = (
            (df["close"] < df["ema50"]) &
            (df["rsi"] < (100 - momentum_rsi_min)) &
            (df["rsi"] > (100 - momentum_rsi_max)) &
            (df["adx"] > 26) &
            (df["close"] < df["momentum_ema"]) &
            (df["close"] <= df["recent_low_10"] * 1.001) &
            (df["volume"] > df["volume_mean_20"])
        )

        # ═══════════════════════════════════════
        # 策略 3: ema_cross — EMA 交叉 — v2: 大幅收紧
        # v1 产生了 436 笔交易 (47%), 胜率 41.5%, 是最大亏损源
        # v2: 加 ADX+RSI+MACD 三重确认, 减少假交叉
        # ═══════════════════════════════════════
        ema_cross_long = (
            df[f"ema{int(self.ema_fast.value)}_cross_up"] &
            (df["adx"] > 18) &  # v2: 需要一定趋势
            (df["close"] > df["ema200"]) &  # 必须在长期均线上方
            (df["rsi"] < 65) &  # 不超买
            (df["rsi"] > 35) &  # 不太弱
            (df["macd_hist"] > df["macd_hist"].shift(1))  # MACD动量增强
        )
        ema_cross_short = (
            df[f"ema{int(self.ema_fast.value)}_cross_down"] &
            (df["adx"] > 18) &
            (df["close"] < df["ema200"]) &
            (df["rsi"] > 35) &  # 不超卖
            (df["rsi"] < 65) &
            (df["macd_hist"] < df["macd_hist"].shift(1))  # MACD动量减弱
        )

        # ═══════════════════════════════════════
        # 策略 4: bollinger — 布林带均值回归
        # 原逻辑: 触碰下轨 + RSI不极端 → LONG | 触碰上轨 + RSI不极端 → SHORT
        #         布林带收窄 <2% 跳过
        # ═══════════════════════════════════════
        bollinger_long = (
            (df["close"] <= df["bb_lower"]) &
            (df["rsi"] < 45) & (df["rsi"] > 20) &
            (df["bb_width"] > 0.02)
        )
        bollinger_short = (
            (df["close"] >= df["bb_upper"]) &
            (df["rsi"] > 55) & (df["rsi"] < 80) &
            (df["bb_width"] > 0.02)
        )

        # ═══════════════════════════════════════
        # 策略 5: counter_trend — 逆势极端反转 — v3: 更严格
        # v1=84笔, v2=331笔(太松了)
        # v3: 收紧 RSI 阈值 + 布林带极端确认
        # ═══════════════════════════════════════
        counter_long = (
            df["is_bearish_trend"] &
            (df["rsi"] < (self.rsi_oversold.value * 0.55)) &  # v2=0.7, v3=0.55 更严格
            (df["rsi"] > 12) &
            (df["close"] < df["bb_lower"])  # 价格已跌破下轨
        )
        counter_short = (
            df["is_bullish_trend"] &
            (df["rsi"] > (self.rsi_overbought.value * 1.3)) &  # v2=1.2, v3=1.3
            (df["rsi"] < 88) &
            (df["close"] > df["bb_upper"])  # 价格已突破上轨
        )

        # ═══════════════════════════════════════
        # 策略 6: grid — 低波动震荡 (简化版)
        # 原逻辑: ADX < 25 (趋势市跳过), 布林带宽度适中 → 做震荡
        # Freqtrade 中实现为特殊条件的 bollinger 入场
        # ═══════════════════════════════════════
        grid_long = (
            (df["close"] <= df["bb_lower"]) &
            (df["rsi"] < 40) & (df["rsi"] > 25) &
            (df["adx"] < 25) &  # 低波动确认
            (df["bb_width"] > 0.015) & (df["bb_width"] < 0.06)  # 宽度适中
        )
        grid_short = (
            (df["close"] >= df["bb_upper"]) &
            (df["rsi"] > 60) & (df["rsi"] < 75) &
            (df["adx"] < 25) &
            (df["bb_width"] > 0.015) & (df["bb_width"] < 0.06)
        )

        # ═══════════════════════════════════════
        # 按优先级合并入场信号 (防止一个 bar 上多个信号重复进场)
        # 优先级: pullback > momentum > ema_cross > bollinger > counter_trend > grid
        # 使用掩码叠加实现: 高优先级先设置，低优先级只在无更高优先级信号时才设置
        # ═══════════════════════════════════════

        # ── 构建 LONG 已使用掩码 (已触发更高优先级信号的 bar) ──
        used_long = pullback_long.copy()
        df.loc[pullback_long, "enter_long"] = 1
        df.loc[pullback_long, "enter_tag"] = "pullback"

        new_long = ~used_long & momentum_long
        df.loc[new_long, "enter_long"] = 1
        df.loc[new_long, "enter_tag"] = "momentum"
        used_long |= new_long

        new_long = ~used_long & ema_cross_long
        df.loc[new_long, "enter_long"] = 1
        df.loc[new_long, "enter_tag"] = "ema_cross"
        used_long |= new_long

        new_long = ~used_long & bollinger_long
        df.loc[new_long, "enter_long"] = 1
        df.loc[new_long, "enter_tag"] = "bollinger"
        used_long |= new_long

        new_long = ~used_long & counter_long
        df.loc[new_long, "enter_long"] = 1
        df.loc[new_long, "enter_tag"] = "counter_trend"
        used_long |= new_long

        new_long = ~used_long & grid_long
        df.loc[new_long, "enter_long"] = 1
        df.loc[new_long, "enter_tag"] = "grid"

        # ── 构建 SHORT 已使用掩码 ──
        used_short = pullback_short.copy()
        df.loc[pullback_short, "enter_short"] = 1
        df.loc[pullback_short, "enter_tag"] = df.loc[pullback_short, "enter_tag"].mask(
            df["enter_tag"] == "", "pullback")

        new_short = ~used_short & momentum_short
        df.loc[new_short, "enter_short"] = 1
        df.loc[new_short, "enter_tag"] = df.loc[new_short, "enter_tag"].mask(
            df["enter_tag"] == "", "momentum")
        used_short |= new_short

        new_short = ~used_short & ema_cross_short
        df.loc[new_short, "enter_short"] = 1
        df.loc[new_short, "enter_tag"] = df.loc[new_short, "enter_tag"].mask(
            df["enter_tag"] == "", "ema_cross")
        used_short |= new_short

        new_short = ~used_short & bollinger_short
        df.loc[new_short, "enter_short"] = 1
        df.loc[new_short, "enter_tag"] = df.loc[new_short, "enter_tag"].mask(
            df["enter_tag"] == "", "bollinger")
        used_short |= new_short

        new_short = ~used_short & counter_short
        df.loc[new_short, "enter_short"] = 1
        df.loc[new_short, "enter_tag"] = df.loc[new_short, "enter_tag"].mask(
            df["enter_tag"] == "", "counter_trend")
        used_short |= new_short

        new_short = ~used_short & grid_short
        df.loc[new_short, "enter_short"] = 1
        df.loc[new_short, "enter_tag"] = df.loc[new_short, "enter_tag"].mask(
            df["enter_tag"] == "", "grid")

        return df

    # ════════════════════════════════════════════
    # 出场信号
    # ════════════════════════════════════════════
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        出场信号 — 主要靠 stoploss/ROI/trailing 处理
        这里只放需要立即出场的条件信号
        """
        df = dataframe.copy()

        df["exit_long"] = 0
        df["exit_short"] = 0
        df["exit_tag"] = ""

        # RSI 极端反转信号 (非主要出场方式，止损/止盈/ROI为主)
        # 做多出场: RSI 超买 + MACD 转空
        exit_long_signal = (
            (df["rsi"] > 75) &
            (df["macd_hist"] < 0) &
            (df["macd"] < df["macd_signal"])
        )
        df.loc[exit_long_signal, "exit_long"] = 1
        df.loc[exit_long_signal, "exit_tag"] = "rsi_overbought"

        # 做空出场: RSI 超卖 + MACD 转多
        exit_short_signal = (
            (df["rsi"] < 25) &
            (df["macd_hist"] > 0) &
            (df["macd"] > df["macd_signal"])
        )
        df.loc[exit_short_signal, "exit_short"] = 1
        df.loc[exit_short_signal, "exit_tag"] = "rsi_oversold"

        return df

    # ════════════════════════════════════════════
    # 自定义出场 (僵尸仓/浮亏止损)
    # ════════════════════════════════════════════
    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs) -> str | None:
        """
        对应原 bot 的出场优先规则引擎:
          - ROI > 40% → "take_profit_surge"
          - ROI < -30% + 持仓 > 1h → "floating_loss"
          - 持仓 > 4h + |ROI| < 2% → "stale_zombie"

        Freqtrade ROI 表已覆盖 ROI>40% 的情况，这里补充后两种。
        """

        # ── 浮亏止损: ROI < -25% 且持仓 > 1h ──
        if current_profit < -0.25:
            hours = (current_time - trade.open_date_utc).total_seconds() / 3600
            if hours > 1.0:
                return "floating_loss_timeout"

        # ── 僵尸仓退出: 持仓 > 1.5h 且 |ROI| < 1% (v3: 2h→1.5h) ──
        hours = (current_time - trade.open_date_utc).total_seconds() / 3600
        if hours > 1.5 and abs(current_profit) < 0.01:
            return "stale_zombie"

        return None

    # ════════════════════════════════════════════
    # 自定义止损 (波动熔断)
    # ════════════════════════════════════════════
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs) -> float | None:
        """
        v4.1 波动熔断: ATR > 2.5x 均值 → SL 收紧到 1.0x
        对应原 bot 保护层 #4
        """
        if after_fill:
            # 新开仓: 使用默认 stoploss (-30%)
            return None

        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and len(dataframe) > 14:
                last_candle = dataframe.iloc[-1]
                atr_now = last_candle.get("atr", 0)
                atr_mean = dataframe["atr"].tail(50).mean()
                if atr_now > atr_mean * 2.5 and atr_mean > 0:
                    # 波动率飙升 → 收紧止损到 -15%
                    return -0.15
        except Exception:
            pass

        return None  # 使用默认 stoploss

    # ════════════════════════════════════════════
    # 自定义仓位 (ADX 动态 + counter_trend 半仓)
    # ════════════════════════════════════════════
    def custom_stake_amount(self, pair: str, current_time: datetime,
                            current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float,
                            leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        """
        对应原 bot ADX 动态仓位 + counter_trend 半仓策略
        """
        stake = proposed_stake

        if entry_tag == "counter_trend":
            # 逆势半仓
            stake = proposed_stake * 0.5

        # ADX 动态仓位: 趋势越强仓位越大
        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is not None and len(dataframe) > 0:
                adx = dataframe["adx"].iloc[-1]
                if not np.isnan(adx):
                    if adx > 35:
                        stake = min(stake * 1.2, max_stake)  # 强趋势加仓 20%
                    elif adx < 18:
                        stake = stake * 0.7  # 弱趋势减仓 30%
        except Exception:
            pass

        return max(min_stake or 0, min(stake, max_stake))

    # ════════════════════════════════════════════
    # 信号确认 (时段过滤 + 周末保护)
    # ════════════════════════════════════════════
    def custom_entry_signal(self, pair: str, current_time: datetime,
                            proposed_rate: float, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        """
        对应原 bot:
          - 周末凌晨保护 (保护层 #10): 低流动性时段只平仓不开仓
          - 信号置信度相当于"有 entry_tag 则通过"
        """
        # 周末凌晨保护 (周五 22:00 ~ 周一 04:00 UTC → 北京时间 周六 06:00 ~ 周一 12:00)
        # 简化: UTC 周五 20:00 ~ 周日 20:00 判定为低流动性
        weekday = current_time.weekday()
        hour = current_time.hour
        is_weekend_low = (
            (weekday == 4 and hour >= 20) or  # 周五晚
            (weekday == 5) or                  # 周六全天
            (weekday == 6 and hour < 20)       # 周日白天
        )
        if is_weekend_low and entry_tag != "counter_trend":
            # 周末只允许 counter_trend (极端反转有更高安全边际)
            return False

        return True  # 默认通过
