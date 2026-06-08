#!/bin/bash
# deploy.sh - Git Pull + 优雅重启 + 部署记录
# 用法: bash /root/BitgetBot/deploy.sh
set -e

BOTDIR="/root/BitgetBot"
LOGFILE="/tmp/bot.log"
DEPLOYLOG="$BOTDIR/deploy.log"

cd "$BOTDIR"

# ─── 0. 拉取最新代码 ───
echo "[deploy] $(date '+%Y-%m-%d %H:%M:%S') pulling..." | tee -a "$DEPLOYLOG"
git pull origin main

COMMIT=$(git rev-parse --short HEAD)
echo "[deploy] commit: $COMMIT" | tee -a "$DEPLOYLOG"

# ─── 1. 优雅停止 ───
if [ -f /tmp/bot.pid ]; then
    OLD_PID=$(cat /tmp/bot.pid)
    echo "[deploy] stopping bot (PID $OLD_PID)..." | tee -a "$DEPLOYLOG"
    kill -15 "$OLD_PID" 2>/dev/null || true
    sleep 5
    # 还没退出则强制
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[deploy] force kill..." | tee -a "$DEPLOYLOG"
        kill -9 "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
else
    # 未找到 PID 文件，回退到进程名匹配
    echo "[deploy] no PID file, searching by process name..." | tee -a "$DEPLOYLOG"
    pkill -15 -f deepseek_quant_bot.py 2>/dev/null || true
    sleep 5
    pkill -9 -f deepseek_quant_bot.py 2>/dev/null || true
fi

# ─── 2. 启动 ───
echo "[deploy] starting bot..." | tee -a "$DEPLOYLOG"
bash "$BOTDIR/start.sh"

# ─── 3. 显示状态 ───
sleep 4
echo "[deploy] recent logs:" | tee -a "$DEPLOYLOG"
tail -5 "$LOGFILE" | tee -a "$DEPLOYLOG"
echo "[deploy] done: $(date '+%Y-%m-%d %H:%M:%S') commit=$COMMIT" | tee -a "$DEPLOYLOG"
