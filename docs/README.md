# DeepSeekQuantBot v4.5 → Phase 2 实盘 Canary

> Bitget U本位合约 | 6 策略 | 10 层风控 | AI 研究层 | 50U 实盘验证

## 固定阅读路径

```
① docs/README.md                        ← 你在这里
        ↓
② docs/architecture/overview.md         ← 系统架构
        ↓
③ docs/design/strategy-design.md        ← 策略设计
        ↓
④ AGENTS.md                             ← 审计规则 + Gotcha
        ↓
⑤ docs/audit/FINAL_TRADING_SYSTEM_AUDIT.md ← 实盘准入状态
```

## 项目结构

```
BitgetBot/
├── deepseek_quant_bot.py     # 主程序 (~3700行)
├── CLAUDE.md                 # 项目规则 + 参数冻结
├── AGENTS.md                 # Agent 审计规范
├── CHANGELOG.md              # 版本历史
│
├── config_manager.py         # 配置加载 + 验证
├── exchange_interface.py     # ccxt Bitget 封装
├── trade_executor.py         # 仓位计算 + 下单
├── trade_logger.py           # 结构化日志
├── signal_scorer.py          # 信号评分
├── indicator_calculator.py   # 技术指标
├── risk_monitor.py           # 风控监控
├── safety_manager.py         # 交易安全
├── session_manager.py        # 交易时段
├── market_context.py         # 市场情绪
├── equity_auditor.py         # 资金审计
├── performance_tracker.py    # 绩效跟踪
├── self_learner.py           # 自学习引擎
├── genetic_evolver.py        # 遗传进化
├── deepseek_analyst.py       # AI 研究层
│
├── dashboard.py              # 实时面板
├── check_balance.py          # 余额对账
├── audit_trades.py           # 离线审计
├── bot_watchdog.py           # 进程守护
│
├── docs/                     # 📚 项目文档
│   ├── README.md             #   导航入口
│   ├── architecture/         #   架构设计
│   │   └── overview.md
│   ├── design/               #   策略设计
│   │   ├── strategy-design.md
│   │   ├── kalman-role.md
│   │   ├── scoring-model.md
│   │   └── v4.5-filters.md
│   ├── operations/           #   运维指南
│   │   ├── deploy.md
│   │   └── troubleshooting.md
│   ├── roadmap/              #   路线图
│   │   ├── NEXT.md
│   │   └── ai-copilot-upgrade-plan.md
│   ├── archive/              #   历史文档
│   │   ├── V4.5_PLAN.md
│   │   ├── freqtrade-migration.md
│   │   └── legacy-knowledge.md
│   └── audit/                # 🔍 审计报告
│       ├── FINAL_TRADING_SYSTEM_AUDIT.md  ← 实盘准入
│       ├── PNL_AUDIT.md                   ← PnL 计算链
│       ├── EQUITY_AUDIT.md                ← 权益计算链
│       ├── CLOSE_PATH_AUDIT.md            ← 平仓路径重构
│       └── PRE_LIVE_AUDIT.md              ← 部署前审计
│
├── archive/                  # 📦 版本封存
│   └── v4.5_phase1_20260613/ # Phase 1 模拟盘终态
│       ├── PHASE1_SUMMARY.md
│       ├── code/
│       ├── config/
│       └── docs/
│
├── data/                     # 📊 运行时数据 (gitignore)
├── logs/                     # 📋 交易日志 (gitignore)
│   ├── trades.jsonl          #   主交易日志
│   └── trade_ledger.jsonl    #   唯一交易账本
│
├── tests/                    # 测试
├── tools/                    # 专用审计工具
└── freqtrade/                # Freqtrade 独立试点
```

## 文档导航

| 目的 | 文档 |
|------|------|
| 系统架构 | [architecture/overview.md](architecture/overview.md) |
| 策略设计 | [design/strategy-design.md](design/strategy-design.md) |
| Kalman | [design/kalman-role.md](design/kalman-role.md) |
| 信号评分 | [design/scoring-model.md](design/scoring-model.md) |
| v4.5 过滤器 | [design/v4.5-filters.md](design/v4.5-filters.md) |
| 部署 | [operations/deploy.md](operations/deploy.md) |
| 排错 | [operations/troubleshooting.md](operations/troubleshooting.md) |
| 路线图 | [roadmap/NEXT.md](roadmap/NEXT.md) |
| **PnL 审计** | [**audit/PNL_AUDIT.md**](audit/PNL_AUDIT.md) |
| **权益审计** | [**audit/EQUITY_AUDIT.md**](audit/EQUITY_AUDIT.md) |
| **平仓重构** | [**audit/CLOSE_PATH_AUDIT.md**](audit/CLOSE_PATH_AUDIT.md) |
| **部署前审计** | [**audit/PRE_LIVE_AUDIT.md**](audit/PRE_LIVE_AUDIT.md) |
| **实盘准入** | [**audit/FINAL_TRADING_SYSTEM_AUDIT.md**](audit/FINAL_TRADING_SYSTEM_AUDIT.md) |

## 当前状态

| 项目 | 状态 |
|------|:---:|
| Phase 1 模拟盘 | ✅ 已封存 (-22.23%) |
| Phase 2 实盘代码 | ✅ 已准备 |
| 统一平仓出口 | ✅ 已重构 (11 路径 → 1) |
| P0 修复 | ✅ 已完成 |
| 实盘准入 | 🟡 PnL Trust 85/100 |

## 快速命令

```bash
# 本地
python deepseek_quant_bot.py     # 启动
python dashboard.py               # 面板

# 部署
git push origin main
ssh root@8.210.3.197 "cd /root/BitgetBot && git pull && bash deploy.sh"
```
