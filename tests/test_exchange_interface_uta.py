#!/usr/bin/env python3
"""tests/test_exchange_interface_uta.py — UTA Adapter Phase 2: 写路径 payload 单元测试

验证:
  1. Long entry payload → _uta_v3_create_market_order
  2. Short entry payload → _uta_v3_create_market_order
  3. Long close payload → _uta_v3_close_positions
  4. Short close payload → _uta_v3_close_positions
  5. pos_loss TPSL payload → _uta_v3_place_strategy_order
  6. pos_profit TPSL payload → _uta_v3_place_strategy_order

全程 MagicMock, 不访问真实交易所。
"""

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange_interface import ExchangeInterface
from config_manager import ConfigManager


# ── 辅助工厂 ──

def _make_uta_exchange():
    """构造 _is_uta=True 的 ExchangeInterface，仅 mock ccxt 方法"""
    config = MagicMock(spec=ConfigManager)
    config.is_sandbox = False
    config.timeframe = "1h"
    config.kline_limit = 50
    config.bitget_api_key = "test_key"
    config.bitget_secret = "test_secret"
    config.bitget_passphrase = "test_pass"
    config.SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]

    with patch('exchange_interface.ccxt.bitget') as mock_bitget:
        mock_ex = MagicMock()
        mock_ex.markets = {
            "BTC/USDT:USDT": {
                "id": "BTCUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.1, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
            "ETH/USDT:USDT": {
                "id": "ETHUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.01, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
            "SOL/USDT:USDT": {
                "id": "SOLUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.001, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
            "DOT/USDT:USDT": {
                "id": "DOTUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.001, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
            "LINK/USDT:USDT": {
                "id": "LINKUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.001, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
            "XRP/USDT:USDT": {
                "id": "XRPUSDT", "swap": True, "contractSize": 1.0,
                "precision": {"price": 0.0001, "amount": 1},
                "limits": {"amount": {"min": 1}, "cost": {"min": 5}},
            },
        }
        mock_ex.markets_by_id = {
            "BTCUSDT": {"symbol": "BTC/USDT:USDT"},
            "ETHUSDT": {"symbol": "ETH/USDT:USDT"},
            "SOLUSDT": {"symbol": "SOL/USDT:USDT"},
            "DOTUSDT": {"symbol": "DOT/USDT:USDT"},
            "LINKUSDT": {"symbol": "LINK/USDT:USDT"},
            "XRPUSDT": {"symbol": "XRP/USDT:USDT"},
        }
        # mock price_to_precision
        mock_ex.price_to_precision = MagicMock(side_effect=lambda sym, p: str(round(p, 2)))
        # mock fetch_ticker for SL/TP direction validation
        mock_ex.fetch_ticker = MagicMock(return_value={"mark": 50000.0, "last": 50000.0})
        # mock for cache methods
        mock_ex.fetch_ohlcv = MagicMock(return_value=[])
        mock_ex.fetch_trading_fees = MagicMock(return_value={})
        mock_ex.fetch_positions = MagicMock(return_value=[])
        mock_ex.fetch_open_orders = MagicMock(return_value=[])
        # mock set_sandbox_mode
        mock_ex.set_sandbox_mode = MagicMock()

        # UTA V3 endpoints — capture payload
        mock_ex.private_uta_post_v3_trade_place_order = MagicMock(
            return_value={"code": "00000", "msg": "success",
                          "data": {"orderId": "ut_ord_001", "fillPrice": "50000.0"}})
        mock_ex.private_uta_post_v3_trade_place_strategy_order = MagicMock(
            return_value={"code": "00000", "msg": "success",
                          "data": {"orderId": "ut_strat_001"}})
        mock_ex.private_uta_post_v3_trade_close_positions = MagicMock(
            return_value={"code": "00000", "msg": "success",
                          "data": {"orderId": "ut_close_001"}})
        mock_ex.private_uta_get_v3_account_assets = MagicMock(
            return_value={"code": "00000",
                          "data": {"usdtEquity": "10000.0", "accountEquity": "10000.0",
                                   "assets": [{"coin": "USDT", "available": "10000.0",
                                               "locked": "0"}]}})
        mock_ex.private_uta_get_v3_position_current_position = MagicMock(
            return_value={"code": "00000", "data": []})

        mock_bitget.return_value = mock_ex

        ei = ExchangeInterface(config)
        ei._is_uta = True  # force UTA path
        return ei, mock_ex


class TestUTAMarketOrderPayload(unittest.TestCase):
    """UTA V3 市价单 payload"""

    def setUp(self):
        self.ei, self.ex = _make_uta_exchange()

    def test_long_entry_payload(self):
        """Long entry: side=buy, posSide=long, tradeSide=open"""
        order = self.ei._uta_v3_create_market_order(
            symbol="SOL/USDT:USDT", side="buy", amount=2.0,
            pos_side="long", trade_side="open")
        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "ut_ord_001")
        self.assertEqual(order["average"], 50000.0)

        call_params = self.ex.private_uta_post_v3_trade_place_order.call_args[0][0]
        self.assertEqual(call_params["symbol"], "SOLUSDT")
        self.assertEqual(call_params["category"], "USDT-FUTURES")
        self.assertEqual(call_params["marginCoin"], "USDT")
        self.assertEqual(call_params["side"], "buy")
        self.assertEqual(call_params["posSide"], "long")
        self.assertEqual(call_params["orderType"], "market")
        self.assertEqual(call_params["qty"], "2.0")
        self.assertEqual(call_params["tradeSide"], "open")

    def test_short_entry_payload(self):
        """Short entry: side=sell, posSide=short, tradeSide=open"""
        order = self.ei._uta_v3_create_market_order(
            symbol="DOT/USDT:USDT", side="sell", amount=2.0,
            pos_side="short", trade_side="open")
        self.assertIsNotNone(order)

        call_params = self.ex.private_uta_post_v3_trade_place_order.call_args[0][0]
        self.assertEqual(call_params["symbol"], "DOTUSDT")
        self.assertEqual(call_params["side"], "sell")
        self.assertEqual(call_params["posSide"], "short")
        self.assertEqual(call_params["qty"], "2.0")
        self.assertEqual(call_params["tradeSide"], "open")

    def test_below_min_order_returns_none(self):
        """qty < min_amount → 跳过, 不发送 POST"""
        self.ex.private_uta_post_v3_trade_place_order.reset_mock()
        order = self.ei._uta_v3_create_market_order(
            symbol="BTC/USDT:USDT", side="buy", amount=0.0001,
            pos_side="long", trade_side="open")
        self.assertIsNone(order)
        # 不应调用 place_order (在 min check 处直接 return)
        self.ex.private_uta_post_v3_trade_place_order.assert_not_called()

    def test_market_order_failure_returns_none(self):
        """API 失败时返回 None"""
        self.ex.private_uta_post_v3_trade_place_order.return_value = {
            "code": "40085", "msg": "blocked"}
        order = self.ei._uta_v3_create_market_order(
            symbol="BTC/USDT:USDT", side="buy", amount=1.0,
            pos_side="long", trade_side="open")
        self.assertIsNone(order)


class TestUTAClosePositionPayload(unittest.TestCase):
    """UTA V3 平仓 payload"""

    def setUp(self):
        self.ei, self.ex = _make_uta_exchange()

    def test_long_close_payload(self):
        """Close long: posSide=long"""
        result = self.ei._uta_v3_close_positions(
            symbol="BTC/USDT:USDT", pos_side="long")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "ut_close_001")

        call_params = self.ex.private_uta_post_v3_trade_close_positions.call_args[0][0]
        self.assertEqual(call_params["category"], "USDT-FUTURES")
        self.assertEqual(call_params["symbol"], "BTCUSDT")
        self.assertEqual(call_params["posSide"], "long")
        self.assertEqual(call_params["marginCoin"], "USDT")

    def test_short_close_payload(self):
        """Close short: posSide=short"""
        result = self.ei._uta_v3_close_positions(
            symbol="ETH/USDT:USDT", pos_side="short")
        self.assertIsNotNone(result)

        call_params = self.ex.private_uta_post_v3_trade_close_positions.call_args[0][0]
        self.assertEqual(call_params["symbol"], "ETHUSDT")
        self.assertEqual(call_params["posSide"], "short")

    def test_already_closed_detected(self):
        """仓位已不存在时返回 already_closed"""
        self.ex.private_uta_post_v3_trade_close_positions.return_value = {
            "code": "40757", "msg": "Not enough position"}
        result = self.ei._uta_v3_close_positions(
            symbol="BTC/USDT:USDT", pos_side="long")
        self.assertEqual(result["id"], "already_closed")

    def test_close_position_failure_returns_none(self):
        """其他失败返回 None"""
        self.ex.private_uta_post_v3_trade_close_positions.return_value = {
            "code": "40099", "msg": "exchange environment is incorrect"}
        result = self.ei._uta_v3_close_positions(
            symbol="BTC/USDT:USDT", pos_side="long")
        self.assertIsNone(result)


class TestUTATPSLPayload(unittest.TestCase):
    """UTA V3 策略单 (TPSL) payload"""

    def setUp(self):
        self.ei, self.ex = _make_uta_exchange()

    def test_pos_loss_payload(self):
        """pos_loss (止损) payload"""
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="BTC/USDT:USDT", plan_type="pos_loss",
            trigger_price=49000.0, hold_side="long")
        self.assertTrue(ok)

        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["symbol"], "BTCUSDT")
        self.assertEqual(call_params["category"], "USDT-FUTURES")
        self.assertEqual(call_params["marginCoin"], "USDT")
        self.assertEqual(call_params["planType"], "pos_loss")
        self.assertEqual(call_params["holdSide"], "long")
        self.assertEqual(call_params["posSide"], "long")    # V3: posSide must equal holdSide
        self.assertEqual(call_params["triggerType"], "mark_price")
        self.assertIn("triggerPrice", call_params)
        self.assertIn("executePrice", call_params)

    def test_pos_profit_payload(self):
        """pos_profit (止盈) payload"""
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="ETH/USDT:USDT", plan_type="pos_profit",
            trigger_price=2100.0, hold_side="short")
        self.assertTrue(ok)

        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["symbol"], "ETHUSDT")
        self.assertEqual(call_params["planType"], "pos_profit")
        self.assertEqual(call_params["holdSide"], "short")
        self.assertEqual(call_params["posSide"], "short")

    def test_pos_loss_short_payload(self):
        """Short pos_loss: holdSide=short"""
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="BTC/USDT:USDT", plan_type="pos_loss",
            trigger_price=51000.0, hold_side="short")
        self.assertTrue(ok)

        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["planType"], "pos_loss")
        self.assertEqual(call_params["holdSide"], "short")
        self.assertEqual(call_params["posSide"], "short")

    def test_pos_profit_long_payload(self):
        """Long pos_profit: holdSide=long"""
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="BTC/USDT:USDT", plan_type="pos_profit",
            trigger_price=52000.0, hold_side="long")
        self.assertTrue(ok)

        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["planType"], "pos_profit")
        self.assertEqual(call_params["holdSide"], "long")
        self.assertEqual(call_params["posSide"], "long")

    def test_strategy_order_failure_returns_false(self):
        """策略单失败返回 False"""
        self.ex.private_uta_post_v3_trade_place_strategy_order.return_value = {
            "code": "40085", "msg": "blocked"}
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="BTC/USDT:USDT", plan_type="pos_loss",
            trigger_price=49000.0, hold_side="long")
        self.assertFalse(ok)

    def test_invalid_hold_side_rejected(self):
        """非法 hold_side → 拒绝发单, 不调 API"""
        self.ex.private_uta_post_v3_trade_place_strategy_order.reset_mock()
        ok = self.ei._uta_v3_place_strategy_order(
            symbol="BTC/USDT:USDT", plan_type="pos_loss",
            trigger_price=49000.0, hold_side="buy")  # "buy" not "long"/"short"
        self.assertFalse(ok)
        self.ex.private_uta_post_v3_trade_place_strategy_order.assert_not_called()


