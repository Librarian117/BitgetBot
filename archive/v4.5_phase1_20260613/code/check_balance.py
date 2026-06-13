#!/usr/bin/env python3
"""从交易所 API 拉取真实余额做最终对账"""
import json
import os

import ccxt

# Load .env
env = {}
with open('/root/BitgetBot/.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"')

ex = ccxt.bitget({
    'apiKey': env.get('BITGET_API_KEY', ''),
    'secret': env.get('BITGET_SECRET', ''),
    'password': env.get('BITGET_PASSPHRASE', ''),
    'options': {'defaultType': 'swap', 'sandbox': True},
})

bal = ex.fetch_balance()
usdt = bal.get('USDT', {})
print("=== Bitget 沙箱真实数据 ===")
print(f"USDT 可用余额: {usdt.get('free', 0):.2f}")
print(f"USDT 总权益:   {usdt.get('total', 0):.2f}")
print(f"已用保证金:    {usdt.get('used', 0):.2f}")

pos = ex.fetch_positions()
open_pos = [p for p in pos if float(p.get('contracts', 0) or 0) != 0]
print(f"\n持仓数: {len(open_pos)}")
total_upl = 0
for p in open_pos:
    upl = float(p.get('unrealizedPnl', 0) or 0)
    margin = float(p.get('initialMargin', 0) or 0)
    total_upl += upl
    print(f"  {p['symbol']} {p['side']} 张数={p['contracts']} 浮盈={upl:.2f} 保证金={margin:.2f}")

print(f"\n总浮盈: {total_upl:.2f}")
print("初始资金: 10000.00")
print(f"当前权益: {usdt.get('total', 0):.2f}")
print(f"累计盈亏: {usdt.get('total', 0) - 10000:.2f} ({(usdt.get('total', 0)/10000 - 1)*100:.1f}%)")

# 计算累计手续费
total_fees = 0
for fn in sorted(os.listdir('/root/BitgetBot/logs')):
    if not fn.startswith('trades_2026'):
        continue
    with open(f'/root/BitgetBot/logs/{fn}') as f:
        for line in f:
            line = line.strip()
            if not line or '"event": "CYCLE"' not in line:
                continue
            try:
                c = json.loads(line)
                total_fees = max(total_fees, c.get('cumulative_fees', 0))
            except (json.JSONDecodeError, KeyError):
                pass

print(f"\n累计手续费(日志): {total_fees:.2f}")
print("平仓PnL(去重后): 约+370 (之前审计)")
print(f"手续费:          -{total_fees:.2f}")
print("──────────────────────────")
print(f"交易净PnL估计:   {370 - total_fees:.2f}")
print(f"实际亏损:        {usdt.get('total', 0) - 10000:.2f}")
print(f"差额(持仓浮亏等): {(usdt.get('total', 0) - 10000) - (370 - total_fees):.2f}")
