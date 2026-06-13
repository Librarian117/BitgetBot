#!/usr/bin/env python3
"""
audit_trades.py — 离线交易审计工具
P1: 数据源切换到 trade_ledger.jsonl (TRADE_CLOSE), trade_id 去重
用法: python audit_trades.py [ledger_path]
"""
import json
import os
import sys
from collections import defaultdict

# P1: 默认读 trade_ledger.jsonl, 接受命令行参数
LEDGER_PATH = sys.argv[1] if len(sys.argv) > 1 else "logs/trade_ledger.jsonl"

trades = []
if not os.path.exists(LEDGER_PATH):
    print(f"[ERROR] Ledger file not found: {LEDGER_PATH}")
    print("用法: python audit_trades.py [ledger_path]")
    sys.exit(1)

with open(LEDGER_PATH, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("event") != "TRADE_CLOSE":
            continue
        # P1: ledger 字段映射到 audit 期望格式
        close_ts = int(t.get("close_ts", 0) or 0)
        from datetime import datetime
        timestamp = datetime.fromtimestamp(close_ts).strftime("%Y-%m-%dT%H:%M:%S") if close_ts > 0 else ""
        trades.append({
            "timestamp": timestamp,
            "symbol": t.get("symbol", ""),
            "direction": t.get("side", "LONG"),
            "entry_price": float(t.get("entry", 0) or 0),
            "pnl": float(t.get("pnl", 0) or 0),
            "pnl_pct": float(t.get("pnl_pct", 0) or 0),
            "close_reason": t.get("close_reason", "?"),
            "trade_id": t.get("trade_id", ""),
        })

if not trades:
    print(" 暂无已平仓交易记录")
    sys.exit(0)

# P1: trade_id 去重 (替代旧的 (symbol, entry_price) 手工去重)
seen = set()
deduped = []
dup_count = 0
dup_pnl = 0.0
for t in trades:
    tid = t["trade_id"]
    if tid and tid not in seen:
        seen.add(tid)
        deduped.append(t)
    elif not tid:
        # 无 trade_id 的仍保留 (兼容旧数据)
        deduped.append(t)
    else:
        dup_count += 1
        dup_pnl += t["pnl"]

raw_pnl = sum(t["pnl"] for t in trades)
real_pnl = sum(t["pnl"] for t in deduped)

print(f"原始: {len(trades)}笔 PnL={raw_pnl:+.2f}")
if dup_count > 0:
    print(f"去重: {len(deduped)}笔 PnL={real_pnl:+.2f}")
    print(f"移除: {dup_count}笔重复 (trade_id), 虚增PnL={dup_pnl:+.2f}")

# Per-day
print("\n=== 按日 PnL ===")
by_day = defaultdict(float)
for t in deduped:
    day = t["timestamp"][:10] if t["timestamp"] else "?"
    by_day[day] += t["pnl"]
for day in sorted(by_day.keys()):
    print(f"  {day}: {by_day[day]:+.2f}")

# Per-symbol
print("\n=== 按币种 ===")
by_sym = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
for t in deduped:
    s = t["symbol"]
    if t["pnl"] > 0:
        by_sym[s]["wins"] += 1
    else:
        by_sym[s]["losses"] += 1
    by_sym[s]["pnl"] += t["pnl"]
for s, d in sorted(by_sym.items(), key=lambda x: x[1]["pnl"]):
    tot = d["wins"] + d["losses"]
    wr = d["wins"] / tot * 100 if tot > 0 else 0
    print(f"  {s:5s} {tot:3d}笔 胜{d['wins']:2d} PnL={d['pnl']:+8.2f} 胜率{wr:.0f}%")

# Per-direction
print("\n=== 按方向 ===")
by_dir = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
for t in deduped:
    d = t["direction"]
    by_dir[d]["count"] += 1
    by_dir[d]["pnl"] += t["pnl"]
    if t["pnl"] > 0:
        by_dir[d]["wins"] += 1
for d, dd in sorted(by_dir.items()):
    wr = dd["wins"] / dd["count"] * 100 if dd["count"] > 0 else 0
    print(f"  {d:5s} {dd['count']:3d}笔 PnL={dd['pnl']:+8.2f} 胜率{wr:.0f}%")

# Per close_reason
print("\n=== 按出场原因 ===")
by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0})
for t in deduped:
    r = t.get("close_reason", "?")
    by_reason[r]["count"] += 1
    by_reason[r]["pnl"] += t["pnl"]
for r, d in sorted(by_reason.items(), key=lambda x: x[1]["pnl"]):
    print(f"  {r:25s} {d['count']:3d}笔 PnL={d['pnl']:+8.2f}")

# Recent trades
print("\n=== 最近10笔 ===")
for t in sorted(deduped, key=lambda x: x["timestamp"])[-10:]:
    ts = t["timestamp"][:16] if t["timestamp"] else "?"
    print(f"  {ts} {t['symbol']:5s} {t['direction']:5s} "
          f"PnL={t['pnl']:+8.2f} pnl_pct={t['pnl_pct']:+7.1f}% {t['close_reason']}")
