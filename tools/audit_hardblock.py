#!/usr/bin/env python3
"""30-day Hard Block audit — run on server: python3 tools/audit_hardblock.py"""
import json, os, sys
from collections import defaultdict
from datetime import datetime

LOG_DIR = sys.argv[1] if len(sys.argv) > 1 else "logs"

all_closed = []
for fn in sorted(os.listdir(LOG_DIR)):
    if not fn.startswith("trades_2026-") or not fn.endswith(".jsonl"):
        continue
    day = fn.replace("trades_", "").replace(".jsonl", "")
    if day < "2026-05-13":
        continue
    with open(os.path.join(LOG_DIR, fn)) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                if d.get("event") == "POSITION_CLOSE":
                    all_closed.append(d)
            except: pass

all_closed.sort(key=lambda x: x.get("timestamp", ""))

# ── Hard Block Simulation ──
def sim_hard_block(trades):
    rolling = []
    blocked = {"LONG": False, "SHORT": False}
    events = []
    kept = []
    blocked_list = []

    for t in trades:
        d = t.get("direction", "?")
        if blocked.get(d, False):
            blocked_list.append(t)
            continue
        kept.append(t)
        rolling.append(t)
        if len(rolling) > 10:
            rolling.pop(0)

        for chk in ["LONG", "SHORT"]:
            dt = [r for r in rolling if r.get("direction") == chk]
            if len(dt) >= 5:
                wr = sum(1 for r in dt if float(r.get("pnl", 0)) > 0) / len(dt)
                was = blocked[chk]
                if wr < 0.30:
                    if not was:
                        events.append((t.get("timestamp", "")[:10], chk, "BLOCK", wr))
                    blocked[chk] = True
                else:
                    if was:
                        events.append((t.get("timestamp", "")[:10], chk, "UNBLOCK", wr))
                    blocked[chk] = False
    return kept, blocked_list, events

hb_kept, hb_blocked, hb_events = sim_hard_block(all_closed)

# ── Results ──
def pnl_sum(trades):
    return sum(float(t.get("pnl", 0)) for t in trades)

def win_rate(trades):
    if not trades: return 0
    return sum(1 for t in trades if float(t.get("pnl", 0)) > 0) / len(trades)

print("=" * 60)
print("30天 Hard Block 审计: May 13 - Jun 12, 2026")
print("=" * 60)

total = len(all_closed)
actual_pnl = pnl_sum(all_closed)
actual_wr = win_rate(all_closed)
print(f"总交易: {total}笔 | PnL={actual_pnl:+.2f} | WR={actual_wr*100:.0f}%")

long_t = [t for t in all_closed if t.get("direction") == "LONG"]
short_t = [t for t in all_closed if t.get("direction") == "SHORT"]
print(f"  LONG:  {len(long_t)}笔 PnL={pnl_sum(long_t):+.2f} WR={win_rate(long_t)*100:.0f}%")
print(f"  SHORT: {len(short_t)}笔 PnL={pnl_sum(short_t):+.2f} WR={win_rate(short_t)*100:.0f}%")

print()
print("─" * 40)
print("Q1-Q2: Hard Block 触发历史")
print("─" * 40)
blocks = [e for e in hb_events if e[2] == "BLOCK"]
unblocks = [e for e in hb_events if e[2] == "UNBLOCK"]
print(f"触发: {len(blocks)}次 | 解封: {len(unblocks)}次")
for e in hb_events:
    print(f"  {e[0]} {e[1]}: {e[2]} (WR={e[3]*100:.0f}%)")

print()
print("─" * 40)
print("Q3: 拦截的 trades")
print("─" * 40)
print(f"拦截总笔数: {len(hb_blocked)} ({len(hb_blocked)/total*100:.0f}% of all trades)")
b_wins = [t for t in hb_blocked if float(t.get("pnl", 0)) > 0]
b_loss = [t for t in hb_blocked if float(t.get("pnl", 0)) < 0]
print(f"  拦截盈利单: {len(b_wins)}笔 PnL={pnl_sum(b_wins):+.2f}")
print(f"  拦截亏损单: {len(b_loss)}笔 PnL={pnl_sum(b_loss):+.2f}")
print(f"  净效果: 避免 {-pnl_sum(hb_blocked):+.2f} USDT 损失")
print(f"  错过: {pnl_sum(b_wins):+.2f} USDT 盈利")

for d in ["LONG", "SHORT"]:
    db = [t for t in hb_blocked if t.get("direction") == d]
    if db:
        dw = [t for t in db if float(t.get("pnl", 0)) > 0]
        dl = [t for t in db if float(t.get("pnl", 0)) < 0]
        print(f"  {d}: {len(db)}笔 (win={len(dw)} loss={len(dl)}) PnL={pnl_sum(db):+.2f}")

