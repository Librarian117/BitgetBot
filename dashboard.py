#!/usr/bin/env python3
"""
dashboard.py — 实时账户面板
============================
直接查 Bitget 交易所，不依赖 status.json。
用法: python dashboard.py        # 一次性快照
      python dashboard.py --loop # 每 30 秒刷新
"""
import ccxt
import os
import sys
import time
from time_utils import now
from dotenv import load_dotenv

load_dotenv()

def get_exchange():
    ex = ccxt.bitget({
        'apiKey': os.getenv('BITGET_API_KEY'),
        'secret': os.getenv('BITGET_SECRET'),
        'password': os.getenv('BITGET_PASSPHRASE'),
        'options': {'defaultType': 'swap'},
        'enableRateLimit': True,
    })
    ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex

def print_dashboard(ex):
    bal = ex.fetch_balance()
    total = float(bal.get('total', {}).get('USDT', 0))
    free = float(bal.get('free', {}).get('USDT', 0))
    float(bal.get('used', {}).get('USDT', 0))

    positions = ex.fetch_positions()
    pos_list = []
    total_upl = 0
    total_margin = 0
    longs = 0
    shorts = 0
    bare = 0 

    for p in positions:
        c = float(p.get('contracts', 0))
        if c == 0:
            continue
        sym_full = p['symbol']
        base = sym_full.replace('/USDT:USDT', '')
        entry = float(p.get('entryPrice', 0))
        mark = float(p.get('markPrice', 0))
        upl = float(p.get('unrealizedPnl', 0))
        mgn = float(p.get('initialMargin', 0))
        side = p.get('side', '?')
        total_upl += upl
        total_margin += mgn
        if side == 'long':
            longs += 1
        else:
            shorts += 1

        # Check SL/TP (v3.6: 优先仓位级别 TPSL)
        has_sl = False
        has_tp = False
        sl_count = 0
        # v3.6: 先查仓位 level 的 TPSL (place-pos-tpsl API 设置)
        info = p.get("info", {})
        if info.get("stopLoss", "") or info.get("stopLossPrice", ""):
            has_sl = True
            sl_count += 1
        if info.get("takeProfit", "") or info.get("takeProfitPrice", ""):
            has_tp = True
            sl_count += 1
        # 回退: 查独立 plan order
        if not has_sl or not has_tp:
            try:
                orders = ex.fetch_open_orders(sym_full, params={'stop': True}) or []
            except Exception:
                orders = []
            sl_count = max(sl_count, len(orders))
            for o in orders:
                tp_price = float(o.get('info', {}).get('triggerPrice', 0))
                if side == 'short':
                    if tp_price < entry:
                        has_tp = True
                    if tp_price > entry:
                        has_sl = True
                else:
                    if tp_price > entry:
                        has_tp = True
                    if tp_price < entry:
                        has_sl = True
        if not has_sl or not has_tp:
            bare += 1

        roi = upl / mgn * 100 if mgn > 0 else 0
        pos_list.append({
            'symbol': base, 'side': side, 'entry': entry, 'mark': mark,
            'upl': upl, 'margin': mgn, 'roi': roi,
            'sl_count': sl_count, 'has_tp': has_tp, 'has_sl': has_sl,
            'contracts': c,
        })

    equity = total  # ccxt total already includes unrealized PnL
    initial = float(os.getenv('INITIAL_EQUITY', '10000'))
    all_time = (equity / initial - 1) * 100
    margin_pct = total_margin / equity * 100 if equity > 0 else 0

    # ── Print Dashboard ──
    print(f"\n{'='*60}")
    print(f"  DeepSeekQuantBot 实时面板  |  {now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")
    print(f"  权益:  {equity:>10.2f} USDT    累计: {all_time:+.2f}%")
    print(f"  余额:  {total:>10.2f} USDT    浮盈: {total_upl:+.2f}")
    print(f"  可用:  {free:>10.2f} USDT    保证金: {total_margin:.0f} ({margin_pct:.1f}%)")
    print(f"  初始:  {initial:>10.0f} USDT    持仓: {len(pos_list)}/8  做多:{longs} 做空:{shorts}")
    if bare > 0:
        print(f"  ⚠️ 裸仓: {bare} 个!")
    print(f"{'='*60}")
    print(f"  {'币种':6s} {'方向':5s} {'入场':>10s} {'现价':>10s} {'浮盈':>8s} {'保证金':>7s} {'收益率':>7s} {'SL/TP':>5s}")
    print(f"  {'─'*68}")

    for p in sorted(pos_list, key=lambda x: x['upl']):
        sl_status = f"{p['sl_count']}"
        if not p['has_sl']:
            sl_status += '!SL'
        if not p['has_tp']:
            sl_status += '!TP'
        print(f"  {p['symbol']:6s} {p['side']:5s} {p['entry']:>10.4f} {p['mark']:>10.4f} {p['upl']:>+8.2f} {p['margin']:>7.0f} {p['roi']:>+6.1f}% {sl_status:>5s}")

    print(f"  {'─'*60}")
    print(f"  总浮盈: {total_upl:+.2f} USDT    |  做多{longs} 做空{shorts}")

    # Alerts
    if bare > 0:
        bare_names = [p['symbol'] for p in pos_list if p['sl_count'] == 0]
        print(f"  🚨 裸仓告警: {', '.join(bare_names)}")
    worst = min(pos_list, key=lambda x: x['upl'], default=None)
    if worst and worst['upl'] < -30:
        print(f"  ⚠️  最大浮亏: {worst['symbol']} {worst['upl']:+.2f}")
    best = max(pos_list, key=lambda x: x['upl'], default=None)
    if best and best['upl'] > 50 and best['roi'] > 20:
        print(f"  💡 可锁仓: {best['symbol']} 浮盈{best['upl']:+.2f} (ROI {best['roi']:.0f}%)")
    if abs(longs - shorts) > len(pos_list) * 0.7:
        print(f"  ⚠️  方向失衡: 做多{longs} 做空{shorts} (单向占比过高)")
    print(f"{'='*60}\n")

if __name__ == '__main__':
    loop = '--loop' in sys.argv or '-l' in sys.argv
    ex = get_exchange()
    print_dashboard(ex)
    if loop:
        try:
            while True:
                time.sleep(30)
                print_dashboard(ex)
        except KeyboardInterrupt:
            print('\n退出.')
