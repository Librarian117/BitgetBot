#!/usr/bin/env python3
"""tests/test_filter_pipeline_parity.py — P3 Commit 3: regime + kalman parity"""

import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filter_pipeline import resolve_kalman, detect_market_regime


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


if __name__ == "__main__":
    unittest.main()
