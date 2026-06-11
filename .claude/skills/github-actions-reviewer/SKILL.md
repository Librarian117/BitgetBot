---
name: github-actions-reviewer
description: GitHub Actions CI/CD 自动审查 — Push→Test→Backtest→风险检查 流水线审计与优化
status: stable
---

# GitHub Actions Reviewer — 量化交易 CI/CD 自动化审查

当前项目采用手动部署（本地 `git push` + 服务器 `deploy.sh`），缺少自动化 CI 流水线。本 Skill 负责审查/提议/优化 GitHub Actions 工作流。

## 当前部署架构

```
本地 VS Code → git push origin main → ssh 服务器 → bash deploy.sh
                                                 ├─ git pull
                                                 ├─ 优雅停止 (kill -15 → kill -9)
                                                 └─ start.sh 重启
```

**痛点：**
- 无自动化测试（push 前不跑 lint / backtest）
- 无风险门禁（代码变更可能破坏止损/仓位逻辑）
- 部署依赖人工 ssh，凌晨发现问题无法自动回滚
- [deploy.sh](deploy.sh) 只在服务器执行，本地无 CI 验证

## 审查维度

### 1. CI 流水线审计

检查目标流水线是否覆盖以下阶段：

```
Push → Lint → Unit Test → Backtest → Risk Gate → Deploy
```

**审计清单：**

```bash
# 检查是否有 workflows 目录
ls -la .github/workflows/ 2>/dev/null || echo "❌ 无 GitHub Actions 工作流"

# 检查是否有测试文件
find . -name "test_*.py" -o -name "*_test.py" | head -20

# 检查是否有 lint 配置
ls pyproject.toml setup.cfg .ruff.toml 2>/dev/null || echo "❌ 无 lint 配置"

# 检查 deploy.sh 是否幂等
grep -n "kill\|pkill\|rm -f" deploy.sh
```

### 2. 回测自动化

交易策略变更必须有回测门禁：

```yaml
# .github/workflows/backtest.yml 核心逻辑
backtest:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with:
        python-version: '3.10'
    - run: pip install ccxt pandas numpy python-dotenv
    - name: 回测最近 7 天
      run: python audit_trades.py
      env:
        BITGET_API_KEY: ${{ secrets.BITGET_SANDBOX_KEY }}  # 只用沙箱
    - name: 亏损熔断检查
      run: |
        python -c "
        import json
        trades = [json.loads(l) for l in open('logs/trades.jsonl') if 'POSITION_CLOSE' in l]
        losses = [t for t in trades if t.get('pnl', 0) < 0]
        loss_rate = len(losses)/len(trades) if trades else 0
        assert loss_rate < 0.5, f'胜率过低: {(1-loss_rate)*100:.1f}%'
        print(f'✅ 回测胜率: {(1-loss_rate)*100:.1f}% ({len(losses)}/{len(trades)} 亏损)')
        "
```

### 3. 风险门禁

每次 push 必须通过以下检查才能部署：

**止损逻辑完整性：**
```bash
grep -c "set_position_sl_tp\|stopLoss\|SL_ROI\|AUTO_SL" deepseek_quant_bot.py
```

**仓位计算不超限：**
```bash
grep -c "MAX_CONCURRENT_POSITIONS\|max_positions\|position_size" deepseek_quant_bot.py
```

**风控参数不退化：**
```bash
# 检查 .env 中关键风控参数未被注释或设为零
grep -E "MAX_DAILY_LOSS_PCT|AUTO_SL_ENABLED|AUTO_SL_ROI_THRESHOLD" .env
```

### 4. 部署安全检查

部署前自动验证：

- [ ] `deploy.sh` 中的 `kill -15` + `kill -9` 逻辑正确
- [ ] `start.sh` 记录 `BOT_VERSION`（commit hash）
- [ ] 部署后健康检查通过（`grep HEALTHY health.json`）
- [ ] 服务器磁盘空间充足（`df -h`）
- [ ] 日志文件未无限增长（检查 `logrotate` 或日志大小限制）

## 建议的工作流文件

### `.github/workflows/ci.yml` — 基础 CI

