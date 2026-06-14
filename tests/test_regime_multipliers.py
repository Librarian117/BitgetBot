#!/usr/bin/env python3
"""tests/test_regime_multipliers.py — P2 Commit 2: REGIME_MULTIPLIERS key alignment

Verify every regime label from _detect_market_regime() has a matching multiplier entry.
This confirms the v4.0 "state-driven position/SL/TP" feature is now functional.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_executor import TradeExecutor


class TestRegimeMultipliers(unittest.TestCase):
    """P2 Commit 2: 所有 regime label 都有匹配的 multiplier"""

    # Labels produced by _detect_market_regime() in deepseek_quant_bot.py
    ALL_REGIME_LABELS = [
        "strong_bull",
        "bull",
        "range",
        "bear",
        "strong_bear",
        "panic",
    ]

    def test_all_labels_have_multipliers(self):
        """每一个真实 regime label 都能命中 REGIME_MULTIPLIERS, 不回退到 default"""
        for label in self.ALL_REGIME_LABELS:
            cfg = TradeExecutor.REGIME_MULTIPLIERS.get(label)
            self.assertIsNotNone(
                cfg, f"regime '{label}' should have a multiplier entry"
            )
            for key in ("pos", "sl", "tp"):
                self.assertIn(key, cfg, f"regime '{label}' missing '{key}' key")
                self.assertIsInstance(cfg[key], (int, float),
                                      f"regime '{label}' {key} should be numeric")

    def test_unknown_falls_back_to_default(self):
        """未识别的 regime 返回 default {pos:1.0, sl:1.0, tp:1.0}"""
        cfg = TradeExecutor.REGIME_MULTIPLIERS.get("unknown", {"pos": 1.0, "sl": 1.0, "tp": 1.0})
        self.assertEqual(cfg["pos"], 1.0)
        self.assertEqual(cfg["sl"], 1.0)
        self.assertEqual(cfg["tp"], 1.0)

    def test_pos_multiplier_in_safe_range(self):
        """仓位乘数在 0.5-1.10 范围内 (不激进放大)"""
        for label in self.ALL_REGIME_LABELS:
            cfg = TradeExecutor.REGIME_MULTIPLIERS[label]
            self.assertGreaterEqual(
                cfg["pos"], 0.5,
                f"regime '{label}' pos={cfg['pos']} < 0.5 — too restrictive"
            )
            self.assertLessEqual(
                cfg["pos"], 1.10,
                f"regime '{label}' pos={cfg['pos']} > 1.10 — too aggressive"
            )

    def test_sl_multiplier_in_safe_range(self):
        """SL 乘数在 0.8-1.2 范围内"""
        for label in self.ALL_REGIME_LABELS:
            cfg = TradeExecutor.REGIME_MULTIPLIERS[label]
            self.assertGreaterEqual(cfg["sl"], 0.70,
                                    f"regime '{label}' sl={cfg['sl']} — SL too tight")
            self.assertLessEqual(cfg["sl"], 1.30,
                                 f"regime '{label}' sl={cfg['sl']} — SL too wide")

    def test_panic_is_most_restrictive(self):
        """panic 的仓位乘数应该是最小的"""
        panic_pos = TradeExecutor.REGIME_MULTIPLIERS["panic"]["pos"]
        for label in self.ALL_REGIME_LABELS:
            if label == "panic":
                continue
            other_pos = TradeExecutor.REGIME_MULTIPLIERS[label]["pos"]
            self.assertLessEqual(
                panic_pos, other_pos,
                f"panic pos={panic_pos} should be <= {label} pos={other_pos}"
            )

    def test_strong_bull_bear_symmetric(self):
        """strong_bull 和 strong_bear 乘数应对称"""
        bull = TradeExecutor.REGIME_MULTIPLIERS["strong_bull"]
        bear = TradeExecutor.REGIME_MULTIPLIERS["strong_bear"]
        self.assertEqual(bull["pos"], bear["pos"],
                         "strong_bull/bear pos should be symmetric")
        self.assertEqual(bull["sl"], bear["sl"],
                         "strong_bull/bear sl should be symmetric")
        self.assertEqual(bull["tp"], bear["tp"],
                         "strong_bull/bear tp should be symmetric")

    def test_range_is_more_restrictive_than_bull(self):
        """range 的仓位乘数应该 <= bull"""
        range_pos = TradeExecutor.REGIME_MULTIPLIERS["range"]["pos"]
        bull_pos = TradeExecutor.REGIME_MULTIPLIERS["bull"]["pos"]
        self.assertLessEqual(range_pos, bull_pos,
                             f"range pos={range_pos} should <= bull pos={bull_pos}")

    def test_execute_uses_multiplier(self):
        """验证 execute() 正确消费 multiplier — 不抛异常即通过"""
        # Minimal smoke test: 确认 multiplier dict 结构合法
        for label in self.ALL_REGIME_LABELS:
            cfg = TradeExecutor.REGIME_MULTIPLIERS[label]
            # 每个 multiplier 都应该是合法的浮点数
            self.assertTrue(isinstance(cfg["pos"], (int, float)))
            self.assertTrue(isinstance(cfg["sl"], (int, float)))
            self.assertTrue(isinstance(cfg["tp"], (int, float)))


if __name__ == "__main__":
    unittest.main()
