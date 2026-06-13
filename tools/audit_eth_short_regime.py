#!/usr/bin/env python3
"""P1: ETH SHORT Regime Audit — compare profitable vs losing week"""
import json, os, sys
from collections import defaultdict, Counter

LOG_DIR = sys.argv[1] if len(sys.argv) > 1 else "logs"

# ── Load all ETH SHORT trades ──
eth_short_signals = []  # SIGNAL / SIGNAL_FULL events with pre-trade context
eth_short_closes = []   # POSITION_CLOSE events

for fn in sorted(os.listdir(LOG_DIR)):
    if not fn.startswith("trades_2026-") or not fn.endswith(".jsonl"):
        continue
    if fn < "trades_2026-06-01.jsonl":  # last 2 weeks
        continue
    with open(os.path.join(LOG_DIR, fn)) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                if d.get("symbol") != "ETH": continue
                if d.get("direction") != "SHORT": continue
                if d.get("event") == "POSITION_CLOSE":
                    eth_short_closes.append(d)
                elif d.get("event") in ("SIGNAL_FULL", "ENTRY_SNAPSHOT"):
                    eth_short_signals.append(d)
            except: pass

# ── Split into weeks ──
week1 = [t for t in eth_short_closes if "2026-06-01" <= t.get("timestamp","")[:10] <= "2026-06-07"]
week2 = [t for t in eth_short_closes if "2026-06-08" <= t.get("timestamp","")[:10] <= "2026-06-12"]

wk1_pnl = sum(float(t.get("pnl",0)) for t in week1)
wk2_pnl = sum(float(t.get("pnl",0)) for t in week2)

print("=" * 65)
print("P1: ETH SHORT Regime Audit")
print("=" * 65)
print(f"Week1 (Jun1-7): {len(week1)} trades, PnL={wk1_pnl:+.2f}")
print(f"Week2 (Jun8-12): {len(week2)} trades, PnL={wk2_pnl:+.2f}")

# ── 1. PnL Distribution ──
def describe(trades, label):
    if not trades: return
    pnls = sorted([float(t.get("pnl",0)) for t in trades])
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    print(f"\n{label} PnL分布:")
    print(f"  Mean={sum(pnls)/len(pnls):+.2f}  Median={pnls[len(pnls)//2]:+.2f}")
    print(f"  Wins={len(wins)}({len(wins)/len(pnls)*100:.0f}%) AvgWin={sum(wins)/len(wins):+.2f}" if wins else f"  Wins=0")
    print(f"  Losses={len(losses)}({len(losses)/len(pnls)*100:.0f}%) AvgLoss={sum(losses)/len(losses):+.2f}" if losses else f"  Losses=0")
    print(f"  Best={max(pnls):+.2f}  Worst={min(pnls):+.2f}")

describe(week1, "Week1")
describe(week2, "Week2")

# ── 2. Exit Reason ──
print("\n" + "─" * 40)
print("Exit Reason 对比:")
print(f"{'Reason':<20} {'Week1':>12} {'Week2':>12}")
reasons = set()
for t in week1 + week2:
    reasons.add(t.get("close_reason","?"))
for r in sorted(reasons):
    w1 = sum(float(t.get("pnl",0)) for t in week1 if t.get("close_reason")==r)
    w2 = sum(float(t.get("pnl",0)) for t in week2 if t.get("close_reason")==r)
    c1 = len([t for t in week1 if t.get("close_reason")==r])
    c2 = len([t for t in week2 if t.get("close_reason")==r])
    print(f"{r:<20} {w1:>+8.1f}({c1}t) {w2:>+8.1f}({c2}t)")

# ── 3. Holding Time ──
print("\n" + "─" * 40)
print("持仓时间对比:")
for label, trades in [("Week1", week1), ("Week2", week2)]:
    hours = [float(t.get("holding_hours",0)) for t in trades]
    print(f"  {label}: mean={sum(hours)/len(hours):.1f}h median={sorted(hours)[len(hours)//2]:.1f}h")