```yaml
name: CI - Lint & Test
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.10' }
      - run: pip install ruff
      - run: ruff check . --select E,F,W  # 基础错误检查

  syntax:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.10' }
      - run: python -m py_compile deepseek_quant_bot.py
      - run: |
          for f in signal_scorer.py self_learner.py genetic_evolver.py equity_auditor.py \
                   flow_monitor.py trailing_sl.py grid_strategy.py safety_manager.py \
                   health_monitor.py performance_tracker.py portfolio_manager.py \
                   news_integration.py time_utils.py; do
            python -m py_compile "$f" && echo "✅ $f"
          done

  backtest:
    runs-on: ubuntu-latest
    needs: [lint, syntax]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.10' }
      - run: pip install ccxt pandas numpy python-dotenv
      - name: 离线审计（不连交易所）
        run: python audit_trades.py || echo "⚠️ 无交易历史可审计"
      - name: 策略语法完整性
        run: |
          python -c "
          from signal_scorer import SignalScorer
          from genetic_evolver import GeneticEvolver
          print('✅ 核心模块可导入')
          "
```

### `.github/workflows/risk-gate.yml` — PR 风险门禁

```yaml
name: Risk Gate
on:
  pull_request:
    branches: [main]

jobs:
  risk-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: 止损逻辑完整性
        run: |
          SL_COUNT=$(grep -c "stopLoss\|set_position_sl_tp\|AUTO_SL\|止损\|SL_ROI" deepseek_quant_bot.py || true)
          if [ "$SL_COUNT" -lt 2 ]; then
            echo "❌ 止损逻辑可能被删除！当前引用 $SL_COUNT 处（预期 >= 2）"
            exit 1
          fi
          echo "✅ 止损逻辑: $SL_COUNT 处引用"
      - name: 风控参数检查
        run: |
          if ! grep -q "MAX_DAILY_LOSS_PCT" .env; then
            echo "❌ MAX_DAILY_LOSS_PCT 缺失！"
            exit 1
          fi
          echo "✅ 风控参数完整"
      - name: 硬编码密钥检查
        run: |
          if grep -rn "sk-\|api_key.*=.*'[^$]\|secret.*=.*'[^$]" --include="*.py" . | grep -v ".env\|os.environ\|os.getenv\|ConfigManager\|load_dotenv"; then
            echo "❌ 发现潜在硬编码密钥！"
            exit 1
          fi
          echo "✅ 无硬编码密钥"
```

### `.github/workflows/deploy.yml` — 自动部署（可选，需 SSH secrets）

```yaml
name: Deploy to Server
on:
  push:
    branches: [main]
  workflow_dispatch:  # 手动触发

jobs:
  deploy:
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'  # 默认手动触发
    steps:
      - name: SSH 部署
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_HOST }}
          username: root
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: bash /root/BitgetBot/deploy.sh
      - name: 等待健康检查
        run: sleep 10
      - name: 验证部署
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_HOST }}
          username: root
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            grep HEALTHY /root/BitgetBot/health.json && echo "✅ 部署成功" || echo "❌ 健康检查失败"
            grep BOT_VERSION /tmp/bot.log | tail -1
```

## 自助审计命令

```bash
# 检查所有 Python 文件是否可编译
find . -name "*.py" ! -path "./.venv/*" ! -path "./venv/*" -exec python -m py_compile {} \; 2>&1 | grep -v "✅"

# 检查关键风控逻辑未被删除
for pattern in "stopLoss" "MAX_DAILY_LOSS_PCT" "AUTO_SL" "close_position" "set_position_sl_tp"; do
  count=$(grep -r "$pattern" --include="*.py" . | wc -l)
  echo "$pattern: $count 处引用"
done

# 检查 deploy.sh 同步性（本地 vs 服务器）
diff <(git show main:deploy.sh) <(ssh root@8.210.3.197 "cat /root/BitgetBot/deploy.sh") 2>/dev/null || echo "⚠️ 无法连接服务器对比"
```

## 输出格式

审查结束后生成：

```
## CI/CD 审查报告
### 缺失阶段
- [ ] Lint (ruff)
- [ ] Syntax Check (py_compile)
- [ ] Backtest (audit_trades.py)
- [ ] Risk Gate (止损/风控参数验证)
- [ ] Auto Deploy (SSH deploy.sh)

### 新增建议
- 添加 `.github/workflows/ci.yml`
- 添加 `.github/workflows/risk-gate.yml`
- 配置 GitHub Secrets (SERVER_HOST, SSH_PRIVATE_KEY)

### 风险项
- [file:line] 描述 | 严重程度 | 修复建议
```

## 相关文件

- [deploy.sh](deploy.sh) — 服务器部署脚本
- [start.sh](start.sh) — 启动脚本
- [sync.ps1](sync.ps1) — 备用 SCP 同步
- [equity_auditor.py](equity_auditor.py) — 资金审计
- [health_monitor.py](health_monitor.py) — 健康检查
- [audit_trades.py](audit_trades.py) — 离线交易审计
