# DeepSeekQuantBot v4.5 — 量化信号 + AI 研究顾问混合交易机器人

> Bitget 沙箱 U本位合约 | 每 5 分钟扫描 | 量化决策引擎 | AI 研究层 (非决策) | 7 时段波动模型 | 纯量化遗传进化 | 禁止对锁 | R:R影子模式 | 沙箱风控统一 | Pullback Confirmation Attribution | Regime 切换观测

## 项目规则

### 角色
你是资深量化交易系统架构师。

### 语言要求
- 全程使用中文进行分析、注释、文档
- 所有 Commit 说明使用中文

### 架构要求
- 优先模块化
- 优先异步
- 优先风险控制
- 禁止未来函数

### 代码规范
- 类型提示
- dataclass
- 完整异常处理
- 日志必须详细

### 目标
提高实盘收益率和稳定性。

## 启动/停止

### Git 部署工作流 (主)

```bash
# 本地开发 → 提交推送
git add -A
git commit -m "feat: xxx"
git push origin main
```

### SSH 中文路径修复

Windows 用户名含中文 (`林华俊`) 导致 SSH 创建 `known_hosts` 时路径乱码。

**解决方法**: 显式指定密钥和 known_hosts 路径:
```bash
SSH_KEY=/c/Users/林华俊/.ssh/id_ed25519
KNOWN_HOSTS=/tmp/ssh_known_hosts

# GitHub Push
GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS" git push origin main --tags

# 服务器操作
ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS root@8.210.3.197 "<command>"
```

### 部署
```bash
# 一键: commit → push → 服务器 deploy
ssh -i /c/Users/林华俊/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/srv_known_hosts root@8.210.3.197 "cd /root/BitgetBot && git pull && echo 'BOT_VERSION='\$(git rev-parse --short HEAD) && bash deploy.sh"

# 仅重启
ssh ... root@8.210.3.197 "cd /root/BitgetBot && bash deploy.sh"
```

### 回滚

```bash
ssh root@8.210.3.197
cd /root/BitgetBot
git log --oneline -5                    # 找到目标 commit
git reset --hard <commit>               # 回退版本
bash deploy.sh                          # 重启
```

### 本地启动

```bash
python deepseek_quant_bot.py
```

### 备用部署 (GitHub 不可用时)

```powershell
.\sync.ps1                # MD5 对比 → SCP 上传变更 → 远程重启
.\sync.ps1 -DryRun        # 仅查看有无差异
.\sync.ps1 -NoRestart     # 上传但不重启 bot
.\sync.ps1 -Full          # 强制全量上传
```

### 服务器手动操作

```bash
ssh root@8.210.3.197
bash /root/BitgetBot/deploy.sh          # 一键部署 (推荐)
bash /root/BitgetBot/start.sh           # 仅启动 (不拉代码)
pkill -15 -f deepseek_quant_bot.py      # 优雅停止
cat /root/BitgetBot/deploy.log          # 部署历史
grep BOT_VERSION /tmp/bot.log           # 当前运行版本
tail -f /tmp/bot.log                    # 实时日志
```

## 架构 (主文件 `deepseek_quant_bot.py`)

### 内嵌模块 (10 个类)

