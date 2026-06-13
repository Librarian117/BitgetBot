#!/usr/bin/env python3
"""
Bot 巡检守护脚本 — 服务器 crontab 每 3 小时执行
功能: 检查 bot 存活 / 权益 / 最近交易 / 异常告警
P1: 数据源对齐 — status→data/status.json, trades→trade_ledger.jsonl
"""
import subprocess
import json
import os
import sys
from datetime import datetime, timezone, timedelta

BOT_DIR = "/root/BitgetBot"
LOG_FILE = "/tmp/bot.log"
HEALTH_FILE = f"{BOT_DIR}/health.json"
STATUS_FILE = f"{BOT_DIR}/data/status.json"       # P1: 当前 bot 实际写入路径
ALERT_FILE = f"{BOT_DIR}/alert.log"
LEDGER_FILE = f"{BOT_DIR}/logs/trade_ledger.jsonl" # P1: closed-trade SSoT

def run(cmd):
    return subprocess.getoutput(cmd).strip()

def log_alert(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ALERT_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"[WARN] {msg}")

issues = []

# 1. 进程存活检查
ps = run("ps aux | grep 'deepseek_quant_bot.py' | grep -v grep")
if not ps:
    issues.append("Bot 进程未运行!")
    print("[INFO] 尝试重启 bot...")
    result = run(f"cd {BOT_DIR} && bash start.sh")
    print(f"  重启结果: {result}")
else:
    pid = ps.split()[1]
    print(f"[OK] Bot 运行中 (PID {pid})")

# 2. 检查 status.json 权益
try:
    with open(STATUS_FILE) as f:
        status = json.load(f)
    acct = status.get("account", {})
    equity = acct.get("equity", status.get("balance", 0))
    initial = status.get("initial_equity", 10000)
    pnl_pct = (equity - initial) / initial * 100 if initial > 0 else 0
    positions = status.get("position_count", len(status.get("positions", [])))
    print(f"[INFO] 权益: {equity:.2f} ({pnl_pct:+.2f}%) | 持仓: {positions}")

    # 权益骤跌 >5% 告警
    prev_file = f"{BOT_DIR}/.prev_equity"
    if os.path.exists(prev_file):
        with open(prev_file) as f:
            prev_equity = float(f.read().strip())
        if prev_equity > 0:
            drop = (prev_equity - equity) / prev_equity * 100
            if drop > 5:
                issues.append(f"权益骤跌 {drop:.1f}%: {prev_equity:.2f} -> {equity:.2f}")
    with open(prev_file, "w") as f:
        f.write(str(equity))
except Exception as e:
    issues.append(f"无法读取 status.json: {e}")
    print(f"[WARN] 无法读取 status.json: {e}")

# 3. 检查最近日志中的错误
recent = run(f"tail -60 {LOG_FILE}")
error_count = recent.count("ERROR")
warning_count = recent.count("WARNING")
if error_count > 5:
    issues.append(f"日志中 {error_count} 个错误 (最近60行)")
print(f"[INFO] 日志: {error_count} 错误, {warning_count} 警告")

# 4. 检查最近交易 (P1: 优先从 trade_ledger.jsonl)
tz = timezone(timedelta(hours=8))
today = datetime.now(tz).strftime("%Y-%m-%d")
opens = closes = 0
ledger_ok = False

if os.path.exists(LEDGER_FILE):
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "TRADE_CLOSE":
                    close_ts = int(rec.get("close_ts", 0) or 0)
                    if close_ts > 0:
                        trade_day = datetime.fromtimestamp(close_ts).strftime("%Y-%m-%d")
                        if trade_day == today:
                            closes += 1
                    else:
                        closes += 1
        ledger_ok = True
    except Exception as e:
        print(f"[WARN] 读取 ledger 失败: {e}")

if ledger_ok:
    print(f"[INFO] 今日平仓: {closes} 笔 (来源: trade_ledger.jsonl)")
else:
    # P1 fallback: 旧日志路径
    trade_file = f"{BOT_DIR}/logs/trades_{today}.jsonl"
    if os.path.exists(trade_file):
        lines = run(f"tail -30 {trade_file}")
        opens = lines.count("POSITION_OPEN")
        closes = lines.count("POSITION_CLOSE")
        print(f"[INFO] 今日交易: {opens} 开仓, {closes} 平仓 (来源: 旧日志)")
    else:
        print("[INFO] 今日暂无交易记录")

# 5. 报告
if issues:
    print(f"\n{'='*50}")
    print(f"[ALERT] 发现 {len(issues)} 个问题:")
    for i in issues:
        print(f"  [X] {i}")
        log_alert(i)
    print(f"{'='*50}")
    sys.exit(1)
else:
    print(f"\n[OK] {datetime.now(tz).strftime('%H:%M:%S')} 巡检通过 - bot 正常运行")
