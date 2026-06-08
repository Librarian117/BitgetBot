#!/bin/bash
# start.sh - Bot 安全启动 (防重复 + PID 文件锁)
set -e

PIDFILE="/tmp/bot.pid"
LOGFILE="/tmp/bot.log"
BOTDIR="/root/BitgetBot"

# 1. 检查是否已经在运行
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Bot already running (PID $OLD_PID). Stop it first: kill $OLD_PID"
        exit 1
    fi
    rm -f "$PIDFILE"
fi

# 2. 清理旧进程 (以防万一)
pkill -9 -f deepseek_quant_bot.py 2>/dev/null || true
sleep 1

# 3. 启动 (单实例, bot 自己管理 PID 文件)
cd "$BOTDIR"
VERSION=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "BOT_VERSION=$VERSION" | tee -a "$LOGFILE"
nohup python3 -u deepseek_quant_bot.py >"$LOGFILE" 2>&1 &
BOT_PID=$!

# 4. 等待启动确认
sleep 4
MODULES=$(grep -c '已启用' "$LOGFILE" 2>/dev/null)
if [ $? -ne 0 ]; then MODULES=0; fi
echo "Bot started (PID $BOT_PID), modules: $MODULES"

# 5. 检查崩溃
if [ "$MODULES" -lt 5 ]; then
    echo "WARNING: Few modules loaded. Check $LOGFILE"
    grep -E '(ERROR|Traceback)' "$LOGFILE" | tail -3
fi