# Duration of blocks
print()
print("─" * 40)
print("封锁持续时间")
print("─" * 40)
block_start = {}
for e in hb_events:
    if e[2] == "BLOCK":
        block_start[e[1]] = e[0]
    elif e[2] == "UNBLOCK" and e[1] in block_start:
        dur = (datetime.fromisoformat(e[0]) - datetime.fromisoformat(block_start[e[1]])).days
        print(f"  {e[1]}: {block_start[e[1]]} → {e[0]} ({dur}天)")
        del block_start[e[1]]
for d, start in block_start.items():
    print(f"  {d}: {start} → still blocked at end of period")

print()
print("─" * 40)
print("Q4-Q5: 30天对比")
print("─" * 40)
hb_pnl = pnl_sum(hb_kept)
hb_wr = win_rate(hb_kept)
print(f"{'指标':<20} {'实际':>12} {'Hard Block':>12}")
print(f"{'总PnL':<20} {actual_pnl:>+12.2f} {hb_pnl:>+12.2f}")
print(f"{'交易数':<20} {total:>12} {len(hb_kept):>12}")
print(f"{'胜率':<20} {actual_wr*100:>11.0f}% {hb_wr*100:>11.0f}%")
print(f"{'拦截交易':<20} {'0':>12} {len(hb_blocked):>12}")
diff = hb_pnl - actual_pnl
print(f"{'净差异':<20} {'':>12} {diff:>+12.2f}")
print()
if diff > 0:
    print(f"Hard Block 30天净收益: +{diff:.2f} USDT")
else:
    print(f"Hard Block 30天净损失: {diff:.2f} USDT")

# Simulate Soft Block (half-size on degraded)
def sim_soft_block(trades):
    rolling = []
    degraded = {"LONG": False, "SHORT": False}
    adjusted = []
    for t in trades:
        d = t.get("direction", "?")
        mult = 0.5 if degraded.get(d, False) else 1.0
        pnl = float(t.get("pnl", 0)) * mult
        adjusted.append(pnl)
        rolling.append(t)
        if len(rolling) > 10:
            rolling.pop(0)
        for chk in ["LONG", "SHORT"]:
            dt = [r for r in rolling if r.get("direction") == chk]
            if len(dt) >= 5:
                wr = sum(1 for r in dt if float(r.get("pnl", 0)) > 0) / len(dt)
                degraded[chk] = wr < 0.30
    return sum(adjusted)

sb_pnl = sim_soft_block(all_closed)
print(f"Soft Block (v4.2) PnL: {sb_pnl:+.2f} (vs 实际 {actual_pnl:+.2f})")

# Direction-specific drift
print()
print("─" * 40)
print("方向漂移分析")
print("─" * 40)
# Check: would Hard Block have caused deadlock?
# A deadlock = direction stays blocked while market has shifted
# Look at per-week PnL by direction
for wk_start in ["2026-05-18", "2026-05-25", "2026-06-01", "2026-06-08"]:
    wk_end_dates = {"2026-05-18": "2026-05-24", "2026-05-25": "2026-05-31",
                    "2026-06-01": "2026-06-07", "2026-06-08": "2026-06-12"}
    wk_end = wk_end_dates.get(wk_start, wk_start)
    wk_trades = [t for t in all_closed if wk_start <= t.get("timestamp", "")[:10] <= wk_end]
    if not wk_trades: continue
    wk_long = pnl_sum([t for t in wk_trades if t.get("direction") == "LONG"])
    wk_short = pnl_sum([t for t in wk_trades if t.get("direction") == "SHORT"])
    print(f"{wk_start}~{wk_end}: LONG={wk_long:+.1f} SHORT={wk_short:+.1f} Total={wk_long+wk_short:+.1f} ({len(wk_trades)}t)")

print()
print("=" * 60)
print("最终判断")
print("=" * 60)
# The key question: was Hard Block solving a real problem or over-fixing a rare one?
# Answer based on: number of triggers, duration, missed wins vs avoided losses
if len(blocks) <= 2 and pnl_sum(b_wins) > abs(pnl_sum(b_loss)) * 0.5:
    print("v4.2 在过度修复一个罕见问题")
    print("Hard Block 30天只触发2次，且拦截了大量盈利单")
elif len(blocks) <= 2 and abs(pnl_sum(b_loss)) > pnl_sum(b_wins) * 3:
    print("v4.2 在解决一个真实但低频的问题")
    print("Hard Block 触发少但单次拦截价值极高")
else:
    print("需要更细粒度的分析")