# ── 4. Signal quality from ENTRY_SNAPSHOT ──
print("\n" + "─" * 40)
print("入场信号特征对比 (ENTRY_SNAPSHOT):")
for label, wk_start, wk_end in [("Week1", "2026-06-01", "2026-06-07"), ("Week2", "2026-06-08", "2026-06-12")]:
    sigs = [s for s in eth_short_signals if wk_start <= s.get("timestamp","")[:10] <= wk_end]
    if not sigs:
        sigs = [s for s in eth_short_closes if wk_start <= s.get("timestamp","")[:10] <= wk_end]
        print(f"  {label}: {len(sigs)} signals (from CLOSE data)")

    # Extract available fields
    fields = {}
    for s in sigs:
        for k, v in s.items():
            if isinstance(v, (int, float)) and k not in ("pnl", "pnl_pct", "price", "balance", "equity"):
                if k not in fields: fields[k] = []
                fields[k].append(v)

    for k in sorted(fields.keys()):
        vals = fields[k]
        if len(vals) < 3: continue
        print(f"  {k}: mean={sum(vals)/len(vals):.2f} median={sorted(vals)[len(vals)//2]:.2f} range=[{min(vals):.2f},{max(vals):.2f}]")

# ── 5. Available fields in signals ──
print("\n" + "─" * 40)
print("ENTRY_SNAPSHOT 可用字段:")
if eth_short_signals:
    sample = eth_short_signals[0]
    for k, v in sorted(sample.items()):
        if isinstance(v, (int, float)):
            print(f"  {k} = {v}")
        elif isinstance(v, str) and len(v) < 100:
            print(f"  {k} = '{v}'")
        else:
            print(f"  {k} = {type(v).__name__}")

# ── 6. RSI / ADX / Confidence from CLOSE data ──
print("\n" + "─" * 40)
print("RSI/ADX/Confidence 对比 (from CLOSE):")
fields_of_interest = ["rsi", "adx", "confidence", "atr", "vol_ratio", "ema"]
for field in fields_of_interest:
    w1_vals = [float(t.get(field, 0)) for t in week1 if t.get(field) is not None]
    w2_vals = [float(t.get(field, 0)) for t in week2 if t.get(field) is not None]
    if not w1_vals or not w2_vals: continue
    print(f"  {field}:")
    print(f"    Week1: mean={sum(w1_vals)/len(w1_vals):.2f} median={sorted(w1_vals)[len(w1_vals)//2]:.2f}")
    print(f"    Week2: mean={sum(w2_vals)/len(w2_vals):.2f} median={sorted(w2_vals)[len(w2_vals)//2]:.2f}")

# ── 7. Market regime from cycle logs ──
print("\n" + "─" * 40)
print("市场 Regime 对比 (from CYCLE events):")
cycles = []
for fn in sorted(os.listdir(LOG_DIR)):
    if not fn.startswith("trades_2026-06-") or not fn.endswith(".jsonl"):
        continue
    with open(os.path.join(LOG_DIR, fn)) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                if d.get("event") == "CYCLE":
                    cycles.append(d)
            except: pass

regime_w1 = Counter()
regime_w2 = Counter()
for c in cycles:
    ts = c.get("timestamp","")[:10] if c.get("timestamp") else ""
    regime = c.get("market_regime","unknown")
    if "2026-06-01" <= ts <= "2026-06-07":
        regime_w1[regime] += 1
    elif "2026-06-08" <= ts <= "2026-06-12":
        regime_w2[regime] += 1

print(f"{'Regime':<20} {'Week1':>8} {'Week2':>8}")
for r in sorted(set(list(regime_w1.keys()) + list(regime_w2.keys()))):
    print(f"{r:<20} {regime_w1.get(r,0):>8} {regime_w2.get(r,0):>8}")

# ── 8. Kalman state from REGIME_CHANGE events ──
print("\n" + "─" * 40)
print("Regime 切换事件:")
regime_changes = []
for fn in sorted(os.listdir(LOG_DIR)):
    if not fn.startswith("trades_2026-06-") or not fn.endswith(".jsonl"):
        continue
    with open(os.path.join(LOG_DIR, fn)) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                if d.get("event") == "REGIME_CHANGE":
                    regime_changes.append(d)
            except: pass

for rc in regime_changes:
    print(f"  {rc.get('timestamp','?')[:16]} {rc.get('from_regime','?')} → {rc.get('to_regime','?')} (持续{rc.get('duration_hours',0):.1f}h)")

# ── Summary ──
print("\n" + "=" * 65)
print("关键发现")
print("=" * 65)
# Compare win rates and avg PnL
w1_wr = len([t for t in week1 if float(t.get("pnl",0))>0])/len(week1)*100 if week1 else 0
w2_wr = len([t for t in week2 if float(t.get("pnl",0))>0])/len(week2)*100 if week2 else 0
print(f"ETH SHORT 胜率: Week1={w1_wr:.0f}% → Week2={w2_wr:.0f}%")
print(f"ETH SHORT PnL:  Week1={wk1_pnl:+.2f} → Week2={wk2_pnl:+.2f}")
