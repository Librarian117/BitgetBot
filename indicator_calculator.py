#!/usr/bin/env python3
"""
indicator_calculator.py — 本地量化指标计算 (Phase 1 提取自 deepseek_quant_bot.py)

纯 Python + pandas 本地计算；不依赖交易所 API 的指标数据。

指标:
  - EMA(N)     指数移动均线
  - RSI(14)    相对强弱指标 (Wilder smoothing)
  - ATR(14)    平均真实波幅 (Wilder smoothing)
  - ADX(14)    平均趋向指数 (Wilder smoothing)
  - MACD(12,26,9)
  - Bollinger Bands(20,2)
"""

import numpy as np
import pandas as pd
from typing import Optional

from config_manager import ConfigManager


class IndicatorCalculator:
    """
    纯 Python + pandas 本地计算；
    不依赖交易所 API 的指标数据。

    指标：
      - EMA(N)     指数移动均线
      - RSI(14)    相对强弱指标 (Wilder smoothing)
      - ATR(14)    平均真实波幅 (Wilder smoothing)
      - ADX(14)    平均趋向指数 (Wilder smoothing)
    """

    def __init__(self, config: ConfigManager):
        self.ema_period = config.ema_period
        self.rsi_period = config.rsi_period
        self.atr_period = config.atr_period
        self.adx_period = config.adx_period

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """输入 OHLCV DataFrame，追加 ema, rsi, atr, adx, macd, bb 列"""
        df = df.copy()
        close = df["close"]

        df["ema"] = close.ewm(span=self.ema_period, adjust=False).mean()
        df["rsi"] = self._wilder_rsi(close, self.rsi_period)
        df["atr"] = self._wilder_atr(df, self.atr_period)
        df["adx"] = self._wilder_adx(df, self.adx_period)

        # ── v3.2: MACD (12, 26, 9) ──
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        # ── v3.2: Bollinger Bands (20, 2) ──
        bb_mean = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        df["bb_upper"] = bb_mean + 2 * bb_std
        df["bb_lower"] = bb_mean - 2 * bb_std
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / bb_mean  # 带宽

        return df

    def compute_tf_indicators(self, df: pd.DataFrame, ema_period: int = 50) -> pd.DataFrame:
        """为多时间周期计算简化指标 (EMA50 + ATR + ADX)"""
        df = df.copy()
        close = df["close"]

        df["ema"] = close.ewm(span=ema_period, adjust=False).mean()
        df["atr"] = self._wilder_atr(df, self.atr_period)
        df["adx"] = self._wilder_adx(df, self.adx_period)

        return df

    @staticmethod
    def _wilder_adx(df: pd.DataFrame, period: int) -> pd.Series:
        """Wilder's ADX — 平均趋向指数"""
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

        smooth_plus_dm = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        smooth_minus_dm = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

        plus_di = 100.0 * smooth_plus_dm / atr.replace(0, np.nan)
        minus_di = 100.0 * smooth_minus_dm / atr.replace(0, np.nan)

        di_sum = plus_di + minus_di
        dx = (plus_di - minus_di).abs() / di_sum.replace(0, np.nan) * 100.0

        adx = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        return adx

    @staticmethod
    def _wilder_rsi(series: pd.Series, period: int) -> pd.Series:
        """Wilder's RSI"""
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
        """Wilder's ATR"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        return atr
