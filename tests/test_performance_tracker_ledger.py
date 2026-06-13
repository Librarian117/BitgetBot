#!/usr/bin/env python3
"""tests/test_performance_tracker_ledger.py — WS1 Commit 1 验收测试

验证:
  1. load_trades_from_ledger() 可从 trade_ledger.jsonl 重建 trades
  2. 坏行/缺字段/非 TRADE_CLOSE 记录被安全跳过
  3. get_metrics_from_ledger() 可独立计算 metrics
  4. 空 ledger / 缺失 ledger 安全返回
  5. 现有 record_trade() / _load() 行为不受影响
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from performance_tracker import PerformanceTracker


class TestLedgerRebuild(unittest.TestCase):
    """WS1 Commit 1: Ledger 重建能力 (additive, 不切换 source)"""

    @classmethod
    def setUpClass(cls):
        cls.ledger_path = tempfile.mktemp(suffix=".jsonl")

    def _write_ledger(self, lines):
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            for line in lines:
                if isinstance(line, str):
                    f.write(line + "\n")
                else:
                    f.write(json.dumps(line) + "\n")

    # ── 正常路径 ──

    def test_load_trades_normal(self):
        """3 笔 TRADE_CLOSE + 噪音 → 只返回 3 笔"""
        self._write_ledger([
            "this is not json",
            {"event": "CYCLE", "cycle": 1},
            {
                "event": "TRADE_CLOSE", "trade_id": "t1",
                "symbol": "BTC", "side": "LONG", "strategy": "pullback",
                "entry": 65000.0, "exit": 65500.0, "pnl": 12.34, "pnl_pct": 3.21,
                "pnl_source": "API", "fee": 0.12, "funding": 0.0, "net_profit": 12.22,
                "close_reason": "RULE_TP", "open_ts": 1710000000,
                "close_ts": 1710003600, "duration_min": 60.0,
            },
            {
                "event": "TRADE_CLOSE", "trade_id": "t2",
                "symbol": "ETH", "side": "SHORT", "strategy": "momentum",
                "entry": 3000.0, "exit": 2950.0, "pnl": -8.50, "pnl_pct": -2.83,
                "pnl_source": "API", "fee": 0.09, "funding": -0.01, "net_profit": -8.60,
                "close_reason": "RULE_SL", "open_ts": 1710001000,
                "close_ts": 1710004600, "duration_min": 60.0,
            },
            {
                "event": "TRADE_CLOSE", "trade_id": "t3",
                "symbol": "SOL", "side": "LONG", "strategy": "pullback",
                "entry": 80.0, "exit": 82.0, "pnl": 5.00, "pnl_pct": 6.25,
                "pnl_source": "FALLBACK", "fee": 0.02, "funding": 0.0,
                "net_profit": 0, "close_reason": "DETECTED",
                "open_ts": 1710002000, "close_ts": 1710005600, "duration_min": 60.0,
            },
            "also bad json?",
        ])
        trades = PerformanceTracker.load_trades_from_ledger(self.ledger_path)
        self.assertEqual(len(trades), 3)

    def test_field_mapping(self):
        """Ledger 字段正确映射到 performance trade 字段"""
        self._write_ledger([{
            "event": "TRADE_CLOSE", "trade_id": "t1",
            "symbol": "BTC", "side": "LONG", "strategy": "pullback",
            "entry": 65000.0, "exit": 65500.0, "pnl": 12.34, "pnl_pct": 3.21,
            "pnl_source": "API", "fee": 0.12, "funding": 0.0,
            "close_reason": "RULE_TP", "open_ts": 1710000000,
            "close_ts": 1710003600, "duration_min": 60.0,
        }])
        trades = PerformanceTracker.load_trades_from_ledger(self.ledger_path)
        t = trades[0]
        self.assertEqual(t["symbol"], "BTC")
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["strategy"], "pullback")
        self.assertAlmostEqual(t["entry"], 65000.0)
        self.assertAlmostEqual(t["exit"], 65500.0)
        self.assertAlmostEqual(t["pnl"], 12.34)
        self.assertAlmostEqual(t["pnl_pct"], 3.21)
        self.assertTrue(t["win"])
        self.assertNotEqual(t["timestamp"], "")
        self.assertNotEqual(t["date"], "")

    def test_metrics_from_ledger(self):
        """get_metrics_from_ledger 产出正确的指标"""
        self._write_ledger([
            {"event": "TRADE_CLOSE", "trade_id": "w1",
             "symbol": "A", "side": "LONG", "pnl": 10.0, "pnl_pct": 5.0,
             "entry": 100.0, "exit": 110.0, "close_ts": 1710000000,
             "pnl_source": "API", "fee": 0.1, "funding": 0.0,
             "close_reason": "TP", "open_ts": 1700000000, "duration_min": 100.0},
            {"event": "TRADE_CLOSE", "trade_id": "w2",
             "symbol": "B", "side": "SHORT", "pnl": 5.0, "pnl_pct": 2.5,
             "entry": 200.0, "exit": 195.0, "close_ts": 1710001000,
             "pnl_source": "API", "fee": 0.1, "funding": 0.0,
             "close_reason": "TP", "open_ts": 1700001000, "duration_min": 50.0},
            {"event": "TRADE_CLOSE", "trade_id": "l1",
             "symbol": "C", "side": "LONG", "pnl": -3.0, "pnl_pct": -2.0,
             "entry": 300.0, "exit": 297.0, "close_ts": 1710002000,
             "pnl_source": "API", "fee": 0.1, "funding": 0.0,
             "close_reason": "SL", "open_ts": 1700002000, "duration_min": 30.0},
        ])
        m = PerformanceTracker.get_metrics_from_ledger(self.ledger_path)
        self.assertEqual(m["total_trades"], 3)
        self.assertEqual(m["wins"], 2)
        self.assertEqual(m["losses"], 1)
        self.assertAlmostEqual(m["total_pnl"], 12.0)
        self.assertAlmostEqual(m["avg_win"], 7.5)
        self.assertAlmostEqual(m["avg_loss"], 3.0)

    # ── 边界 / 坏数据 ──

    def test_skip_missing_required_fields(self):
        """缺 symbol / side → 跳过"""
        self._write_ledger([
            # 缺 symbol
            {"event": "TRADE_CLOSE", "trade_id": "x",
             "symbol": "", "side": "LONG", "pnl": 1.0, "close_ts": 1710000000},
            # 缺 side
            {"event": "TRADE_CLOSE", "trade_id": "y",
             "symbol": "BTC", "side": "", "pnl": 1.0, "close_ts": 1710000000},
            # 只有 event 标签 (最小坏记录)
            {"event": "TRADE_CLOSE"},
            # 正常记录 — 唯一应该被接受的
            {"event": "TRADE_CLOSE", "trade_id": "ok",
             "symbol": "ETH", "side": "SHORT", "pnl": -1.0,
             "close_ts": 1710000000, "fee": 0.0, "pnl_source": "API",
             "close_reason": "SL", "entry": 100.0, "exit": 99.0,
             "open_ts": 1700000000, "duration_min": 1.0},
        ])
        trades = PerformanceTracker.load_trades_from_ledger(self.ledger_path)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["symbol"], "ETH")

    def test_empty_ledger(self):
        """空文件 → 0 trades, empty_metrics"""
        self._write_ledger([])
        trades = PerformanceTracker.load_trades_from_ledger(self.ledger_path)
        self.assertEqual(len(trades), 0)
        m = PerformanceTracker.get_metrics_from_ledger(self.ledger_path)
        self.assertEqual(m["total_trades"], 0)

    def test_missing_ledger_file(self):
        """不存在的文件 → 安全返回 0 trades"""
        trades = PerformanceTracker.load_trades_from_ledger("/nonexistent/path.jsonl")
        self.assertEqual(len(trades), 0)

    # ── 现有路径不受影响 ──

    def test_existing_behavior_unchanged(self):
        """record_trade() + get_metrics() + _load() 仍正常工作"""
        pt = PerformanceTracker()
        before = len(pt.trades)
        pt.record_trade("TEST", "LONG", 100.0, 110.0, 5.0, 5.0, "pullback", 30.0)
        self.assertEqual(len(pt.trades), before + 1)
        m = pt.get_metrics()
        self.assertGreaterEqual(m["total_trades"], before + 1)
        # _load() 路径未变: 仍从 performance.json 读取
        self.assertTrue(hasattr(pt, "_path"))
        self.assertIn("performance.json", pt._path)


# ════════════════════════════════════════════
# P1 Commit 2: Runtime source switch tests
# ════════════════════════════════════════════

class TestRuntimeLedgerSource(unittest.TestCase):
    """Commit 2: get_metrics() prefers ledger, falls back to memory"""

    @classmethod
    def setUpClass(cls):
        cls.ledger_path = tempfile.mktemp(suffix=".jsonl")

    def _write_ledger(self, lines):
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            for line in lines:
                if isinstance(line, str):
                    f.write(line + "\n")
                else:
                    f.write(json.dumps(line) + "\n")

    def _make_trade(self, symbol, pnl, **kw):
        base = {
            "event": "TRADE_CLOSE", "symbol": symbol, "side": "LONG",
            "pnl": pnl, "pnl_pct": 1.0, "entry": 100.0, "exit": 100 + pnl,
            "pnl_source": "API", "fee": 0.0, "funding": 0.0,
            "close_reason": "TEST", "open_ts": 1700000000,
            "close_ts": 1710000000, "duration_min": 1.0,
        }
        base.update(kw)
        return base

    def test_ledger_preferred_when_available(self):
        """有 ledger 时, get_metrics() 使用 ledger 数据"""
        self._write_ledger([
            self._make_trade("A", 5.0, trade_id="a1"),
            self._make_trade("B", -2.0, trade_id="b1"),
        ])
        # Patch LEDGER_FILE to use test path
        old_ledger = PerformanceTracker.LEDGER_FILE
        PerformanceTracker.LEDGER_FILE = self.ledger_path
        try:
            pt = PerformanceTracker()
            # 即使 pt.trades 来自 performance.json (旧数据), get_metrics() 优先用 ledger
            m = pt.get_metrics()
            self.assertEqual(m["total_trades"], 2)
            self.assertAlmostEqual(m["total_pnl"], 3.0)
            self.assertEqual(m["wins"], 1)
            self.assertEqual(m["losses"], 1)
        finally:
            PerformanceTracker.LEDGER_FILE = old_ledger

    def test_fallback_to_memory_when_ledger_missing(self):
        """ledger 缺失时, get_metrics() fallback 到 self.trades"""
        pt = PerformanceTracker()
        pt.trades = [{
            "timestamp": "", "date": "", "symbol": "X", "direction": "LONG",
            "entry": 100.0, "exit": 110.0, "pnl": 10.0, "pnl_pct": 10.0,
            "strategy": "pullback", "duration_min": 1.0, "win": True,
        }]
        # 用不存在的路径
        old_ledger = PerformanceTracker.LEDGER_FILE
        PerformanceTracker.LEDGER_FILE = "/nonexistent/ledger.jsonl"
        try:
            m = pt.get_metrics()
            self.assertEqual(m["total_trades"], 1)
            self.assertAlmostEqual(m["total_pnl"], 10.0)
        finally:
            PerformanceTracker.LEDGER_FILE = old_ledger

    def test_fallback_when_ledger_empty(self):
        """ledger 为空时, get_metrics() fallback 到 self.trades"""
        self._write_ledger([])
        pt = PerformanceTracker()
        pt.trades = [{
            "timestamp": "", "date": "", "symbol": "Y", "direction": "SHORT",
            "entry": 200.0, "exit": 190.0, "pnl": 10.0, "pnl_pct": 5.0,
            "strategy": "momentum", "duration_min": 1.0, "win": True,
        }]
        old_ledger = PerformanceTracker.LEDGER_FILE
        PerformanceTracker.LEDGER_FILE = self.ledger_path
        try:
            m = pt.get_metrics()
            # Ledger 空 → fallback 到内存 trades
            self.assertEqual(m["total_trades"], 1)
            self.assertAlmostEqual(m["total_pnl"], 10.0)
        finally:
            PerformanceTracker.LEDGER_FILE = old_ledger

    def test_explicit_trades_not_overridden(self):
        """显式传入 trades 时, 不使用 ledger"""
        self._write_ledger([
            self._make_trade("A", 5.0, trade_id="a1"),
            self._make_trade("B", 3.0, trade_id="b1"),
        ])
        explicit = [{
            "timestamp": "", "date": "", "symbol": "Z", "direction": "LONG",
            "entry": 300.0, "exit": 310.0, "pnl": 10.0, "pnl_pct": 3.33,
            "strategy": "pullback", "duration_min": 1.0, "win": True,
        }]
        pt = PerformanceTracker()
        old_ledger = PerformanceTracker.LEDGER_FILE
        PerformanceTracker.LEDGER_FILE = self.ledger_path
        try:
            m = pt.get_metrics(trades=explicit)
            # 显式传入 → 只用传入的 trades, 不用 ledger
            self.assertEqual(m["total_trades"], 1)
            self.assertAlmostEqual(m["total_pnl"], 10.0)
        finally:
            PerformanceTracker.LEDGER_FILE = old_ledger

    def test_record_trade_invalidates_cache(self):
        """record_trade() 后缓存失效, 下次 get_metrics() 重新读取"""
        self._write_ledger([
            self._make_trade("A", 5.0, trade_id="a1"),
        ])
        old_ledger = PerformanceTracker.LEDGER_FILE
        PerformanceTracker.LEDGER_FILE = self.ledger_path
        try:
            pt = PerformanceTracker()
            # 第一次调用: 从 ledger 加载
            m1 = pt.get_metrics()
            self.assertEqual(m1["total_trades"], 1)
            # 模拟新交易: 追加到 ledger 并调用 record_trade()
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._make_trade("B", 3.0, trade_id="b1")) + "\n")
            pt.record_trade("B", "LONG", 100.0, 103.0, 3.0, 3.0, "pullback", 1.0)
            # 第二次调用: 缓存已失效 → 重新读取 ledger
            m2 = pt.get_metrics()
            self.assertEqual(m2["total_trades"], 2)
            self.assertAlmostEqual(m2["total_pnl"], 8.0)
        finally:
            PerformanceTracker.LEDGER_FILE = old_ledger


if __name__ == "__main__":
    unittest.main()
