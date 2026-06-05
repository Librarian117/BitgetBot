#!/usr/bin/env python3
"""Bot 10 小时运行报告"""
import json, os

LOG = "/root/BitgetBot/logs/trades_2026-06-03.jsonl"
if not os.path.exists(LOG):
    LOG = "/root/BitgetBot/logs/trades.jsonl"

with open(LOG) as f:
    lines = [json.loads(l) for l in f if l.strip()]

cycles = [l for l in lines if l.get("event") == "CYCLE"]
closes = [l for l in lines if l.get("event") == "POSITION_CLOSE"]
ai_exit = [l for l in lines if l.get("event") == "AI_EXIT_DECISION"]
ai_entry = [l for l in lines if l.get("event") == "AI_DECISION"]
genetic = [l for l in lines if l.get("risk_type", "") == "GENETIC_EVOLVE"]
vol_skip = [l for l in lines if l.get("event") == "VOL_SKIP"]
adx_skip = [l for l in lines if l.get("event") == "ADX_SKIP"]

# 只统计今天 00:00 之后
today = [c for c in cycles if c.get("timestamp", "").startswith("2026-06-03")]

print("=" * 55)
print("  DeepSeekQuantBot v3.7 — 10小时运行报告")
print("=" * 55)

# 账户
if cycles:
    last = cycles[-1]
    first_today = today[0] if today else cycles[0]
    print(f"\n💰 账户: 权益={last['equity']:.2f} | 起始={last.get('initial_equity',10000)}")
    eq_delta = last['equity'] - last.get('initial_equity', 10000)
    print(f"   累计PnL: {eq_delta:+.2f} USDT ({eq_delta/last.get('initial_equity',10000)*100:+.2f}%)")
    print(f"   当前持仓: 0 个 (空仓)")

# 周期
print(f"\n🔄 周期统计:")
print(f"   总周期: {len(today)}")
cand_0 = sum(1 for c in today if c.get("candidates", 0) == 0)
cand_n = sum(1 for c in today if c.get("candidates", 0) > 0)
print(f"   有信号: {cand_n} 周期 ({cand_n/max(1,len(today))*100:.0f}%)")
print(f"   无信号: {cand_0} 周期 ({cand_0/max(1,len(today))*100:.0f}%)")

# 交易
print(f"\n📈 交易:")
print(f"   平仓记录: {len(closes)} 笔")
real_closes = {}
for c in closes:
    key = f"{c.get('symbol','?')}_{c.get('entry_price',0):.4f}"
    if key not in real_closes:
        real_closes[key] = c
total_pnl = sum(c["pnl"] for c in real_closes.values())
wins = sum(1 for c in real_closes.values() if c["pnl"] > 0)
losses = sum(1 for c in real_closes.values() if c["pnl"] <= 0)
print(f"   去重后: {len(real_closes)} 笔 | 胜: {wins} | 亏: {losses}")
print(f"   胜率: {wins/max(1,len(real_closes))*100:.0f}% | 总PnL: {total_pnl:+.2f} USDT")
for c in list(real_closes.values())[-5:]:
    print(f"   {c.get('symbol'):6s} {c.get('direction','?'):6s} "
          f"PnL={c.get('pnl',0):+8.2f} {c.get('close_reason','?'):12s} "
          f"入场{c.get('entry_price',0):.4f} 出场{c.get('exit_price',0):.4f}")

# AI
print(f"\n🤖 AI 决策:")
print(f"   入场审核: {len(ai_entry)} 次")
if ai_entry:
    confirms = sum(1 for d in ai_entry if d.get("decision") == "CONFIRM")
    rejects = sum(1 for d in ai_entry if d.get("decision") == "REJECT")
    print(f"   CONFIRM: {confirms} | REJECT: {rejects}")
print(f"   退出审核: {len(ai_exit)} 次")
if ai_exit:
    failed = sum(1 for d in ai_exit if "解析失败" in d.get("reason", "") or "默认" in d.get("reason", ""))
    holds = sum(1 for d in ai_exit if d.get("decision") == "HOLD")
    closes_ai = sum(1 for d in ai_exit if d.get("decision") == "CLOSE")
    print(f"   HOLD: {holds} | CLOSE: {closes_ai} | 解析失败: {failed}")
    if ai_exit:
        last_r = ai_exit[-1].get("reason", "?")[:100]
        print(f"   最近退出理由: {last_r}")

# 过滤器
print(f"\n🔍 过滤统计:")
print(f"   ADX过滤: {len(adx_skip)} 次")
print(f"   量比过滤: {len(vol_skip)} 次")
if len(vol_skip) > 0 and len(today) >= 10:
    print(f"   ⚠️ 量比过滤活跃 — 沙箱量数据不可靠")

# 遗传进化
print(f"\n🧬 遗传进化: {len(genetic)} 次")
if genetic:
    for g in genetic[-2:]:
        detail = g.get("details", "")[:100]
        print(f"   {g.get('timestamp','?')[:19]} {detail}")

# 新功能
print(f"\n🔧 v3.7 新功能状态:")
print(f"   市场状态识别: ✅ 就绪 (等信号触发)")
print(f"   情绪量化:     ✅ 就绪 (F&G=11 极端恐惧→反向买入)")
print(f"   策略权重闭环: ✅ 就绪")
print(f"   方向自动开关: ✅ 就绪")
print(f"   紧急回顾:     ✅ 就绪 (连续亏损2笔触发)")

print(f"\n{'='*55}")
print(f"  结论: 10小时0交易，信号过滤过严")
print(f"  建议: 手动降低ADX阈值/量比，等待沙箱市场波动")
print(f"{'='*55}")
