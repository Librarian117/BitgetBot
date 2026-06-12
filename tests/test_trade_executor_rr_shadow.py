"""
v4.5 回归测试: 低 R:R 影子路径不抛 NameError

触发条件: _rr_ratio < min_rr_ratio 时，日志 f-string 使用正确的变量名
           _rr_expected_profit / _rr_ratio (而非 expected_profit / rr_ratio)

不依赖真实交易所 — 全程 mock。
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestLowRRShadowPath(unittest.TestCase):
    """验证 execute() 在低 R:R 时不会因 NameError 崩溃"""

    def setUp(self):
        """构造最小 mock 环境, 触发 _rr_ratio < min_rr_ratio 路径"""
        # --- Mock exchange ---
        self.mock_exchange = MagicMock()
        self.mock_exchange.fetch_usdt_balance.return_value = 10000.0
        self.mock_exchange.get_account_summary.return_value = {
            "equity": 10000, "balance": 10000, "used_margin": 0,
            "unrealized_pnl": 0, "positions_detail": [],
        }
        self.mock_exchange.get_contract_size.return_value = 1.0
        self.mock_exchange.get_min_amount.return_value = 1.0
        self.mock_exchange.get_taker_fee.return_value = 0.0006  # 必须返回 float

        # 市价单成交返回 (模拟主单成功)
        self.mock_exchange.create_order.return_value = {
            "id": "test_order_001", "price": 100.0, "average": 100.0,
        }
        # pos-tpsl 成功
        self.mock_exchange.private_mix_post_v2_mix_order_place_pos_tpsl.return_value = {
            "code": "00000",
        }
        # 价格精度 / 行情
        self.mock_exchange.price_to_precision.side_effect = lambda sym, p: str(round(p, 2))
        self.mock_exchange.market.return_value = {"contractSize": 1.0}
        self.mock_exchange.fetch_ticker.return_value = {"mark": 100.0, "last": 100.0}

        # --- Mock config ---
        self.mock_config = MagicMock()
        self.mock_config.leverage = 10
        self.mock_config.adx_margin_min = 0.02
        self.mock_config.adx_margin_max = 0.04
        self.mock_config.sl_atr_mult = 2.0
        self.mock_config.tp_atr_mults = [1.0, 2.0]       # TP1=1xATR (小 TP)
        self.mock_config.tp_split_ratios = [0.5, 0.5]     # 需要 len() 计算
        self.mock_config.slippage_buffer = 0.001
        self.mock_config.kelly_multiplier = 1.0
        self.mock_config.min_position_value = 0
        self.mock_config.max_position_pct = 0.05
        self.mock_config.min_rr_ratio = 5.0               # 高门槛 → 容易触发影子
        self.mock_config.max_position_ratio = 1.0
        self.mock_config.is_sandbox = True
        self.mock_config.sandbox_safety = True             # 走入 R:R 检查路径
        self.mock_config.momentum_enabled = False

        # 手续费
        self.mock_config.taker_fee = 0.0006
        self.mock_config.maker_fee = 0.0002

        # --- Mock safety ---
        self.mock_safety = MagicMock()
        self.mock_safety._skip_safety = True
        self.mock_safety.check_min_position_value.return_value = (True, "")
        self.mock_safety.check_max_notional.return_value = (True, "")
        self.mock_safety.check_api_errors.return_value = (True, "")
        self.mock_safety.check_order_value.return_value = (True, "")
        self.mock_safety.check_profit_after_fees.return_value = (True, "")
        self.mock_safety.confirm_order_fill.return_value = (True, None)
        self.mock_safety.order_confirmation_enabled = False
        self.mock_safety.limit_fallback_enabled = False

        # --- Mock tlogger (TradeLogger) ---
        self.mock_tlogger = MagicMock()

        # --- Mock SessionManager ---
        self.session_patcher = patch(
            "trade_executor.SessionManager.get_session",
            return_value={
                "label": "test", "adx_threshold": 16, "vol_ratio": 1.5,
                "sl_mult": 1.0, "margin_mult": 1.0, "tp_mult": 1.0,
            },
        )
        self.session_patcher.start()

    def tearDown(self):
        self.session_patcher.stop()

    def _make_executor(self):
        """构造 TradeExecutor — safety 在构造后注入"""
        from trade_executor import TradeExecutor

        executor = TradeExecutor(
            config=self.mock_config,
            exchange=self.mock_exchange,
            tlogger=self.mock_tlogger,
        )
        executor.safety = self.mock_safety
        executor._skip_safety = False  # 启用安全层以走入 R:R 检查路径
        return executor

    def test_low_rr_shadow_does_not_throw_name_error(self):
        """核心断言: 低 R:R 时 execute() 正常返回, 不抛 NameError"""
        executor = self._make_executor()

        # 用极小 ATR 使 TP 距离短 → 预期盈利小 → R:R 低
        # ATR=0.1, price=100 → TP距离=1*0.1=0.1 → 预期盈利=0.1*张数*合约面值
        # 张数 = (保证金*杠杆)/(价格*合约面值) = (200*10)/(100*1) = 20
        # 预期盈利 ≈ 0.1*20*1 = 2.0, 手续费 ≈ 张数*价格*taker_fee ≈ 20*100*0.0006 ≈ 1.2
        # R:R ≈ 2.0/1.2 ≈ 1.67 < min_rr_ratio=5 → 触发影子路径

        result = executor.execute(
            symbol="BTC/USDT:USDT",
            direction="SHORT",
            price=100.0,
            atr=0.1,              # 极小波动 → 小预期盈利
            adx=30.0,
            strategy="pullback",
        )

        # 不应抛异常; result 应包含成功标志
        self.assertTrue(result["success"],
                        f"execute 应返回 success=True, 实际: {result}")
        # 验证 R:R 变量存在且合理
        self.assertGreater(result.get("rr_ratio", 0), 0,
                           "rr_ratio 应 > 0")
        self.assertLess(result["rr_ratio"], self.mock_config.min_rr_ratio,
                        f"rr_ratio={result['rr_ratio']} 应 < min_rr_ratio={self.mock_config.min_rr_ratio}")

    def test_low_rr_shadow_logs_warning(self):
        """低 R:R 时应打印影子警告日志"""
        executor = self._make_executor()

        executor.execute(
            symbol="BTC/USDT:USDT",
            direction="SHORT",
            price=100.0,
            atr=0.1,
            adx=30.0,
            strategy="pullback",
        )

        # log_rr_shadow 至少被调用一次 (影子记录)
        self.mock_tlogger.log_rr_shadow.assert_called()


if __name__ == "__main__":
    unittest.main()
