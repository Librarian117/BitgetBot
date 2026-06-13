#!/usr/bin/env python3
"""
flow_monitor.py — 订单流监控器 v2.0
====================================
v2.0: OI持久化 / 48点4小时趋势 / 极端背离硬阻止 / 仓位影响 / 退出审核

数据源: Bitget v2 公开 API (免费，无需额外权限)

信号逻辑:
  OI↑ + 价格↑ → bullish (多头加仓，趋势延续)
  OI↑ + 价格↓ → bearish (空头加仓，趋势延续)
  OI↓ + 价格↑ → bearish_divergence (空头离场，涨势将尽)
  OI↓ + 价格↓ → bullish_divergence (多头离场，跌势将尽)
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("QuantBot")


class FlowMonitor:
    """订单流监控器 v2.0 — OI 追踪 + 背离检测 + 持久化"""

    OI_HISTORY_FILE = "oi_history.json"

    def __init__(self, exchange_interface):
        self.ex = exchange_interface
        # OI 历史: {symbol: [(timestamp, oi_value, price), ...]}
        self._oi_history: Dict[str, List[Tuple[float, float, float]]] = {}
        self._max_history = 48      # v2.0: 48点 = 4小时 (5min间隔)
        self._min_trend_points = 6  # v2.0: 至少6个点(30min)才有意义
        self._last_fetch: Dict[str, float] = {}
        self._cache_ttl = 300       # 5分钟缓存

        # 持久化路径
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._history_path = os.path.join(script_dir, self.OI_HISTORY_FILE)
        self._load_oi_history()

    # ════════════════════════════════════════════
    # 公开 API
    # ════════════════════════════════════════════

    def fetch_oi(self, symbol: str, current_price: float = 0) -> Optional[float]:
        """获取指定币种的 OI 数据 (带缓存)"""
        now = time.time()
        if symbol in self._last_fetch:
            if now - self._last_fetch[symbol] < self._cache_ttl:
                if self._oi_history.get(symbol):
                    return self._oi_history[symbol][-1][1]

        try:
            oi_data = self.ex.exchange.fetch_open_interest(symbol)
            oi = float(oi_data.get("openInterestAmount", 0) or 0)
            if oi <= 0:
                return None
            self._last_fetch[symbol] = now

            if symbol not in self._oi_history:
                self._oi_history[symbol] = []
            if current_price > 0:
                self._oi_history[symbol].append((now, oi, current_price))
            if len(self._oi_history[symbol]) > self._max_history:
                self._oi_history[symbol] = self._oi_history[symbol][-self._max_history:]
            return oi
        except Exception as e:
            logger.debug(f"📊 OI 获取失败 {symbol}: {e}")
            return None

    def get_flow_signal(self, symbol: str, current_price: float = 0) -> Dict[str, Any]:
        """v2.0: OI 信号 + 趋势强度 + 极端背离标记"""
        result = {
            "signal": "neutral", "oi_change_pct": 0.0,
            "oi_trend": "flat", "price_trend": "flat",
            "divergence": False, "confidence": 50, "detail": "",
            "extreme": False, "trend_strength": 0.0,  # v2.0
        }

        oi = self.fetch_oi(symbol, current_price)
        if oi is None:
            return result

        hist = self._oi_history.get(symbol, [])
        if len(hist) < self._min_trend_points:
            return result

        # ── OI 变化率 (最近6点 vs 最早6点的均值, 更平滑) ──
        recent_oi = sum(h[1] for h in hist[-6:]) / 6
        early_oi = sum(h[1] for h in hist[:6]) / 6
        if early_oi > 0:
            oi_change = (recent_oi - early_oi) / early_oi * 100
            result["oi_change_pct"] = round(oi_change, 2)
            if oi_change > 3:
                result["oi_trend"] = "up"
                result["trend_strength"] = min(1.0, oi_change / 10)
            elif oi_change < -3:
                result["oi_trend"] = "down"
                result["trend_strength"] = min(1.0, abs(oi_change) / 10)
            else:
                result["oi_trend"] = "flat"
                result["trend_strength"] = 0.0

        # ── 价格趋势 (6点平滑) ──
        recent_price = sum(h[2] for h in hist[-6:] if h[2] > 0) / max(1, sum(1 for h in hist[-6:] if h[2] > 0))
        early_price = sum(h[2] for h in hist[:6] if h[2] > 0) / max(1, sum(1 for h in hist[:6] if h[2] > 0))
        if early_price > 0 and recent_price > 0:
            price_change = (recent_price - early_price) / early_price * 100
            if price_change > 0.3:
                result["price_trend"] = "up"
            elif price_change < -0.3:
                result["price_trend"] = "down"
            else:
                result["price_trend"] = "flat"

        # ── OI-Price 背离检测 ──
        oi_t = result["oi_trend"]
        p_t = result["price_trend"]

        if oi_t == "up" and p_t == "up":
            result["signal"] = "bullish"
            result["confidence"] = min(90, 60 + int(result["trend_strength"] * 30))
            result["detail"] = f"OI+{result['oi_change_pct']:.1f}% 价格同步上涨-多头加仓"

        elif oi_t == "up" and p_t == "down":
            result["signal"] = "bearish"
            result["confidence"] = min(90, 60 + int(result["trend_strength"] * 30))
            result["detail"] = f"OI+{result['oi_change_pct']:.1f}% 价格下跌-空头加仓"

        elif oi_t == "up" and p_t == "flat":
            result["signal"] = "neutral"
            result["confidence"] = 55
            result["detail"] = f"OI+{result['oi_change_pct']:.1f}% 价格横盘-即将突破"

        elif oi_t == "down" and p_t == "up":
            result["signal"] = "bearish_divergence"
            result["divergence"] = True
            result["confidence"] = 75
            result["detail"] = f"OI{result['oi_change_pct']:.1f}% 价格逆涨-空头离场，涨势将尽"

        elif oi_t == "down" and p_t == "down":
            result["signal"] = "bullish_divergence"
            result["divergence"] = True
            result["confidence"] = 75
            result["detail"] = f"OI{result['oi_change_pct']:.1f}% 价格续跌-多头离场，跌势将尽"

        elif oi_t == "down" and p_t == "flat":
            result["signal"] = "neutral"
            result["confidence"] = 55
            result["detail"] = f"OI{result['oi_change_pct']:.1f}% 价格横盘-资金离场"
        else:
            result["signal"] = "neutral"
            result["confidence"] = 50

        # v2.0: 极端背离标记 (OI变化>5% + 背离 = 趋势衰竭信号，降低阈值)
        if result["divergence"] and abs(result["oi_change_pct"]) > 5:
            result["extreme"] = True
            result["detail"] += " ⚠️极端背离"

        # 持久化 (每5个数据点存一次)
        if len(self._oi_history.get(symbol, [])) % 5 == 0:
            self._save_oi_history()

        return result

    # ════════════════════════════════════════════
    # v2.0: 硬阻止 / 仓位影响 / 退出审核
    # ════════════════════════════════════════════

    def should_block_trade(self, symbol: str, direction: str,
                           current_price: float = 0) -> Tuple[bool, str]:
        """
        v2.0: OI 极端背离 → 硬阻止开仓。
        背离方向与交易方向相反时，跳过该信号。
        """
        sig = self.get_flow_signal(symbol, current_price)
        if not sig.get("extreme"):
            return False, ""

        # 极端 bearish_divergence (空头离场) = 涨势将尽 → 阻止 LONG
        if sig["signal"] == "bearish_divergence" and direction == "LONG":
            return True, f"OI极端空头离场: {sig['detail']}"
        # 极端 bullish_divergence (多头离场) = 跌势将尽 → 阻止 SHORT
        if sig["signal"] == "bullish_divergence" and direction == "SHORT":
            return True, f"OI极端多头离场: {sig['detail']}"

        return False, ""

    def get_oi_position_multiplier(self, symbol: str, direction: str,
                                    current_price: float = 0) -> float:
        """v2.0: OI 信号 → 仓位乘数 (0.7-1.2x)"""
        sig = self.get_flow_signal(symbol, current_price)
        signal = sig.get("signal", "neutral")

        # OI 方向与交易方向一致 → 加仓
        if signal == "bullish" and direction == "LONG":
            return 1.10
        if signal == "bearish" and direction == "SHORT":
            return 1.10
        # 背离但交易方向正确 (做空+空头离场=涨势将尽做空对, 做多+多头离场=跌势将尽做多对)
        if signal == "bearish_divergence" and direction == "SHORT":
            return 1.15  # 背离做空最强信号
        if signal == "bullish_divergence" and direction == "LONG":
            return 1.15
        # 极端背离 → 减仓
        if sig.get("extreme"):
            return 0.70
        # OI 方向与交易方向相反 → 减仓
        if signal == "bullish" and direction == "SHORT":
            return 0.85
        if signal == "bearish" and direction == "LONG":
            return 0.85

        return 1.0

    # ════════════════════════════════════════════
    # 批量 & 摘要
    # ════════════════════════════════════════════

    def get_summary(self, symbols: List[str]) -> str:
        """v2.0: OI 摘要 (正确标签为"订单流"而非"新闻")"""
        signals = []
        for sym in symbols:
            hist = self._oi_history.get(sym, [])
            if len(hist) < self._min_trend_points:
                continue
            s = self.get_flow_signal(sym, hist[-1][2])
            if s["signal"] != "neutral":
                prefix = "🚨" if s.get("extreme") else "⚠️" if s["divergence"] else "📊"
                signals.append(f"{prefix} {sym.split('/')[0]}: {s['detail']}")

        if not signals:
            return ""
        return "OI订单流:\n" + "\n".join(f"- {s}" for s in signals)

    # ════════════════════════════════════════════
    # v2.0: 持久化
    # ════════════════════════════════════════════

    def _save_oi_history(self):
        try:
            data = {}
            for sym, hist in self._oi_history.items():
                if len(hist) >= self._min_trend_points:
                    # 只保存最近48点
                    data[sym] = [
                        {"ts": h[0], "oi": h[1], "price": h[2]}
                        for h in hist[-self._max_history:]
                    ]
            tmp = self._history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._history_path)
        except Exception as e:
            logger.debug(f"📊 OI历史保存失败: {e}")

    def _load_oi_history(self):
        if not os.path.exists(self._history_path):
            return
        try:
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            restored = 0
            for sym, hist in data.items():
                self._oi_history[sym] = [
                    (h["ts"], h["oi"], h["price"]) for h in hist
                ]
                restored += len(hist)
            if restored > 0:
                logger.info(f"📊 已恢复 OI 历史: {len(data)}个币种, {restored}个数据点")
        except Exception as e:
            logger.debug(f"📊 OI历史加载失败: {e}")
