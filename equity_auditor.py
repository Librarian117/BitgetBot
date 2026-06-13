#!/usr/bin/env python3
"""
equity_auditor.py — 独立资金审计模块 v1.0
==========================================
独立于主 bot 的账户资金追踪器。每轮记录交易所真实权益,
与 bot 内部统计的 PnL 做对账, 偏差超过阈值时报警。

原则: 账户净值永远是真相。内部统计可能是错的。

用法:
  auditor = EquityAuditor(initial_equity=10000, alarm_threshold=50)
  auditor.snapshot(exchange_equity, bot_realized_pnl, bot_unrealized_pnl, fees)
  auditor.check()  # 偏差超阈值 → 报警
"""

import json
import os
import time
from typing import Dict, Optional


class EquityAuditor:
    """独立资金审计器 — 不与 bot 共享任何内部状态"""

    def __init__(self, initial_equity: float = 10000.0,
                 alarm_threshold: float = 50.0,
                 log_dir: str = "logs"):
        self.initial_equity = initial_equity
        self.alarm_threshold = alarm_threshold
        self.log_dir = log_dir
        self.history: list = []
        self.alarms: list = []

        os.makedirs(log_dir, exist_ok=True)

    def snapshot(self,
                 exchange_equity: float,       # 交易所 API 返回的总权益
                 exchange_balance: float,       # 可用余额
                 exchange_margin: float,        # 已用保证金
                 exchange_upl: float,           # 未实现盈亏
                 bot_realized_pnl: float,       # bot 统计的已实现 PnL
                 bot_fees: float,               # bot 统计的累计手续费
                 open_positions: int = 0,
                 extra: Optional[Dict] = None,
                 bot_funding_fees: float = 0.0,  # v4.5→Phase2: 累计资金费率
                 ) -> Dict:
        """记录当前资金快照, 返回对账结果。

        P1 Commit 3: 双口径审计。
        - realized_basis: initial + realized - fees - funding (bot 内部记账精度)
        - account_equity_basis: initial + realized + upl - fees - funding (对齐 Bitget accountEquity)
        仅当两个口径同时超阈值才报警。
        """
        now = time.time()
        # v4.5→Phase2: 资金费率纳入对账 (totalFee 可为正=支付/负=收取)
        costs = bot_fees + bot_funding_fees
        expected_realized = self.initial_equity + bot_realized_pnl - costs
        expected_with_upl = expected_realized + exchange_upl

        deviation_realized = exchange_equity - expected_realized
        deviation_with_upl = exchange_equity - expected_with_upl

        # P1: 双口径报警 — 仅当两者同时超阈值才触发
        alarm_realized = abs(deviation_realized) > self.alarm_threshold
        alarm_with_upl = abs(deviation_with_upl) > self.alarm_threshold
        # 无持仓时 with_upl = realized (upl=0), 两个口径等价, 只需看 realized
        alarm = alarm_realized and (alarm_with_upl or open_positions == 0)

        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "exchange_equity": round(exchange_equity, 2),
            "exchange_balance": round(exchange_balance, 2),
            "exchange_margin": round(exchange_margin, 2),
            "exchange_upl": round(exchange_upl, 2),
            "bot_realized_pnl": round(bot_realized_pnl, 2),
            "bot_fees": round(bot_fees, 2),
            "bot_funding_fees": round(bot_funding_fees, 4),
            # 旧字段 (向后兼容)
            "expected_equity": round(expected_realized, 2),
            "deviation": round(deviation_realized, 2),
            # P1: 双口径字段
            "expected_equity_realized": round(expected_realized, 2),
            "expected_equity_with_upl": round(expected_with_upl, 2),
            "deviation_realized": round(deviation_realized, 2),
            "deviation_with_upl": round(deviation_with_upl, 2),
            "open_positions": open_positions,
            "alarm": alarm,
        }
        if extra:
            entry.update(extra)

        self.history.append(entry)

        # 偏差报警
        if entry["alarm"]:
            self._alarm(deviation_realized, deviation_with_upl, entry)

        return entry

    def _alarm(self, deviation_realized: float, deviation_with_upl: float, entry: Dict):
        """P1: 双口径报警 — 同时记录两个偏差。

        偏差超过阈值时记录报警 (v4.1: 抑制重复报警 — 偏差变化<20%且<100USDT时不重复写日志)
        """
        # 用 with_upl 偏差做主判断 (更贴近 accountEquity 口径)
        deviation = deviation_with_upl
        # 检查是否需要抑制重复报警
        if self.alarms:
            last = self.alarms[-1]
            last_dev = last.get("deviation", 0)
            dev_change = abs(deviation - last_dev)
            dev_pct_change = dev_change / (abs(last_dev) + 0.01)  # 避免除零
            if dev_pct_change < 0.20 and dev_change < 100:
                # 偏差未显著变化 → 更新计数但不写报警日志
                self.alarms.append({
                    "time": entry["time"], "deviation": round(deviation, 2),
                    "deviation_realized": round(deviation_realized, 2),
                    "msg": f"偏差持续: {deviation:+.2f} (抑制)",
                })
                return

        direction = "虚增" if deviation < 0 else "低估"
        msg = (
            f"🚨 资金对账偏差 {deviation:+.2f} USDT ! "
            f"交易所权益={entry['exchange_equity']:.2f} "
            f"bot预期(realized)={entry['expected_equity_realized']:.2f} "
            f"bot预期(+upl)={entry['expected_equity_with_upl']:.2f} "
            f"({direction} |{abs(deviation):.0f}|)"
        )
        self.alarms.append({
            "time": entry["time"],
            "deviation": round(deviation, 2),
            "deviation_realized": round(deviation_realized, 2),
            "msg": msg,
        })
        # 写入独立报警日志
        self._write_alarm(entry, msg)

    def _write_alarm(self, entry: Dict, msg: str):
        """写入报警到 equity_alarms.jsonl"""
        with open(os.path.join(self.log_dir, "equity_alarms.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"msg": msg, **entry}, ensure_ascii=False) + "\n")

    def check(self) -> Dict:
        """返回当前对账状态 (P1: 含双口径)"""
        if not self.history:
            return {"status": "no_data"}

        latest = self.history[-1]
        return {
            "status": "ALARM" if latest["alarm"] else "OK",
            "exchange_equity": latest["exchange_equity"],
            "expected_equity": latest["expected_equity"],
            "expected_equity_realized": latest.get("expected_equity_realized", latest["expected_equity"]),
            "expected_equity_with_upl": latest.get("expected_equity_with_upl", latest["expected_equity"]),
            "deviation": latest["deviation"],
            "deviation_realized": latest.get("deviation_realized", latest["deviation"]),
            "deviation_with_upl": latest.get("deviation_with_upl", latest["deviation"]),
            "total_alarms": len(self.alarms),
            "samples": len(self.history),
        }

    def save(self):
        """持久化历史到 equity_audit.json"""
        path = os.path.join(self.log_dir, "equity_audit.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "initial_equity": self.initial_equity,
                "alarm_threshold": self.alarm_threshold,
                "history": self.history[-200:],  # 最近 200 条
                "alarms": self.alarms[-50:],     # 最近 50 条报警
            }, f, ensure_ascii=False, indent=2)
