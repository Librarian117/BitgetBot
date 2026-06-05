#!/usr/bin/env python3
"""
health_monitor.py — 全链路健康检查 v3.6
========================================
根据 Bitget v2 API 文档重构，每周期扫描，输出 health.json。

检查项:
  1. Bitget API 连通性
  2. 仓位 TPSL 保护 (position-level + plan order 双检测)
  3. 超额计划单 (每仓 > 2 个 SL/TP = 异常)
  4. 信号流速 (是否长时间 0 信号)
  5. 风险指标 (日内亏损逼近上限？)
  6. 卡单检测 (持仓超过 N 小时未动)
  7. 保证金健康 (可用余额/总权益比例)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from time_utils import now, now_iso

logger = logging.getLogger("QuantBot")


class HealthMonitor:
    """全链路健康检查器 v3.6"""

    HEALTH_FILE = "health.json"

    # ── 阈值 ──
    MAX_PLAN_ORDERS_PER_POS = 4     # 每仓计划单上限 (正常 2: SL+TP)
    STALE_HOURS_WARN = 24           # 持仓过久警告
    SIGNAL_DRY_HOURS_WARN = 12      # 长时间无信号警告
    MARGIN_RATIO_WARN = 0.8         # 保证金超 80% 余额告警
    BALANCE_MIN_RATIO = 0.15        # 可用余额低于 15% 告警

    def __init__(self, config, exchange, analyst, riskmon, tlogger):
        self.config = config
        self.exchange = exchange
        self.analyst = analyst
        self.riskmon = riskmon
        self.tlogger = tlogger

        self._start_time = now()
        self._error_count: int = 0
        self._last_signal_time: Optional[datetime] = None
        self._last_trade_time: Optional[datetime] = None
        self._position_open_times: Dict[str, datetime] = {}

        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._health_path = os.path.join(script_dir, self.HEALTH_FILE)

    # ════════════════════════════════════════════
    # v3.6: Position TPSL 检测 (兼容 place-pos-tpsl)
    # ════════════════════════════════════════════

    @staticmethod
    def _check_position_tpsl(pos: Dict) -> tuple:
        """
        检查仓位级别的 SL/TP。
        place-pos-tpsl API 设置后，仓位 info 中有 stopLoss/takeProfit 字段。
        返回 (has_sl, has_tp)
        """
        info = pos.get("info", pos)
        sl = info.get("stopLoss", "")
        tp = info.get("takeProfit", "")
        return (bool(sl and sl != ""), bool(tp and tp != ""))

    @staticmethod
    def _check_plan_orders(orders: List[Dict], pos_side: str, entry: float) -> tuple:
        """
        检查独立计划单中的 SL/TP。
        返回 (has_sl, has_tp, order_count)
        """
        has_sl = has_tp = False
        for o in orders:
            tp_val = float(o.get("info", {}).get("triggerPrice", 0) or 0)
            if tp_val <= 0:
                continue
            if pos_side == "short":
                if tp_val > entry: has_sl = True    # 做空 SL 在入场价上方
                if tp_val < entry: has_tp = True    # 做空 TP 在入场价下方
            else:
                if tp_val < entry: has_sl = True    # 做多 SL 在入场价下方
                if tp_val > entry: has_tp = True    # 做多 TP 在入场价上方
        return (has_sl, has_tp, len(orders))

    def check(self, cycle: int, candidates: int, trades: int,
              positions: List[Dict], errors: int = 0) -> Dict[str, Any]:
        """全链路扫描，返回健康状态"""
        self._error_count += errors
        _now = now()
        if candidates > 0:
            self._last_signal_time = _now
        if trades > 0:
            self._last_trade_time = _now

        issues: List[str] = []
        warnings: List[str] = []

        # ── 1. Bitget API 连通性 ──
        api_ok = True
        try:
            self.exchange.fetch_usdt_balance()
        except Exception:
            api_ok = False
            issues.append("Bitget API 不可达")

        # DeepSeek 断路器
        circuit_broken = self.analyst.circuit_breaker_open()
        if circuit_broken:
            issues.append("DeepSeek 断路器已熔断")

        # ── 2. 仓位 TPSL 保护 (v3.6: 优先 position-level + 回退 plan order) ──
        bare_count = 0
        excess_count = 0

        # 一次性拉取原始仓位数据 (有 info.stopLoss/takeProfit)
        raw_positions = {}
        try:
            raw_positions = self.exchange.get_open_positions()
        except Exception:
            pass

        for p in positions:
            sym = p.get("symbol", "?")
            sym_full = f"{sym}/USDT:USDT"
            entry = p.get("entry_price", 0)
            side = p.get("side", "LONG")
            pos_side_str = "short" if side == "SHORT" else "long"

            # a) 先从仓位 info 检查 (place-pos-tpsl 设置的)
            has_sl = has_tp = False
            order_count = 0

            real_pos = raw_positions.get(sym_full, {})
            pos_sl, pos_tp = self._check_position_tpsl(real_pos)
            has_sl = pos_sl
            has_tp = pos_tp

            # b) 回退: 从计划单检查
            try:
                orders = self.exchange.exchange.fetch_open_orders(
                    sym_full, params={"stop": True}
                ) or []
                plan_sl, plan_tp, order_count = self._check_plan_orders(
                    orders, pos_side_str, entry
                )
                has_sl = has_sl or plan_sl
                has_tp = has_tp or plan_tp
            except Exception:
                pass

            if not has_sl or not has_tp:
                bare_count += 1
                missing = []
                if not has_sl: missing.append("SL")
                if not has_tp: missing.append("TP")
                issues.append(f"{sym} 裸仓！缺少{'/'.join(missing)}保护")

            # c) 超额订单检测
            if order_count > self.MAX_PLAN_ORDERS_PER_POS:
                excess_count += 1
                warnings.append(f"{sym} 计划单过多 ({order_count}个)，建议清理")

            # 记录开仓时间
            if sym_full not in self._position_open_times:
                self._position_open_times[sym_full] = _now

        if bare_count > 0:
            logger.error(f"🏥 健康警报: {bare_count}个裸仓")
        if excess_count > 0:
            logger.warning(f"🏥 超额计划单: {excess_count}个仓位")

        # ── 3. 信号流速 ──
        hours_since_signal = (
            (_now - self._last_signal_time).total_seconds() / 3600
            if self._last_signal_time else (_now - self._start_time).total_seconds() / 3600
        )
        if hours_since_signal > self.SIGNAL_DRY_HOURS_WARN:
            warnings.append(f"超过 {hours_since_signal:.0f}h 无信号")

        # ── 4. 卡单检测 ──
        current_syms = {p.get("symbol", "") for p in positions}
        for sym, open_time in list(self._position_open_times.items()):
            base = sym.replace("/USDT:USDT", "")
            if base not in current_syms:
                self._position_open_times.pop(sym, None)
                continue
            hours_open = (_now - open_time).total_seconds() / 3600
            if hours_open > self.STALE_HOURS_WARN:
                warnings.append(f"{base} 持仓超 {hours_open:.0f}h 未动")

        # ── 5. 保证金健康 (v3.6 新增) ──
        try:
            bal = self.exchange.fetch_usdt_balance()
            total_margin = sum(
                abs(p.get("margin", 0)) for p in positions
            )
            if total_margin > 0 and bal > 0:
                margin_ratio = total_margin / bal
                if margin_ratio > self.MARGIN_RATIO_WARN:
                    warnings.append(f"保证金占比 {margin_ratio*100:.0f}%，可用余额偏低")
        except Exception:
            pass

        # ── 6. 风险指标 ──
        try:
            risk = self.riskmon.get_status()
            daily_pnl = risk.get("pnl_pct", 0)
            if daily_pnl < -3:
                issues.append(f"日内亏损 {daily_pnl:.1f}%，接近锁仓阈值")
            if risk.get("blocked"):
                issues.append("日内风控已锁仓，暂停交易")
        except Exception:
            pass

        # ── 7. 错误率 ──
        if self._error_count > 20:
            issues.append(f"累计异常 {self._error_count} 次")

        # ── 汇总 ──
        status = {
            "timestamp": _now.isoformat(),
            "cycle": cycle,
            "runtime_hours": round((_now - self._start_time).total_seconds() / 3600, 1),
            "api_ok": api_ok,
            "circuit_breaker": circuit_broken,
            "positions_count": len(positions),
            "bare_positions": bare_count,
            "excess_plan_orders": excess_count,
            "hours_since_signal": round(hours_since_signal, 1),
            "error_count": self._error_count,
            "issues": issues,
            "warnings": warnings,
            "status": "CRITICAL" if issues else "WARNING" if warnings else "HEALTHY",
        }

        # 写 health.json
        try:
            tmp = self._health_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._health_path)
        except Exception:
            pass

        # 告警入日志
        for issue in issues:
            logger.error(f"🏥 健康警报: {issue}")
            self.tlogger.log_risk("HEALTH_CRITICAL", issue)

        return status
