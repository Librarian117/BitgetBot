# DeepSeekQuantBot v4.5 — Agent 入口

> **一句话**：Bitget U本位合约量化交易机器人，6 策略 + 10 层风控，AI 仅研究层不做决策。

## 固定阅读路径

**任何 Agent / 模型 / 开发者 / 未来的你，都按这条路径走：**

```
① docs/README.md                   ← 你在这里
        ↓
② docs/architecture/overview.md    ← 系统怎么运作
        ↓
③ docs/design/strategy-design.md   ← 策略为什么这样设计
        ↓
④ AGENTS.md                        ← 审计规则 + 关键 Gotcha
```

预计 2-3 分钟完成。之后按需深入 `design/` 下的专题文档。

| 文档 | 内容 | 时长 |
|------|------|------|
| `architecture/overview.md` | 模块总览、数据流、组件关系 | ~5 min |
| `design/strategy-design.md` | 6 策略逻辑、风控链路、决策路径 | ~10 min |

## 架构快照

```
市场数据 → 指标计算 → 6 策略信号生成 → 多层过滤 → 风控校验 → 仓位计算 → 下单执行 → 持仓保护
```

| 层 | 职责 |
|----|------|
| 信号层 | pullback / momentum / ema_cross / bollinger / counter_trend / grid |
| 过滤层 | RSI / ADX / Kalman / Hurst / Regime / Confidence / OI / Funding / BTC联动 |
| 风控层 | 止损 / 日内亏损锁 / 方向阻止 / 币种冷却 / 波动熔断 / 僵尸仓退出 |
| 执行层 | ADX 动态仓位 + 部分止盈 + pos-tpsl 保护 |
| 研究层 | AI 信号解释 / 复盘 / 过滤诊断 (不做决策) |

## 快速参考

```bash
# 本地启动
python deepseek_quant_bot.py

# 部署到服务器 (一条命令)
ssh -i ~/.ssh/id_ed25519 root@8.210.3.197 "cd /root/BitgetBot && git pull && bash deploy.sh"

# 状态检查
python dashboard.py              # 账户面板
python audit_trades.py           # 离线审计
tail -f /tmp/bot.log             # 实时日志 (服务器)

# 回测 (Freqtrade 独立试点)
cd E:\freqtrade && .\run.ps1 backtesting -s BitgetSimpleStrategy --timerange 20250101-20260601
```

## 关键文件

| 文件 | 用途 |
|------|------|
| `deepseek_quant_bot.py` | 主程序 (编排器 + 信号生成 + 过滤 + 调度) |
| `signal_scorer.py` | 量化信号评分 / Kalman 方向确认 |
| `trade_executor.py` | 仓位计算 / 止损止盈 / 下单执行 |
| `exchange_interface.py` | ccxt Bitget 封装 (OI / 费率 / TPSL) |
| `risk_monitor.py` | 日内亏损锁 / 日重置 / 手续费追踪 |
| `genetic_evolver.py` | 纯量化遗传进化 (7 参数变异 → 反事实回测) |
| `.env` | API 密钥 + 50+ 策略参数 + AI 开关 |

## 深入阅读

| 文档 | 说明 |
|------|------|
| `docs/project_structure.md` | 完整模块文档 (信号 / 过滤 / 风控 / 执行 / 数据流) |
| `docs/project_understanding.md` | 系统认知 (策略层级 / 已知故障模式 / 审计姿态) |
| `CLAUDE.md` | 项目规则 + 架构 + 启动/部署 + 关键 gotcha |
| `AGENTS.md` | Agent 行为规范 + 审计维度定义 |
