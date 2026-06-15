#!/usr/bin/env python3
"""tests/test_filter_pipeline_parity.py — P3 Commit 3: regime + kalman parity"""

import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filter_pipeline import (resolve_kalman, detect_market_regime,
                            filter_volume, adjust_direction_bias,
                            filter_btc_linkage)


class TestKalmanResolver(unittest.TestCase):

    def test_allow_no_conflict(self):
        r = resolve_kalman("LONG", "pullback", "up", 0.1, False, False, True)
        self.assertEqual(r["action"], "allow")
        self.assertEqual(r["confidence_penalty"], 0)

    def test_strong_conflict_downgrade(self):
        """kalman_dir=down vs LONG signal, score>0.8 → downgrade"""
        r = resolve_kalman("LONG", "pullback", "down", 0.9, False, False, True)
        self.assertEqual(r["action"], "downgrade")
        self.assertEqual(r["new_strategy"], "counter_trend")
        self.assertTrue(r["kalman_conflict_for_scorer"])
        self.assertGreater(r["confidence_penalty"], 0)

    def test_medium_conflict_downgrade(self):
        """score>0.5 → downgrade"""
        r = resolve_kalman("SHORT", "pullback", "up", 0.6, False, True, False)
        self.assertEqual(r["action"], "downgrade")
        self.assertEqual(r["new_strategy"], "counter_trend")

    def test_weak_conflict_penalize(self):
        """score>0.2 → penalize only"""
        r = resolve_kalman("LONG", "pullback", "down", 0.3, False, False, True)
        self.assertEqual(r["action"], "penalize")
        self.assertIsNone(r["new_strategy"])

    def test_noise_ignored(self):
        """score<=0.2 → ignored"""
        r = resolve_kalman("LONG", "pullback", "down", 0.15, False, False, True)
        self.assertEqual(r["action"], "allow")

    def test_ema_conflict_pullback_downgrade(self):
        """EMA-Kalman conflict, pullback, goes against EMA → downgrade"""
        r = resolve_kalman("LONG", "pullback", "flat", 0.0, True, True, False)
        self.assertEqual(r["action"], "downgrade")
        self.assertEqual(r["new_strategy"], "counter_trend")

    def test_ema_conflict_strong_kalman_penalty(self):
        """EMA-Kalman conflict with strong kalman → penalty applied"""
        r = resolve_kalman("LONG", "pullback", "flat", 0.8, True, True, True)
        self.assertEqual(r["action"], "downgrade")
        self.assertGreater(r["confidence_penalty"], 0)

    def test_flat_kalman_returns_allow(self):
        r = resolve_kalman("LONG", "pullback", "flat", 0.1, False, False, False)
        self.assertEqual(r["action"], "allow")


