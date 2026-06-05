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
import unicodedata
from time_utils import now
from dotenv import load_dotenv

# Windows 终端 UTF-8 修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


def get_exchange():
    ex = ccxt.bitget({
        "apiKey": os.getenv("BITGET_API_KEY"),
        "secret": os.getenv("BITGET_SECRET"),
        "password": os.getenv("BITGET_PASSPHRASE"),
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })
    ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def _disp_width(s: str) -> int:
    """计算字符串的终端显示宽度（CJK 字符占 2 列）"""
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def _pad(s: str, width: int, align: str = "<") -> str:
    """按显示宽度对齐填充字符串"""
    dw = _disp_width(s)
    pad = max(0, width - dw)
    if align == ">":
        return " " * pad + s
    elif align == "^":
        left = pad // 2
        right = pad - left
        return " " * left + s + " " * right
    return s + " " * pad


def _hline(widths: list, sep: str = "┼") -> str:
    """画表格线，CJK 字符 ─ 占两列"""
    parts = []
    for w in widths:
        parts.append("─" * w)
    return sep.join(parts)


def print_dashboard(ex):
    bal = ex.fetch_balance()
    total = float(bal.get("total", {}).get("USDT", 0))
    free = float(bal.get("free", {}).get("USDT", 0))

    positions = ex.fetch_positions()
    pos_list = []
    total_upl = 0
    total_margin = 0
    longs = 0
    shorts = 0
    bare = 0

    for p in positions:
        c = float(p.get("contracts", 0))
        if c == 0:
            continue
        sym_full = p["symbol"]
        base = sym_full.replace("/USDT:USDT", "")
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        upl = float(p.get("unrealizedPnl", 0))
        mgn = float(p.get("initialMargin", 0))
        side = p.get("side", "?")
        total_upl += upl
        total_margin += mgn
        if side == "long":
            longs += 1
        else:
            shorts += 1

        info = p.get("info", {})
        # v4.0 fix: Bitget place-pos-tpsl 把 SL/TP 挂在持仓 info 上
        # fetch_positions 返回 info.stopLoss / info.takeProfit (不是 stopLossPrice)
        has_sl = bool(info.get("stopLoss", "") or info.get("stopLossPrice", ""))
        has_tp = bool(info.get("takeProfit", "") or info.get("takeProfitPrice", ""))
        if not has_sl or not has_tp:
            bare += 1

        sl_val = info.get("stopLoss", "") or info.get("stopLossPrice", "")
        tp_val = info.get("takeProfit", "") or info.get("takeProfitPrice", "")
        roi = upl / mgn * 100 if mgn > 0 else 0
        pos_list.append({
            "symbol": base, "side": side, "entry": entry, "mark": mark,
            "upl": upl, "margin": mgn, "roi": roi,
            "has_sl": has_sl, "has_tp": has_tp,
            "sl": sl_val, "tp": tp_val,
        })

    equity = total
    initial = float(os.getenv("INITIAL_EQUITY", "10000"))
    all_time = (equity / initial - 1) * 100
    margin_pct = total_margin / equity * 100 if equity > 0 else 0

    # ── Column widths (display columns, not chars) ──
    COLS = [6, 6, 8, 11, 11, 9, 8, 8]  # symbol, side, margin, entry, mark, upl, roi, protect
    HEADERS = ["币种", "方向", "保证金", "入场", "现价", "浮盈", "收益率", "保护"]
    ALIGNS = ["<", "<", ">", ">", ">", ">", ">", "<"]

    # ── Print ──
    print(f"\n╔{'═'*58}╗")
    print(f"║  DeepSeekQuantBot 实时面板  |  {now().strftime('%H:%M:%S')}  ║")
    print(f"╠{'═'*58}╣")
    print(f"║  权益:  {equity:>10.2f} USDT    累计: {all_time:+.2f}%  ║")
    print(f"║  余额:  {total:>10.2f} USDT    浮盈: {total_upl:+.2f}  ║")
    print(f"║  可用:  {free:>10.2f} USDT    保证金: {total_margin:.0f} ({margin_pct:.1f}%)  ║")
    print(f"║  初始:  {initial:>10.0f} USDT    持仓: {len(pos_list)}/8  做多:{longs} 做空:{shorts}  ║")

    alerts = []
    if bare > 0:
        alerts.append(f"⚠裸仓:{bare}")
    print(f"╚{'═'*58}╝")
    if alerts:
        print("  " + " | ".join(alerts))

    if pos_list:
        # Header
        header_parts = []
        for h, w, a in zip(HEADERS, COLS, ALIGNS):
            header_parts.append(_pad(h, w, a))
        print("  " + " │ ".join(header_parts))
        print("  " + _hline(COLS, "─┼─"))

        # Rows
        detail_lines = []
        for p in sorted(pos_list, key=lambda x: x["upl"]):
            if not p["has_sl"] and not p["has_tp"]:
                protect = "🔴裸仓"
            elif not p["has_sl"]:
                protect = "⚠️缺SL"
            elif not p["has_tp"]:
                protect = "⚠️缺TP"
            else:
                protect = "🛡️"
            vals = [
                _pad(p["symbol"], COLS[0], ALIGNS[0]),
                _pad(p["side"], COLS[1], ALIGNS[1]),
                _pad(f"{p['margin']:.0f}", COLS[2], ">"),
                _pad(f"{p['entry']:.4f}", COLS[3], ">"),
                _pad(f"{p['mark']:.4f}", COLS[4], ">"),
                _pad(f"{p['upl']:+.2f}", COLS[5], ">"),
                _pad(f"{p['roi']:+.1f}%", COLS[6], ">"),
                _pad(protect, COLS[7], "<"),
            ]
            print("  " + " │ ".join(vals))
            # v4.0: 显示具体 SL/TP 价格
            if p["sl"] or p["tp"]:
                sl_str = f"SL:{float(p['sl']):.4f}" if p["sl"] else "SL:—"
                tp_str = f"TP:{float(p['tp']):.4f}" if p["tp"] else "TP:—"
                print("  " + " " * 14 + f"{sl_str}  {tp_str}")

        print("  " + _hline(COLS, "─┴─"))
        print(f"  总浮盈: {total_upl:+.2f} USDT    |  做多{longs} 做空{shorts}")

    # Alerts
    worst = min(pos_list, key=lambda x: x["upl"], default=None)
    if worst and worst["upl"] < -30:
        print(f"  ⚠️  最大浮亏: {worst['symbol']} {worst['upl']:+.2f}")
    best = max(pos_list, key=lambda x: x["upl"], default=None)
    if best and best["upl"] > 50 and best["roi"] > 20:
        print(f"  💡 可锁仓: {best['symbol']} 浮盈{best['upl']:+.2f} (ROI {best['roi']:.0f}%)")
    if abs(longs - shorts) > len(pos_list) * 0.7 and len(pos_list) >= 2:
        print(f"  ⚠️  方向失衡: 做多{longs} 做空{shorts} (单向占比过高)")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    loop = "--loop" in sys.argv or "-l" in sys.argv
    ex = get_exchange()
    print_dashboard(ex)
    if loop:
        try:
            while True:
                time.sleep(30)
                print_dashboard(ex)
        except KeyboardInterrupt:
            print("\n退出.")