| # | 模块 | 类名 | 职责 |
|---|------|------|------|
| 1 | 配置 | `ConfigManager` | .env 加载 + 50+ 策略参数 + AI 总开关 + 启动校验 |
| 2 | 交易所 | `ExchangeInterface` | ccxt Bitget 封装 (OI/费率/止损/平仓) |
| 3 | 指标 | `IndicatorCalculator` | EMA/RSI/ATR/ADX/MACD/布林 纯本地计算 |
| 4 | AI | `DeepSeekAnalyst` | **研究层** — 信号解释/持仓复盘/过滤诊断 + 断路器 (不做决策) |
| 5 | 情绪 | `MarketContextManager` | CoinGecko热门 + F&G 指数缓存 |
| 6 | 日志 | `TradeLogger` | JSONL 按日分割 `logs/trades_YYYY-MM-DD.jsonl` |
| 7 | 风控 | `RiskMonitor` | 日内亏损锁 + 日重置 + 手续费追踪 |
| 8 | 执行 | `TradeExecutor` | ADX 动态仓位 + 部分止盈 + Kelly 系数 |
| 9 | 时段 | `SessionManager` | 7 时段加密波动模型 (亚洲/欧洲/美股重叠/美股后半/亚洲凌晨/周末活跃/周末凌晨) |
| 10 | 主控 | `DeepSeekQuantBot` | 扫描→多TF→市场状态→量化评分→风控→下单 编排器 |

### 独立模块 (14 个文件)

| 文件 | 类名 | 职责 |
|------|------|------|
| `signal_scorer.py` | `SignalScorer` | **v4.1 核心** — 量化信号评分/置信度/Kalman方向确认/币种冷却/周末保护 |
| `self_learner.py` | `SelfLearner` `WisdomStore` `StrategyTracker` | 自学习引擎 — 交易反思/参数自调/智慧去库 |
| `genetic_evolver.py` | `GeneticEvolver` | **v4.1 纯量化** — 7参数变异→反事实回测→综合评分 (Sharpe 50%+Sortino 30%+PnL 20%)，AI 仅可选意见 |
| `equity_auditor.py` | `EquityAuditor` | **v4.1 新增** — 独立资金审计器，账户净值是唯一真相 |
| `flow_monitor.py` | `FlowMonitor` | OI 订单流 — 持仓量背离检测 (OI↑价↓=空头加仓) |
| `trailing_sl.py` | `TrailingStopManager` | 移动止损 — 盈利后追踪 SL 锁定利润 |
| `grid_strategy.py` | `GridManager` | 震荡市布林网格 |
| `news_integration.py` | `CryptoPanicClient` | 新闻情绪 + F&G 极端阈值 + 情绪量化分数 |
| `portfolio_manager.py` | `PortfolioManager` | BTC 相关性 + 信号排名 |
| `safety_manager.py` | `SafetyManager` | API 熔断/总敞口检查/紧急平仓 |
| `health_monitor.py` | `HealthMonitor` | 全链路健康检查 (TPSL/信号流速/保证金) |
| `performance_tracker.py` | `PerformanceTracker` | Sortino/Calmar/策略/方向/币种拆分 |
| `time_utils.py` | — | 统一 Asia/Shanghai 时区 (now/today_str/now_iso) |
| `dashboard.py` | — | 实时账户面板 |
| `audit_trades.py` | — | **v4.1 新增** — 离线交易审计脚本 |
| `check_balance.py` | — | **v4.1 新增** — 快速余额检查脚本 |

### v4.1 策略体系 (6 种信号)

| 策略 | 触发条件 | 适用市场 |
|------|---------|---------|
| `pullback` | RSI 超卖/超买 (干旱时自适应放宽) | 趋势回调 |
| `momentum` | ADX>25 + 价格突破近期高/低 | 强趋势突破 |
| `ema_cross` | EMA9 金叉/死叉 EMA21 | 趋势跟随 |
| `bollinger` | 触碰布林上下轨反弹 | 震荡均值回归 |
| `grid` | 低波动震荡 (GridManager 独立) | 横盘 |
| `counter_trend` | 趋势校验通过但逆势 (先于方向阻止通过) | 极端反转 |

### v4.1 保护层 (10 层)

