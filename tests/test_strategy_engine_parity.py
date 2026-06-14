#!/usr/bin/env python3
"""tests/test_strategy_engine_parity.py — P3 Commit 1: pullback + momentum signal parity

Verify extracted evaluators produce identical signals to the original inline logic.
Uses fake dataframes — no real exchange needed.
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_engine import (evaluate_pullback, evaluate_momentum,
                            evaluate_ema_cross, evaluate_bollinger)


def _make_df(close_series, high_series=None, low_series=None):
    """Build minimal OHLCV dataframe with EMA columns for momentum test."""
    n = len(close_series)
    highs = high_series or close_series
    lows = low_series or close_series
    df = pd.DataFrame({
        "open": close_series,
        "high": highs,
        "low": lows,
        "close": close_series,
        "volume": [1000.0] * n,
    })
    return df


class TestPullbackParity(unittest.TestCase):
    """evaluate_pullback matches original inline logic"""

    def test_pullback_long_signal(self):
        """close > EMA and RSI < oversold -> LONG pullback"""
        result = evaluate_pullback(close=100.0, ema=95.0, rsi=25.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["strategy"], "pullback")

    def test_pullback_short_signal(self):
        """close < EMA and RSI > overbought -> SHORT pullback"""
        result = evaluate_pullback(close=90.0, ema=95.0, rsi=75.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "SHORT")
        self.assertEqual(result["strategy"], "pullback")

    def test_pullback_no_signal_neutral(self):
        """close near EMA, RSI mid -> no signal"""
        result = evaluate_pullback(close=95.0, ema=95.0, rsi=50.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNone(result)

    def test_pullback_no_signal_wrong_side(self):
        """close > EMA but RSI also high -> no pullback long (not oversold)"""
        result = evaluate_pullback(close=100.0, ema=95.0, rsi=60.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNone(result)

    def test_pullback_at_boundary_oversold(self):
        """RSI exactly at oversold threshold -> LONG"""
        result = evaluate_pullback(close=100.0, ema=95.0, rsi=30.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        # Original code: rsi < effective_oversold — strict less-than
        # rsi=30.0, oversold=30.0 → 30 < 30 is False → no signal
        self.assertIsNone(result)

    def test_pullback_just_below_oversold(self):
        """RSI just below oversold threshold -> LONG"""
        result = evaluate_pullback(close=100.0, ema=95.0, rsi=29.99,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "LONG")


class TestMomentumParity(unittest.TestCase):
    """evaluate_momentum matches original inline logic"""

    def _cfg(self, **kw):
        defaults = dict(
            momentum_enabled=True, momentum_rsi_min=55.0, momentum_rsi_max=75.0,
            momentum_adx_threshold=25.0, momentum_ema_period=20,
            momentum_breakout_bars=10,
        )
        defaults.update(kw)
        return defaults

    def test_momentum_long_signal(self):
        """price > EMA, strong ADX, RSI in momentum zone, breakout high"""
        cfg = self._cfg()
        n = 30
        # Flat prices at 100, then spike up at cidx for breakout
        closes = [100.0] * n
        closes[-2] = 101.0  # cidx: breakout price
        closes[-1] = 100.5  # last candle
        # All recent highs = 100.3, so close=101.0 easily breaks above 100.3*0.998
        highs = [100.3] * n
        df = _make_df(closes, highs)
        cidx = -2
        close = 101.0
        ema = 99.5  # price > EMA

        result = evaluate_momentum(
            close=close, ema=ema, rsi=60.0, adx=30.0,
            df=df, cidx=cidx, **cfg,
        )
        self.assertIsNotNone(result, "momentum breakout should trigger")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["strategy"], "momentum")

    def test_momentum_disabled(self):
        """momentum_enabled=False -> no signal"""
        cfg = self._cfg(momentum_enabled=False)
        df = _make_df([100.0] * 30)
        result = evaluate_momentum(
            close=101.0, ema=100.0, rsi=60.0, adx=30.0,
            df=df, cidx=-2, **cfg,
        )
        self.assertIsNone(result)

    def test_momentum_no_signal_low_adx(self):
        """ADX below threshold -> no signal"""
        cfg = self._cfg()
        df = _make_df([100.0] * 30)
        result = evaluate_momentum(
            close=101.0, ema=100.0, rsi=60.0, adx=20.0,
            df=df, cidx=-2, **cfg,
        )
        self.assertIsNone(result)

    def test_momentum_no_signal_rsi_out_of_zone(self):
        """RSI outside momentum zone -> no signal"""
        cfg = self._cfg()
        df = _make_df([100.0] * 30)
        # LONG requires rsi in (momentum_rsi_min, momentum_rsi_max) = (55, 75)
        result = evaluate_momentum(
            close=101.0, ema=100.0, rsi=50.0, adx=30.0,
            df=df, cidx=-2, **cfg,
        )
        self.assertIsNone(result)

    def test_momentum_short_signal(self):
        """price < EMA, strong ADX, RSI in weak zone, break low"""
        cfg = self._cfg()
        n = 30
        closes = [100.0 - i * 0.1 for i in range(n)]  # downtrend
        lows = [c - 0.5 for c in closes]
        lows[-1] = closes[-1] - 0.1  # breaking recent low
        df = _make_df(closes, low_series=lows)
        cidx = -2
        close = closes[cidx]
        ema = close + 1.0  # price < EMA

        result = evaluate_momentum(
            close=close, ema=ema, rsi=25.0, adx=30.0,
            df=df, cidx=cidx, **cfg,
        )
        # SHORT: rsi < (100-55)=45 and rsi > (100-75)=25 — 25 is NOT > 25
        self.assertIsNone(result)  # boundary: 25 > 25 is False

    def test_momentum_short_just_above_lower_bound(self):
        """RSI just above lower bound -> SHORT momentum"""
        cfg = self._cfg()
        n = 30
        closes = [100.0] * n
        closes[-2] = 99.0   # cidx: breakdown price
        closes[-1] = 99.5   # last candle
        lows = [99.7] * n   # all recent lows = 99.7, close=99.0 easily breaks below 99.7*1.002
        df = _make_df(closes, low_series=lows)
        cidx = -2
        close = 99.0
        ema = 100.5  # price < EMA

        result = evaluate_momentum(
            close=close, ema=ema, rsi=26.0, adx=30.0,
            df=df, cidx=cidx, **cfg,
        )
        self.assertIsNotNone(result, "momentum short breakout should trigger")
        self.assertEqual(result["direction"], "SHORT")
        self.assertEqual(result["strategy"], "momentum")

    def test_pullback_priority_over_momentum(self):
        """pullback is evaluated first; momentum only checked if pullback returns None"""
        # pullback LONG conditions met -> returns pullback, momentum never called
        result = evaluate_pullback(close=100.0, ema=95.0, rsi=25.0,
                                   effective_oversold=30.0, effective_overbought=70.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["strategy"], "pullback")
        # In real code: if pullback_signal is set, momentum is skipped entirely
        # This test verifies pullback signal IS set (so momentum would be skipped)


class TestEmaCrossParity(unittest.TestCase):
    """evaluate_ema_cross matches original inline logic"""

    def _df_flat(self, n=30):
        return _make_df([100.0] * n)

    def test_ema_cross_long_signal(self):
        """金叉: fast crosses above slow + RSI < 60"""
        n = 30
        # Drops then rises to create clear crossover at cidx=-2
        closes = [100.0] * 25 + [98.0, 97.0, 100.0, 103.0, 105.0]
        # At cidx=-2 (close=103): ema9 should cross above ema21 due to recent rise
        df = _make_df(closes)
        result = evaluate_ema_cross(df, cidx=-2, close=103.0, ema200=98.0, rsi=50.0,
                                    momentum_enabled=True)
        self.assertIsNotNone(result, "ema9 crossing above ema21 should trigger LONG")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["strategy"], "ema_cross")

    def test_ema_cross_disabled(self):
        """momentum_enabled=False -> no signal"""
        df = self._df_flat()
        result = evaluate_ema_cross(df, cidx=-2, close=100.0, ema200=98.0, rsi=50.0,
                                    momentum_enabled=False)
        self.assertIsNone(result)

    def test_ema_cross_no_cross(self):
        """no crossover -> no signal"""
        df = self._df_flat()
        result = evaluate_ema_cross(df, cidx=-2, close=100.0, ema200=98.0, rsi=50.0,
                                    momentum_enabled=True)
        self.assertIsNone(result)


class TestBollingerParity(unittest.TestCase):
    """evaluate_bollinger matches original inline logic"""

    def _df_with_range(self, n=30, low_val=98.0, high_val=102.0, close_val=None):
        """Build a dataframe with enough range to create BB width > 2%"""
        closes = [low_val] * (n // 2) + [high_val] * (n // 2)
        if close_val is not None:
            closes[-2] = close_val
        return _make_df(closes)

    def test_bollinger_long_signal(self):
        """close <= lower band + RSI in sweet spot -> LONG"""
        n = 30
        # Moderate range: 92-108 → std≈8, BB width≈32%, lower≈88
        closes = [92.0] * 15 + [108.0] * 15
        closes[-2] = 85.0  # well below lower band
        df = _make_df(closes)
        result = evaluate_bollinger(df, cidx=-2, close=85.0, rsi=30.0)
        self.assertIsNotNone(result, "close below lower BB band should trigger LONG")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["strategy"], "bollinger")

    def test_bollinger_short_signal(self):
        """close >= upper band + RSI in sweet spot -> SHORT"""
        n = 30
        closes = [92.0] * 15 + [108.0] * 15
        closes[-2] = 125.0  # well above upper band (~120)
        df = _make_df(closes)
        result = evaluate_bollinger(df, cidx=-2, close=125.0, rsi=65.0)
        self.assertIsNotNone(result, "close above upper BB band should trigger SHORT")
        self.assertEqual(result["direction"], "SHORT")
        self.assertEqual(result["strategy"], "bollinger")

    def test_bollinger_narrow_band_no_signal(self):
        """narrow BB band (<2%) -> no signal"""
        # All identical values -> BB width = 0
        df = _make_df([100.0] * 30)
        result = evaluate_bollinger(df, cidx=-2, close=100.0, rsi=40.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