class TestUTAWritePathIntegration(unittest.TestCase):
    """UTA V3 写路径集成场景"""

    def setUp(self):
        self.ei, self.ex = _make_uta_exchange()

    def test_create_market_order_close_uta_path(self):
        """create_market_order_close 走 UTA V3 路径"""
        result = self.ei.create_market_order_close(
            symbol="BTC/USDT:USDT", amount=1.0, side="sell", pos_side="long")
        self.assertIsNotNone(result)
        # 应该调用 UTA close positions 而非 ccxt close_position
        self.ex.private_uta_post_v3_trade_close_positions.assert_called_once()
        call_params = self.ex.private_uta_post_v3_trade_close_positions.call_args[0][0]
        self.assertEqual(call_params["symbol"], "BTCUSDT")
        self.assertEqual(call_params["posSide"], "long")

    def test_set_position_sl_tp_uta_path(self):
        """set_position_sl_tp 走 UTA V3 路径 (一次合并 SL+TP)"""
        ok = self.ei.set_position_sl_tp(
            symbol="BTC/USDT:USDT", side="long",
            sl_price=49000.0, tp_price=52000.0)
        self.assertTrue(ok)
        # 应调用一次 strategy order: planType=normal + stopLoss + takeProfit
        self.assertEqual(
            self.ex.private_uta_post_v3_trade_place_strategy_order.call_count, 1)

        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["planType"], "normal")
        self.assertEqual(call_params["holdSide"], "long")
        self.assertEqual(call_params["posSide"], "long")
        self.assertIn("stopLoss", call_params)
        self.assertIn("takeProfit", call_params)
        self.assertNotEqual(call_params.get("stopLoss"), "")
        self.assertNotEqual(call_params.get("takeProfit"), "")

    def test_set_position_sl_tp_short_uta_path(self):
        """Short TPSL via UTA V3 (一次合并 SL+TP)"""
        ok = self.ei.set_position_sl_tp(
            symbol="ETH/USDT:USDT", side="short",
            sl_price=2100.0, tp_price=1900.0)
        self.assertTrue(ok)
        self.assertEqual(
            self.ex.private_uta_post_v3_trade_place_strategy_order.call_count, 1)
        call_params = self.ex.private_uta_post_v3_trade_place_strategy_order.call_args[0][0]
        self.assertEqual(call_params["holdSide"], "short")
        self.assertEqual(call_params["planType"], "normal")

    def test_uta_path_not_used_when_is_uta_false(self):
        """_is_uta=False 时仍走 Classic 路径"""
        self.ei._is_uta = False
        # 重置 mock
        self.ex.private_uta_post_v3_trade_close_positions.reset_mock()

        # Classic close_position 已由 ccxt 提供
        self.ex.close_position = MagicMock(return_value={"id": "ccxt_close_001"})
        result = self.ei.create_market_order_close(
            symbol="BTC/USDT:USDT", amount=1.0, side="sell", pos_side="long")
        self.assertIsNotNone(result)
        # UTA close 不应该被调用
        self.ex.private_uta_post_v3_trade_close_positions.assert_not_called()

    def test_side_to_pos_side_derivation_in_close(self):
        """create_market_order_close: pos_side 为空时从 side 推导"""
        # side=sell → pos_side=short
        result = self.ei.create_market_order_close(
            symbol="BTC/USDT:USDT", amount=1.0, side="sell")
        self.assertIsNotNone(result)
        call_params = self.ex.private_uta_post_v3_trade_close_positions.call_args[0][0]
        self.assertEqual(call_params["posSide"], "short")

    def test_partial_tp_additional_levels_uta_path(self):
        """create_market_order_with_partial_tp: 附加 TP 走 UTA V3"""
        # 先重置 strategy order mock
        self.ex.private_uta_post_v3_trade_place_strategy_order.reset_mock()

        order = self.ei.create_market_order_with_partial_tp(
            symbol="SOL/USDT:USDT", side="buy", amount=2.0,
            sl_price=70.0,
            tp_parts=[(80.0, 0.5), (85.0, 0.5)],
            pos_side="long")
        self.assertIsNotNone(order)

        # place_order 调用1次 (市价单)
        self.assertEqual(self.ex.private_uta_post_v3_trade_place_order.call_count, 1)

        # strategy_order: SL+TP combined(normal) + TP2(pos_profit) = 2
        self.assertGreaterEqual(
            self.ex.private_uta_post_v3_trade_place_strategy_order.call_count, 2)

    def test_no_ccxt_classic_in_uta_path(self):
        """UTA path 不应调用任何 Classic V2 方法"""
        self.ei.create_market_order_close(
            symbol="BTC/USDT:USDT", amount=1.0, side="sell", pos_side="long")
        self.ei.set_position_sl_tp(
            symbol="BTC/USDT:USDT", side="long",
            sl_price=49000.0, tp_price=52000.0)
        self.ei._uta_v3_create_market_order(
            symbol="BTC/USDT:USDT", side="buy", amount=1.0,
            pos_side="long", trade_side="open")

        # 确认没有调用任何 private_mix_post_v2_* 方法
        for attr_name in dir(self.ex):
            if 'private_mix' in attr_name and hasattr(getattr(self.ex, attr_name), 'called'):
                self.assertFalse(
                    getattr(self.ex, attr_name).called,
                    f"{attr_name} should not be called in UTA path")