| # | 保护 | 触发条件 |
|---|------|---------|
| 1 | 止损 | 价格触及 SL |
| 2 | 浮亏止损 | ROI < -30% + 持仓 >1h |
| 3 | 僵尸仓退出 | 持仓 >4h + \|ROI\| < 2% |
| 4 | 波动熔断 | ATR >2.5x 均值 → SL 收紧到 1.0x |
| 5 | 日内亏损锁 | 日亏 > `MAX_DAILY_LOSS_PCT` → 停止交易 |
| 6 | API 熔断 | 连续 5 次错误 → 暂停 |
| 7 | 方向开关 | 滚动 10 笔胜率 <30% → 暂停该方向 |
| 8 | **Kalman 方向确认** | 信号方向与 Kalman 趋势冲突 → 拒绝 (v4.1) |
| 9 | **币种止损冷却** | 同币种止损后 30min 禁止重新开仓 (v4.1) |
| 10 | **周末凌晨保护** | 低流动性时段只平仓不开仓 (v4.1) |

## v4.1 核心设计原则

**AI 不是决策者。** v4.0→v4.1 的关键转变：
- AI (`DeepSeekAnalyst`) 从 `review_signal()`/`review_position()` 决策者降级为**研究层**
- 所有 AI API 调用通过 `DEEPSEEK_MASTER_SWITCH` 总开关 + 6 子开关精细控制
- 遗传进化从 "DeepSeek 择优" 改为**纯量化综合评分**
- 信号置信度由 `SignalScorer` 纯本地计算，不依赖外部 API
- AI 解析失败时规则引擎继续正常运行 (默认策略: 宁可错过，不可盲开)

## 关键文件

| 文件 | 用途 |
|------|------|
| `.env` | API 密钥 + 50+ 策略参数 + v4.1 AI 开关 |
| `status.json` | 实时状态快照 |
| `health.json` | 健康检查报告 |
| `performance.json` | 绩效追踪数据 |
| `trading_wisdom.json` | 自学习智慧库 (LRU 淘汰, 最多 30 条) |
| `strategy_performance.json` | 策略/方向/币种绩效 |
| `logs/trades_YYYY-MM-DD.jsonl` | 按日分割的交易日志 |
| `grid_state.json` | 网格跨周期持久化 |
| `equity_snapshots.jsonl` | **v4.1 新增** — 权益审计快照 |

## 状态检查

```bash
# 账户面板 (直查交易所)
python dashboard.py              # 一次性快照
python dashboard.py --loop       # 每 30 秒刷新

# 离线审计
python audit_trades.py           # 交易审计
python check_balance.py          # 快速余额

# Bot 运行状态 (服务器)
ssh root@8.210.3.197 "ps aux | grep deepseek_quant | grep -v grep"
ssh root@8.210.3.197 "tail -30 /tmp/bot.log"
ssh root@8.210.3.197 "tail -5 /root/BitgetBot/logs/trades_$(date +%Y-%m-%d).jsonl"

# 查看健康状态
ssh root@8.210.3.197 "grep HEALTHY /root/BitgetBot/health.json"

# 查看最近交易
python -c "import json; [print(json.loads(l)) for l in open('logs/trades.jsonl').readlines()[-20:] if 'POSITION_CLOSE' in l or 'AI_DECISION' in l or 'CYCLE' in l]"
```

## Freqtrade (独立试点)

> 位于 `E:\freqtrade\` — 与主 bot 并行运行，纯英文路径避免 Python 3.14 中文 multiprocessing bug

### 安装 & 环境

```powershell
cd E:\freqtrade
.\run.ps1 list-strategies          # 验证环境 (自动设置 TEMP + UTF8 编码)
```

### 关键文件

| 文件 | 用途 |
|------|------|
| `E:\freqtrade\config.json` | Bitget Futures 配置 (API密钥留空, 用环境变量) |
| `E:\freqtrade\run.ps1` | 一键运行脚本 (自动修复中文路径 bug) |
| `E:\freqtrade\strategies\bitget_simple_strategy.py` | 精简策略 (pullback + bollinger, 1h) |
| `E:\freqtrade\strategies\bitget_hybrid_strategy.py` | 6 策略合一 (5m, 信号过多不推荐) |
| `E:\freqtrade\FREQTRADE_迁移指南.md` | 完整使用文档 |

### 常用命令

```powershell
cd E:\freqtrade

