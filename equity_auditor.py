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
                 ) -> Dict:
        """记录当前资金快照, 返回对账结果"""
        now = time.time()
        expected_equity = self.initial_equity + bot_realized_pnl - bot_fees
        deviation = exchange_equity - expected_equity

        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "exchange_equity": round(exchange_equity, 2),
            "exchange_balance": round(exchange_balance, 2),
            "exchange_margin": round(exchange_margin, 2),
            "exchange_upl": round(exchange_upl, 2),
            "bot_realized_pnl": round(bot_realized_pnl, 2),
            "bot_fees": round(bot_fees, 2),
            "expected_equity": round(expected_equity, 2),
            "deviation": round(deviation, 2),
            "open_positions": open_positions,
            "alarm": abs(deviation) > self.alarm_threshold,
        }
        if extra:
            entry.update(extra)

        self.history.append(entry)

        # 偏差报警
        if entry["alarm"]:
            self._alarm(deviation, entry)

        return entry

    def _alarm(self, deviation: float, entry: Dict):
        """偏差超过阈值时记录报警 (v4.1: 抑制重复报警 — 偏差变化<20%且<100USDT时不重复写日志)"""
        # 检查是否需要抑制重复报警
        if self.alarms:
            last = self.alarms[-1]
            last_dev = last["deviation"]
            dev_change = abs(deviation - last_dev)
            dev_pct_change = dev_change / (abs(last_dev) + 0.01)  # 避免除零
            if dev_pct_change < 0.20 and dev_change < 100:
                # 偏差未显著变化 → 更新计数但不写报警日志
                self.alarms.append({
                    "time": entry["time"], "deviation": deviation,
                    "msg": f"偏差持续: {deviation:+.2f} (抑制)",
                })
                return

        direction = "虚增" if deviation < 0 else "低估"
        msg = (
            f"🚨 资金对账偏差 {deviation:+.2f} USDT ! "
            f"交易所权益={entry['exchange_equity']:.2f} "
            f"bot预期={entry['expected_equity']:.2f} "
            f"({direction} |{abs(deviation):.0f}|)"
        )
        self.alarms.append({"time": entry["time"], "deviation": deviation, "msg": msg})
        # 写入独立报警日志
        self._write_alarm(entry, msg)

    def _write_alarm(self, entry: Dict, msg: str):
        """写入报警到 equity_alarms.jsonl"""
        with open(os.path.join(self.log_dir, "equity_alarms.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"msg": msg, **entry}, ensure_ascii=False) + "\n")

    def check(self) -> Dict:
        """返回当前对账状态"""
        if not self.history:
            return {"status": "no_data"}

        latest = self.history[-1]
        return {
            "status": "ALARM" if latest["alarm"] else "OK",
            "exchange_equity": latest["exchange_equity"],
            "expected_equity": latest["expected_equity"],
            "deviation": latest["deviation"],
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
