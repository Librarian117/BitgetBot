#!/usr/bin/env python3
"""tests/test_equity_auditor_dual_basis.py — P1 Commit 3 验收测试

验证:
  1. expected_equity_with_upl 正确对齐 Bitget accountEquity
  2. 持仓期间 open UPL 不单独触发误报警
  3. 真正 accounting drift 仍然能报警
  4. fees 和 funding 正确纳入双口径
  5. 现有 snapshot/check 向后兼容
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equity_auditor import EquityAuditor


class TestDualBasisEquity(unittest.TestCase):
    """P1 Commit 3: 双口径资金审计"""

    def setUp(self):
        self.auditor = EquityAuditor(initial_equity=10000.0, alarm_threshold=50.0)

    def _snap(self, exchange_equity, realized=0, upl=0, fees=0, funding=0, positions=0):
        return self.auditor.snapshot(
            exchange_equity=exchange_equity,
            exchange_balance=exchange_equity - 100,  # approximate
            exchange_margin=100,
            exchange_upl=upl,
            bot_realized_pnl=realized,
            bot_fees=fees,
            bot_funding_fees=funding,
            open_positions=positions,
        )

    # ── 无持仓场景 ──

    def test_no_positions_no_upl(self):
        """无持仓无 UPL: 双口径等价, 偏差为 0"""
        entry = self._snap(10000.0, realized=0, upl=0, positions=0)
        self.assertAlmostEqual(entry["expected_equity_realized"], 10000.0)
        self.assertAlmostEqual(entry["expected_equity_with_upl"], 10000.0)
        self.assertAlmostEqual(entry["deviation_realized"], 0.0)
        self.assertAlmostEqual(entry["deviation_with_upl"], 0.0)
        self.assertFalse(entry["alarm"])

    def test_no_positions_realized_drift(self):
        """无持仓但 realized PnL 与实际不一致: 应报警"""
        # Bot thinks it made +200 but exchange says only +100
        entry = self._snap(10100.0, realized=200, upl=0, positions=0)
        # realized 口径: 10000+200=10200 vs 10100 → deviation=-100
        self.assertAlmostEqual(entry["deviation_realized"], -100.0)
        self.assertAlmostEqual(entry["deviation_with_upl"], -100.0)  # upl=0 so same
        self.assertTrue(entry["alarm"])

    # ── 持仓场景: open UPL 存在, realized 正常 ──

    def test_open_upl_no_false_alarm(self):
        """持仓浮盈 +80, realized 正常: 不应报警 (之前会误报)"""
        # exchange equity = 10080 (initial + upl), bot realized = 0
        entry = self._snap(10080.0, realized=0, upl=80, positions=1)
        # realized 口径: 10000+0=10000 vs 10080 → deviation=80
        self.assertAlmostEqual(entry["deviation_realized"], 80.0)
        # with_upl 口径: 10000+0+80=10080 vs 10080 → deviation=0
        self.assertAlmostEqual(entry["deviation_with_upl"], 0.0)
        # 双口径不会同时超 50: realized=80>50, with_upl=0<50 → 不报警
        self.assertFalse(entry["alarm"],
                         "open UPL alone should NOT trigger alarm")

    def test_open_upl_plus_real_drift(self):
        """持仓浮盈 +80 AND realized 少记 +100: 双口径都超, 应报警"""
        # Bot thinks realized=0 but should be +100
        entry = self._snap(10180.0, realized=0, upl=80, positions=1)
        # realized: 10000+0=10000 vs 10180 → dev=-180
        # with_upl: 10000+0+80=10080 vs 10180 → dev=-100
        self.assertTrue(abs(entry["deviation_realized"]) > 50)
        self.assertTrue(abs(entry["deviation_with_upl"]) > 50)
        self.assertTrue(entry["alarm"],
                        "real drift + open UPL should trigger alarm")

    def test_open_loss_no_false_alarm(self):
        """持仓浮亏 -60, realized 正常: 不应报警"""
        entry = self._snap(9940.0, realized=0, upl=-60, positions=1)
        self.assertAlmostEqual(entry["deviation_realized"], -60.0)
        self.assertAlmostEqual(entry["deviation_with_upl"], 0.0)
        self.assertFalse(entry["alarm"],
                         "open loss alone should NOT trigger alarm")

    # ── fees / funding 纳入口径 ──

    def test_fees_included_in_both_bases(self):
        """手续费在双口径中都被扣除"""
        # realized=+100, fees=-5, funding=-1
        entry = self._snap(10094.0, realized=100, upl=0, fees=5, funding=1, positions=0)
        # both: 10000 + 100 - 5 - 1 = 10094
        self.assertAlmostEqual(entry["expected_equity_realized"], 10094.0)
        self.assertAlmostEqual(entry["expected_equity_with_upl"], 10094.0)
        self.assertAlmostEqual(entry["deviation_realized"], 0.0)

    def test_fees_with_upl(self):
        """手续费 + UPL 同时存在: realized 偏差=|upl|, with_upl 偏差=0"""
        entry = self._snap(10170.0, realized=100, upl=80, fees=5, funding=5, positions=1)
        # realized: 10000+100-5-5=10090 vs 10170 → dev=80 (= upl)
        self.assertAlmostEqual(entry["deviation_realized"], 80.0)
        # with_upl: 10090+80=10170 vs 10170 → dev=0
        self.assertAlmostEqual(entry["deviation_with_upl"], 0.0)
        self.assertFalse(entry["alarm"])

    # ── 向后兼容 ──

    def test_backward_compatible_fields(self):
        """旧字段 expected_equity / deviation 仍存在且等于 realized 口径"""
        entry = self._snap(10080.0, realized=0, upl=80, positions=1)
        self.assertAlmostEqual(entry["expected_equity"], entry["expected_equity_realized"])
        self.assertAlmostEqual(entry["deviation"], entry["deviation_realized"])

    def test_check_returns_dual_basis(self):
        """check() 返回双口径字段"""
        self._snap(10080.0, realized=0, upl=80, positions=1)
        result = self.auditor.check()
        self.assertEqual(result["status"], "OK")
        self.assertIn("expected_equity_realized", result)
        self.assertIn("expected_equity_with_upl", result)
        self.assertIn("deviation_realized", result)
        self.assertIn("deviation_with_upl", result)

    # ── 报警抑制仍生效 ──

    def test_alarm_suppression_still_works(self):
        """连续相同偏差不重复写报警日志"""
        self._snap(10100.0, realized=200, upl=0, positions=0)  # alarm
        self._snap(10100.0, realized=200, upl=0, positions=0)  # suppressed
        self._snap(10100.0, realized=200, upl=0, positions=0)  # suppressed
        self.assertEqual(len(self.auditor.alarms), 3)
        self.assertIn("抑制", self.auditor.alarms[-1]["msg"])


if __name__ == "__main__":
    unittest.main()