# 回测
.\run.ps1 backtesting -s BitgetSimpleStrategy --timerange 20250101-20260601 --fee 0.0006

# Hyperopt (多线程, 200 epochs ≈ 30min)
.\run.ps1 hyperopt -s BitgetSimpleStrategy --hyperopt-loss SharpeHyperOptLoss --spaces buy stoploss roi --epochs 200 -j 4

# 模拟交易
.\run.ps1 trade -s BitgetSimpleStrategy --dry-run

# 实盘
.\run.ps1 trade -s BitgetSimpleStrategy
```

### 回测基线 (2026-06-08 最终)

| 策略 | K线 | 笔数 | 盈亏 | 胜率 | Sharpe | 说明 |
|------|-----|------|------|------|--------|------|
| BitgetSimpleStrategy v4 | 1h | 478 | **+2.09%** | 48.5% | 9.30 | 🏆 最优 (Hyperopt 200 epochs) |
| BitgetSimpleStrategy v4+RSI43 | 1h | 487 | +2.42% | 48.7% | 10.58 | pullback活了但不赚钱 |
| BitgetSimpleStrategy v5 | 1h | 398 | -7.33% | 42.5% | -17.0 | 多变量同时改-崩盘 |
| BitgetHybridStrategy | 5m | 932 | -5.25% | 38.9% | -24.0 | 过度交易, 已弃用 |

**v4 最优参数**: stoploss=-0.221, ROI={0:0.349, 154:0.233, 638:0.072, 1952:0}, RSI=33/67, ADX=27

### 迭代教训
- **Hyperopt 是最有效的改进手段** (-0.06%→+2.09%), 比手写策略逻辑可靠
- **单变量实验 > 多变量同时改**: v5 改3-4个东西直接崩盘, 不知道哪个导致的
- **先统计再改码**: 20,000根K线分析证明 pullback 瓶颈是 RSI 而非 ADX
- **回测 +2% ≠ 实盘 +2%**: 92天太短, 没跑过 dry-run, 不能当真
- **项目已搁置**: 原 bot 服务器继续跑, Freqtrade 本机随时可恢复

### 核心差异 vs 主 Bot

| 维度 | DeepSeekQuantBot | Freqtrade |
|------|-----------------|-----------|
| Hedge 模式 | 支持 (但方向失衡) | One-way only |
| 止损管理 | 手写 pos-tpsl 映射 | 框架层自动 |
| 参数优化 | 手写遗传进化 | Hyperopt (optuna) |
| AI 审核 | DeepSeek (乱码 fallback) | 无 (纯规则) |
| 信号过滤 | 10 层保护 | Protections 插件 |

## 关键 gotcha

- **v4.1 AI 不是决策者** → 信号开仓由量化评分决定，AI 仅提供研究解释；`DEEPSEEK_SIGNAL_REVIEW_ENABLED` 默认 `false`
- **沙箱量数据不可靠** → 量比过滤自动跳过 (`if is_sandbox: vol_ratio=1.0`)
- **deepseek-v4-pro 回复在 `reasoning_content`** → `_merge_content_reasoning()` content 优先
- **AI 强制 JSON 输出** → prompt 要求纯 JSON，禁止分析文字；`_extract_json()` 有 7 步回退解析
- **AI 解析回退** → 研究层默认通过 (不影响决策)；`last_raw_info` 记录原始回复用于诊断
- **Bitget 止损单是 pos-tpsl** → `fetch_positions().info.stopLoss` 读取，**不是**独立计划单
- **`fetch_open_orders(stop=True)` 数不到 pos-tpsl 止损** → 用 `_has_position_tpsl()` 双重检测
- **Hedge 模式** → 不能用 `reduceOnly`/`holdSide`，用 `close_position(symbol, posSide)`
- **place-pos-tpsl** → 一次调用设 SL+TP，自动替换旧单
- **Server UTC+8** → `time_utils.now()` 统一时区，ISO 带 `+08:00`
- **沙箱 OI 可用** → `fetch_open_interest()` 正常返回；L/S 多空比沙箱不可用 (实盘可用)
- **出场优先规则引擎** → ROI>40%止盈 / ROI<-25%超1h止损 / 持仓>4h+|ROI|<2%僵尸仓
- **ADX 自适应** → floor=6, 极端干旱(10轮0信号)→直降到底
- **日亏损锁** → 读 `.env MAX_DAILY_LOSS_PCT`，默认 3%
- **1h/4h K线用 iloc[-2]** → 最后一根已闭合K线，避免未来函数
- **Kalman 方向冲突** → 信号方向与 Kalman 趋势相反时直接拒绝 (v4.1)
- **币种止损冷却** → 同币种止损后 30 分钟内禁止重新开仓，防止连环止损 (v4.1)
- **counter_trend 先于方向阻止通过** → 趋势校验→counter_trend 放行→方向阻止检查 (v4.1)
- **过度过滤**: ADX 阈值经 SessionManager+沙箱+干旱自适应后 floor=6, 实际瓶颈是 Kalman+STRATEGY_ROUTE+方向阻止三层组合过滤, ADX 只是替罪羊
- **Freqtrade 回测不可全信**: v4 +2.09% 仅 92 天验证期, 单一策略(bollinger), 90%僵尸仓出场, 未经历完整牛熊

## 关键 .env 配置

```ini
# Bitget
BITGET_API_KEY=
BITGET_SECRET=
BITGET_PASSPHRASE=
BITGET_SANDBOX=true           # 实盘改为 false

