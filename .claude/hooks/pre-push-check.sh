#!/bin/bash
# pre-push-check.sh — commit/push/deploy 前自动审计
# UserPromptSubmit 钩子触发，覆盖 agent-verifier 5维度 + CI/CD 检查

set -e
cd "$(dirname "$0")/../.."

PASS=0
FAIL=0

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🔍 Pre-Push Agent Audit"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ═══════════════════════════════════════════
# Agent Verifier: 1. 无限循环检测
# ═══════════════════════════════════════════
echo ""
echo "── 1/5 无限循环检测 ──"
LOOPS=$(grep -rn "while True" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null || true)
if [ -n "$LOOPS" ]; then
    echo "$LOOPS" | while read line; do
        file=$(echo "$line" | cut -d: -f1)
        lineno=$(echo "$line" | cut -d: -f2)
        echo "  📍 $file:$lineno"
    done
    echo "  ⚠️  请确认每个 while True 都有 break/return/超时退出"
else
    echo "  ✅ 无 while True 循环"
fi

# ═══════════════════════════════════════════
# Agent Verifier: 2. 死锁检测
# ═══════════════════════════════════════════
echo ""
echo "── 2/5 死锁风险检测 ──"
# 检查 queue.get / Event.wait / Thread.join 是否有 timeout
NO_TIMEOUT=$(grep -rn "\.get()\|\.wait()\|\.join()" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null | grep -v "timeout" || true)
if [ -n "$NO_TIMEOUT" ]; then
    echo "$NO_TIMEOUT" | head -5
    echo "  ⚠️  以上调用缺少 timeout 参数 (可能阻塞)"
else
    echo "  ✅ 阻塞调用均有 timeout"
fi

# ═══════════════════════════════════════════
# Agent Verifier: 3. 幻觉工具调用
# ═══════════════════════════════════════════
echo ""
echo "── 3/5 已知API陷阱检测 ──"
# 检查 v4.1 已知 gotcha
if grep -rn "fetch_open_orders.*stop.*=.*True" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null | grep -qv "//\|#.*fetch_open_orders" ; then
    echo "  ⚠️  fetch_open_orders(stop=True) 查不到 pos-tpsl 止损单! (见 CLAUDE.md gotcha)"
    FAIL=$((FAIL + 1))
fi
if grep -rn "reduceOnly" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null | grep -qv "#\|CLAUDE" ; then
    echo "  ⚠️  Hedge 模式不支持 reduceOnly! (见 CLAUDE.md gotcha)"
    FAIL=$((FAIL + 1))
fi
if grep -rn "fetch_open_positions\|create_market_buy\|create_market_sell" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null; then
    echo "  ⚠️  疑似幻觉API调用 (ccxt无此方法)"
    FAIL=$((FAIL + 1))
fi
if [ $FAIL -eq 0 ]; then
    echo "  ✅ 未发现已知 API 陷阱"
fi

# ═══════════════════════════════════════════
# Agent Verifier: 4. Prompt 爆炸
# ═══════════════════════════════════════════
echo ""
echo "── 4/5 Prompt爆炸风险 ──"
# 检查 prompt 拼接是否有长度限制
PROMPT_LINES=$(grep -rn "prompt.*+=.*str\|prompt.*=.*f\"\|prompt.*=.*+=" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null || true)
PROMPT_COUNT=$(echo "$PROMPT_LINES" | grep -c "." 2>/dev/null || echo 0)
echo "  📊 Prompt 拼接点: $PROMPT_COUNT 处"
# 检查 len(prompt) 保护
LEN_CHECKS=$(grep -rn "len.*prompt\|prompt.*\[:" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null | wc -l)
if [ "$LEN_CHECKS" -eq 0 ]; then
    echo "  ⚠️  未发现 prompt 长度限制 (建议添加 len(prompt) 截断)"
else
    echo "  ✅ prompt 长度保护: $LEN_CHECKS 处"
fi

# ═══════════════════════════════════════════
# Agent Verifier: 5. 硬编码密钥
# ═══════════════════════════════════════════
echo ""
echo "── 5/5 硬编码密钥 ──"
LEAKS=$(grep -rn "sk-\|api_key\s*=\s*['\"]\w\|secret\s*=\s*['\"]\w\{4,\}\|passphrase\s*=\s*['\"]\w\{4,\}" --include="*.py" --include="*.sh" --include="*.json" . --exclude-dir=.venv --exclude-dir=.claude --exclude-dir=node_modules --exclude="*.jsonl" 2>/dev/null | grep -v "os\.environ\|os\.getenv\|load_dotenv\|\.env\|ConfigManager\|EXAMPLE\|example\|xxx\|your-\|#\|//" || true)
if [ -n "$LEAKS" ]; then
    echo "$LEAKS"
    echo "  🔴 发现疑似硬编码密钥!"
    FAIL=$((FAIL + 1))
else
    echo "  ✅ 未发现硬编码密钥"
fi

# ═══════════════════════════════════════════
# CI/CD 检查
# ═══════════════════════════════════════════
echo ""
echo "── CI/CD 审计 ──"

# 止损逻辑完整性
SL_COUNT=$(grep -rc "stopLoss\|set_position_sl_tp\|AUTO_SL\|止损\|SL_ROI" --include="*.py" . --exclude-dir=.venv --exclude-dir=.claude 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
echo "  📊 止损逻辑引用: $SL_COUNT 处 (>=2 正常)"

# GitHub Actions 工作流
if [ -d ".github/workflows" ]; then
    WF_COUNT=$(ls .github/workflows/*.yml 2>/dev/null | wc -l)
    echo "  ✅ GitHub Actions: $WF_COUNT 个工作流"
else
    echo "  ⚠️  无 .github/workflows/ 目录 (建议执行 /github-actions-reviewer)"
fi

# Python 语法完整性 — v4.5: 使用 py_compile (更可靠)
SYNTAX_OK=0
SYNTAX_FAILS=""
for f in deepseek_quant_bot.py signal_scorer.py self_learner.py genetic_evolver.py \
         equity_auditor.py flow_monitor.py trailing_sl.py grid_strategy.py \
         safety_manager.py health_monitor.py; do
    if [ -f "$f" ]; then
        if python3 -m py_compile "$f" 2>/dev/null; then
            SYNTAX_OK=$((SYNTAX_OK + 1))
        else
            echo "  ❌ 语法错误: $f"
            SYNTAX_FAILS="$SYNTAX_FAILS $f"
        fi
    else
        echo "  ⚠️  文件不存在: $f"
    fi
done
echo "  ✅ 核心模块语法: $SYNTAX_OK 通过"
[ -n "$SYNTAX_FAILS" ] && FAIL=$((FAIL + 1))

# 风控参数 — v4.5: daily_loss_limit (ConfigManager 内默认 5%)
if grep -qE "MAX_DAILY_LOSS_PCT|daily_loss_limit" .env 2>/dev/null; then
    echo "  ✅ 日亏损锁已配置"
else
    echo "  ⚠️  日亏损锁使用默认值 (ConfigManager daily_loss_limit=5%)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ $FAIL -eq 0 ]; then
    echo "  ✅ Pre-Push Audit 全部通过"
else
    echo "  🔴 $FAIL 项需要修复 (建议运行 /agent-verifier 和 /github-actions-reviewer)"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
