#!/bin/bash
# agent-verifier 快速扫描 — PostToolUse 钩子，每次编辑 .py 后运行 (< 1s)
# 仅检查最关键的 2 项：硬编码密钥 + 无限循环

FILE="$CLAUDE_FILE_PATH"
if [[ ! "$FILE" == *.py ]]; then
    exit 0
fi

WARNINGS=0

# ── 1. 硬编码密钥检测 ──
if grep -qn "sk-\|api_key\s*=\s*['\"]\w\|secret\s*=\s*['\"]\w\|passphrase\s*=\s*['\"]\w\|token\s*=\s*['\"]\w\{8,\}" "$FILE" 2>/dev/null | grep -v "os.environ\|os.getenv\|load_dotenv\|\.env\|ConfigManager\|#\|EXAMPLE\|example\|xxx\|your-" ; then
    echo "  🔑 AGENT-VERIFY: 疑似硬编码密钥 in $FILE"
    grep -n "sk-\|api_key\s*=\s*['\"]\w\|secret\s*=\s*['\"]\w\|passphrase\s*=\s*['\"]\w" "$FILE" 2>/dev/null | grep -v "os.environ\|os.getenv\|load_dotenv\|\.env\|ConfigManager\|EXAMPLE\|#"
    WARNINGS=$((WARNINGS + 1))
fi

# ── 2. 无限循环检测 ──
if grep -q "while True" "$FILE" 2>/dev/null; then
    # 检查每个 while True 后面是否有 break/return/exit
    python3 -c "
import re, sys
with open('$FILE', encoding='utf-8') as f:
    content = f.read()
# 找所有 while True:
pattern = r'while\s+True\s*:'
matches = list(re.finditer(pattern, content))
issues = []
for m in matches:
    # 检查该 while 块后续 50 行内是否有 break/return
    block = content[m.end():m.end()+2000]
    indent = len(m.group().split(':')[0]) + 4  # 块内缩进
    # 检查是否有 break/return/raise/sys.exit
    has_exit = bool(re.search(r'^[ ]{' + str(indent) + r',}(break|return|raise|sys\.exit|os\._exit)', block, re.MULTILINE))
    if not has_exit:
        line_no = content[:m.start()].count('\n') + 1
        issues.append(f'  ⚠️  while True 无退出条件 @ line {line_no}')
if issues:
    for i in issues:
        print(i)
    sys.exit(1)
" 2>/dev/null && true
    if [ $? -eq 1 ]; then
        WARNINGS=$((WARNINGS + 1))
    fi
fi

if [ $WARNINGS -gt 0 ]; then
    echo "  ⚡ agent-verifier: $WARNINGS warning(s) — 建议 commit 前运行 /agent-verifier"
else
    echo "  ✅ agent-verifier: quick scan OK"
fi