# AI
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-pro

# ── v4.1: AI 研究层开关 ──
DEEPSEEK_MASTER_SWITCH=true             # 总开关: false=关闭所有 AI API 调用
DEEPSEEK_SIGNAL_REVIEW_ENABLED=false    # AI 信号审核 (v4.1 默认关闭, 量化决定)
DEEPSEEK_EXPLAIN_SIGNAL_ENABLED=true    # AI 信号解释 (研究层, 非阻塞)
DEEPSEEK_GENETIC_OPINION_ENABLED=true   # AI 遗传进化意见 (仅供参考, 不驱动决策)
DEEPSEEK_REFLECTION_ENABLED=true        # AI 交易复盘
DEEPSEEK_RULE_GENERATION_ENABLED=true   # AI 规则生成
DEEPSEEK_PERIODIC_REVIEW_ENABLED=true   # AI 周期性持仓审查
DEEPSEEK_FILTER_ANALYSIS_ENABLED=true   # AI 过滤诊断 (为什么无信号)

# 策略核心
ADX_THRESHOLD=16              # 自动调参 floor=6, 极端干旱直降
VOL_RATIO_THRESHOLD=1.5       # 沙箱自动跳过
MAX_CONCURRENT_POSITIONS=8
RSI_OVERSOLD=40               # 遗传进化自动调
RSI_OVERBOUGHT=60             # 遗传进化自动调

# 风控
MAX_DAILY_LOSS_PCT=0.03       # 日内亏损硬锁 (默认3%, 从 .env 读取)
STALE_EXIT_HOURS=4.0          # 僵尸仓退出
STALE_ROI_LIMIT=0.02          # |ROI|<2% 触发
AUTO_SL_ENABLED=true
AUTO_SL_ROI_THRESHOLD=-0.30

# v4.0/v4.1 功能
SELF_LEARNER_ENABLED=true
TRAILING_SL_ENABLED=true
```

## 依赖

```
ccxt pandas numpy requests python-dotenv
```

Python 3.10+ (服务器 Ubuntu 22.04, Python 3.10.12)