class TestRegimeDetection(unittest.TestCase):

    def _tf(self, trends):
        return {tf: {"trend": t, "regime": "trending"} for tf, t in trends.items()}

    def test_strong_bull(self):
        tf = self._tf({"1h": "bullish", "4h": "bullish"})
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=30.0)
        self.assertEqual(r["regime"], "strong_bull")
        self.assertIn("momentum", r["recommended"])

    def test_bull(self):
        tf = self._tf({"1h": "bullish", "4h": "bullish"})
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=15.0)
        self.assertEqual(r["regime"], "bull")

    def test_range(self):
        tf = self._tf({"1h": "bullish", "4h": "bearish"})
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=15.0)
        self.assertEqual(r["regime"], "range")

    def test_bear(self):
        tf = self._tf({"1h": "bearish", "4h": "bearish"})
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=15.0)
        self.assertEqual(r["regime"], "bear")

    def test_strong_bear(self):
        tf = self._tf({"1h": "bearish", "4h": "bearish"})
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=30.0)
        self.assertEqual(r["regime"], "strong_bear")

    def test_panic(self):
        tf = self._tf({"1h": "bullish"})
        r = detect_market_regime(tf, ["1h"], atr=3.0, close=100.0, adx=15.0)
        self.assertEqual(r["regime"], "panic")
        self.assertEqual(r["recommended"], [])

    def test_allowed_long_blocked_by_bearish_tf(self):
        tf = self._tf({"1h": "bearish"})
        r = detect_market_regime(tf, ["1h"], atr=0.5, close=100.0, adx=15.0)
        self.assertNotIn("LONG", r["allowed_directions"])

    def test_allowed_short_blocked_by_bullish_tf(self):
        tf = self._tf({"1h": "bullish"})
        r = detect_market_regime(tf, ["1h"], atr=0.5, close=100.0, adx=15.0)
        self.assertNotIn("SHORT", r["allowed_directions"])

    def test_ranging_tf_does_not_block(self):
        tf = {"1h": {"trend": "bearish", "regime": "ranging"}}
        r = detect_market_regime(tf, ["1h"], atr=0.5, close=100.0, adx=15.0)
        self.assertIn("LONG", r["allowed_directions"])

    def test_markov_bullish_upgrade(self):
        tf = self._tf({"1h": "bearish", "4h": "bearish"})
        mr = {"confidence": 50, "signal": 0.5}
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=15.0,
                                 markov_result=mr)
        # bear + markov_bias > 0.3 → upgrade to range
        self.assertIn(r["regime"], ("range", "bull"))

    def test_markov_bearish_downgrade(self):
        tf = self._tf({"1h": "bullish", "4h": "bullish"})
        mr = {"confidence": 50, "signal": -0.5}
        r = detect_market_regime(tf, ["1h", "4h"], atr=0.5, close=100.0, adx=15.0,
                                 markov_result=mr)
        # bull + markov_bias < -0.3 → downgrade to range
        self.assertIn(r["regime"], ("range", "bear"))

    def test_return_dict_has_all_keys(self):
        r = detect_market_regime({}, [], close=100.0)
        for k in ("regime", "direction", "volatility", "trend_strength",
                  "atr_pct", "consensus", "recommended", "allowed_directions",
                  "confidence", "detail", "markov_bias"):
            self.assertIn(k, r)


class TestVolumeFilter(unittest.TestCase):
    """filter_volume matches original inline logic"""

    def _df_vol(self, volumes):
        import pandas as pd
        n = len(volumes)
        return pd.DataFrame({"open": [100]*n, "high": [100]*n, "low": [100]*n,
                             "close": [100]*n, "volume": volumes})

    def test_sandbox_always_passes(self):
        df = self._df_vol([100]*30)
        r = filter_volume(df, -2, {"vol_ratio": 1.5}, is_sandbox=True)
        self.assertTrue(r["passed"])
        self.assertEqual(r["vol_ratio"], 1.0)

    def test_live_pass_when_ratio_high(self):
        volumes = [50]*18 + [200]*2 + [300]*10  # cur vol >> avg vol
        df = self._df_vol(volumes)
        r = filter_volume(df, -2, {"vol_ratio": 1.5}, is_sandbox=False)
        self.assertTrue(r["passed"])

    def test_live_block_when_ratio_low(self):
        volumes = [500]*20 + [50]*8 + [50, 50]  # cur=50, avg~350, ratio<0.3
        df = self._df_vol(volumes)
        r = filter_volume(df, -2, {"vol_ratio": 1.5}, is_sandbox=False)
        self.assertFalse(r["passed"])
        self.assertLess(r["vol_ratio"], 1.5)

    def test_live_pass_ratio_at_threshold(self):
        volumes = [100]*30
        df = self._df_vol(volumes)
        r = filter_volume(df, -2, {"vol_ratio": 0.5}, is_sandbox=False)
        self.assertTrue(r["passed"])


