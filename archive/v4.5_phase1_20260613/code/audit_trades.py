import json
from collections import defaultdict

trades = []
for fn in ['/root/BitgetBot/logs/trades_2026-06-03.jsonl',
           '/root/BitgetBot/logs/trades_2026-06-04.jsonl',
           '/root/BitgetBot/logs/trades_2026-06-05.jsonl',
           '/root/BitgetBot/logs/trades_2026-06-06.jsonl']:
    try:
        with open(fn) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                    if t.get('event') == 'POSITION_CLOSE':
                        trades.append(t)
                except (json.JSONDecodeError, KeyError):
                    pass
    except FileNotFoundError:
        print(f"⚠️ 日志文件不存在: {fn}")

# Dedup strategy: same (symbol, entry_price) → keep LAST occurrence (final PnL)
# This is because _closed_positions_done was broken, causing same position
# to be re-detected across multiple cycles.
groups = defaultdict(list)
for t in trades:
    key = (t['symbol'], t.get('entry_price', 0))
    groups[key].append(t)

deduped = []
dup_count = 0
dup_pnl = 0.0
for key, grp in groups.items():
    if len(grp) == 1:
        deduped.append(grp[0])
    else:
        # Keep the LAST one (most accurate PnL from exchange)
        last = max(grp, key=lambda x: x['timestamp'])
        deduped.append(last)
        # Calculate wasted PnL from duplicates
        for g in grp:
            if g is not last:
                dup_count += 1
                dup_pnl += g.get('pnl', 0)

raw_pnl = sum(t['pnl'] for t in trades)
real_pnl = sum(t['pnl'] for t in deduped)

print(f"原始: {len(trades)}笔 PnL={raw_pnl:+.2f}")
print(f"去重: {len(deduped)}笔 PnL={real_pnl:+.2f}")
print(f"移除: {dup_count}笔重复, 虚增PnL={dup_pnl:+.2f}")

# Per-day deduped
print("\n=== 去重后每日 PnL ===")
by_day = defaultdict(float)
for t in deduped:
    day = t['timestamp'][:10]
    by_day[day] += t.get('pnl', 0)
for day in sorted(by_day.keys()):
    print(f'{day}: {by_day[day]:+.2f}')

# Per-symbol deduped
print("\n=== 去重后按币种 ===")
by_sym = defaultdict(lambda: {"wins":0,"losses":0,"pnl":0.0})
for t in deduped:
    s = t['symbol']
    if t.get('pnl', 0) > 0:
        by_sym[s]['wins'] += 1
    else:
        by_sym[s]['losses'] += 1
    by_sym[s]['pnl'] += t.get('pnl',0)
for s,d in sorted(by_sym.items(), key=lambda x: x[1]['pnl']):
    tot = d['wins']+d['losses']
    wr = d['wins']/tot*100 if tot>0 else 0
    print(f'{s:5s} {tot:3d}笔 胜{d["wins"]:2d} PnL={d["pnl"]:+8.2f} 胜率{wr:.0f}%')

# Per-direction, per-reason
print("\n=== 去重后按方向 ===")
by_dir = defaultdict(lambda: {"count":0,"pnl":0.0,"wins":0})
for t in deduped:
    d = t['direction']
    by_dir[d]['count'] += 1
    by_dir[d]['pnl'] += t.get('pnl',0)
    if t.get('pnl', 0) > 0:
        by_dir[d]['wins'] += 1
for d,dd in sorted(by_dir.items()):
    wr = dd['wins']/dd['count']*100 if dd['count']>0 else 0
    print(f'{d:5s} {dd["count"]:3d}笔 PnL={dd["pnl"]:+8.2f} 胜率{wr:.0f}%')

print("\n=== 去重后按出场原因 ===")
by_reason = defaultdict(lambda: {"count":0,"pnl":0.0})
for t in deduped:
    r = t.get('close_reason','?')
    by_reason[r]['count'] += 1
    by_reason[r]['pnl'] += t.get('pnl',0)
for r,d in sorted(by_reason.items(), key=lambda x: x[1]['pnl']):
    print(f'{r:14s} {d["count"]:3d}笔 PnL={d["pnl"]:+8.2f}')

# Last trade per symbol shows final state
print("\n=== 去重后详细（最近10笔） ===")
for t in sorted(deduped, key=lambda x: x['timestamp'])[-10:]:
    print(f"{t['timestamp'][:16]} {t['symbol']:5s} {t['direction']:5s} PnL={t['pnl']:+8.2f} pnl_pct={t['pnl_pct']:+7.1f}% {t['close_reason']}")