class TestUTASizingGuard(unittest.TestCase):
    """50U canary sizing guard"""

    def setUp(self):
        self.ei, self.ex = _make_uta_exchange()
        # Mock instruments cache
        self.ei._uta_instruments_cache = {
            "BTCUSDT": {"minQty": 0.0001, "multiplier": 0.0001, "qtyPrecision": 4, "pricePrecision": 1},
            "SOLUSDT": {"minQty": 0.1, "multiplier": 0.1, "qtyPrecision": 1, "pricePrecision": 3},
            "DOTUSDT": {"minQty": 1, "multiplier": 1, "qtyPrecision": 0, "pricePrecision": 3},
            "LINKUSDT": {"minQty": 1, "multiplier": 1, "qtyPrecision": 0, "pricePrecision": 3},
            "XRPUSDT": {"minQty": 1, "multiplier": 1, "qtyPrecision": 0, "pricePrecision": 4},
        }
        self.ei._uta_instruments_ts = time.time() + 600

    def test_allowlist_allows_sol(self):
        ok, reason = self.ei._uta_validate_order("SOL/USDT:USDT", 1.0, 75.0)
        self.assertTrue(ok, reason)

    def test_allowlist_allows_dot(self):
        ok, reason = self.ei._uta_validate_order("DOT/USDT:USDT", 41.0, 1.0)
        self.assertTrue(ok, reason)

    def test_allowlist_allows_link(self):
        ok, reason = self.ei._uta_validate_order("LINK/USDT:USDT", 4.0, 8.0)
        self.assertTrue(ok, reason)

    def test_allowlist_allows_xrp(self):
        ok, reason = self.ei._uta_validate_order("XRP/USDT:USDT", 22.0, 1.2)
        self.assertTrue(ok, reason)

    def test_not_allowlist_btc_skipped(self):
        ok, reason = self.ei._uta_validate_order("BTC/USDT:USDT", 1.0, 70000.0)
        self.assertFalse(ok)
        self.assertIn("SKIP_CANARY_NOT_ALLOWLIST", reason)

    def test_not_allowlist_eth_skipped(self):
        ok, reason = self.ei._uta_validate_order("ETH/USDT:USDT", 1.0, 2000.0)
        self.assertFalse(ok)
        self.assertIn("SKIP_CANARY_NOT_ALLOWLIST", reason)

    def test_below_min_qty_skipped(self):
        ok, reason = self.ei._uta_validate_order("SOL/USDT:USDT", 0.05, 75.0)
        self.assertFalse(ok)
        self.assertIn("SKIP_BELOW_MIN_QTY", reason)

    def test_qty_multiplier_aligned(self):
        ok, reason = self.ei._uta_validate_order("SOL/USDT:USDT", 0.3, 75.0)
        self.assertTrue(ok)  # 0.3 is multiple of 0.1

    def test_create_market_order_allowlist_guard(self):
        """_uta_v3_create_market_order rejects non-allowlist symbols"""
        order = self.ei._uta_v3_create_market_order(
            symbol="BTC/USDT:USDT", side="buy", amount=1.0,
            pos_side="long", trade_side="open")
        self.assertIsNone(order)


if __name__ == "__main__":
    unittest.main()