class TestDirectionBias(unittest.TestCase):
    """adjust_direction_bias matches original inline logic"""

    def test_bearish_bias(self):
        r = adjust_direction_bias(True, False, False, 30.0, 70.0)
        self.assertTrue(r["block_long"])
        self.assertFalse(r["block_short"])
        self.assertEqual(r["effective_overbought"], 60.0)  # 70-10
        self.assertEqual(r["effective_oversold"], 25.0)     # 30-5

    def test_bullish_bias(self):
        r = adjust_direction_bias(False, True, False, 30.0, 70.0)
        self.assertFalse(r["block_long"])
        self.assertTrue(r["block_short"])
        self.assertEqual(r["effective_oversold"], 40.0)     # 30+10
        self.assertEqual(r["effective_overbought"], 75.0)    # 70+5

    def test_neutral_no_bias(self):
        r = adjust_direction_bias(False, False, False, 30.0, 70.0)
        self.assertFalse(r["block_long"])
        self.assertFalse(r["block_short"])
        self.assertEqual(r["effective_oversold"], 30.0)
        self.assertEqual(r["effective_overbought"], 70.0)

    def test_kalman_conflict_unblocks(self):
        """Kalman conflict -> no hard block even in bearish/bullish"""
        r = adjust_direction_bias(True, False, True, 30.0, 70.0)
        self.assertFalse(r["block_long"])  # unblocked by kalman conflict
        self.assertFalse(r["block_short"])

    def test_boundary_floor_ceil(self):
        """RSI adjustments don't exceed floor/ceiling"""
        r = adjust_direction_bias(True, False, False, 10.0, 40.0)
        self.assertEqual(r["effective_overbought"], 45.0)  # floor 45
        self.assertEqual(r["effective_oversold"], 10.0)     # floor 10
        r2 = adjust_direction_bias(False, True, False, 50.0, 80.0)
        self.assertEqual(r2["effective_oversold"], 55.0)     # ceiling 55
        self.assertEqual(r2["effective_overbought"], 85.0)    # ceiling 85


class TestBtcLinkage(unittest.TestCase):
    """filter_btc_linkage matches original inline logic"""

    def _cand(self, symbol, direction):
        return {"symbol": symbol, "direction": direction}

    def test_btc_always_passes(self):
        """BTC symbol itself passes regardless of btc_change"""
        cands = [self._cand("BTC/USDT", "LONG"), self._cand("BTC/USDT", "SHORT")]
        r = filter_btc_linkage(cands, -0.05, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 2)
        self.assertEqual(len(r["blocked"]), 0)

    def test_long_blocked_by_btc_drop(self):
        cands = [self._cand("ETH/USDT", "LONG")]
        r = filter_btc_linkage(cands, -0.05, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 0)
        self.assertEqual(len(r["blocked"]), 1)
        self.assertEqual(r["blocked"][0]["reason"], "btc_drop_block_long")

    def test_short_blocked_by_btc_pump(self):
        cands = [self._cand("ETH/USDT", "SHORT")]
        r = filter_btc_linkage(cands, 0.05, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 0)
        self.assertEqual(r["blocked"][0]["reason"], "btc_pump_block_short")

    def test_long_passes_when_btc_not_below(self):
        cands = [self._cand("ETH/USDT", "LONG")]
        r = filter_btc_linkage(cands, -0.01, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 1)

    def test_short_passes_when_btc_not_above(self):
        cands = [self._cand("ETH/USDT", "SHORT")]
        r = filter_btc_linkage(cands, 0.01, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 1)

    def test_btc_change_none_skips(self):
        cands = [self._cand("ETH/USDT", "LONG")]
        r = filter_btc_linkage(cands, None, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 1)
        self.assertEqual(len(r["blocked"]), 0)

    def test_multiple_candidates_preserve_order(self):
        cands = [self._cand("A/USDT", "LONG"), self._cand("B/USDT", "SHORT"),
                 self._cand("C/USDT", "LONG")]
        r = filter_btc_linkage(cands, 0.0, btc_drop_block_long=-0.03, btc_pump_block_short=0.03)
        self.assertEqual(len(r["passed"]), 3)
        self.assertEqual(r["passed"][0]["symbol"], "A/USDT")
        self.assertEqual(r["passed"][2]["symbol"], "C/USDT")


if __name__ == "__main__":
    unittest.main()
