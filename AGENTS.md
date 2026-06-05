# DeepSeekQuantBot v3.7 — AI + 量化混合交易机器人

> Bitget 沙箱 U本位合约 | 每 5 分钟扫描 | DeepSeek V4 AI 决策 | 7 时段波动模型 | 遗传进化

## 启动/停止

```bash
# 本地启动
python deepseek_quant_bot.py

# 智能同步部署 (MD5 差异对比, 只上传变更文件)
.\sync.ps1                # 完整: 对比→上传变更→重启→显示日志
.\sync.ps1 -DryRun        # 仅查看有无差异, 不上传
.\sync.ps1 -NoRestart     # 上传但不重启 bot
.\sync.ps1 -Full          # 强制全量上传

# 服务器手动操作
ssh root@8.210.3.197
pkill -f deepseek_quant_bot.py        # 停止
bash /root/BitgetBot/start.sh         # 启动 (自动 nohup)
tail -f /tmp/bot.log                  # 查看日志
```

## 架构（主文件 `deepseek_quant_bot.py` ~4700 行）

### 内嵌模块（9 个类）

| # | 模块 | 类名 | 职责 |
|---|------|------|------|
| 1 | 配置 | `ConfigManager` | .env 加载 + 50+ 策略参数 + 启动校验 |
| 2 | 交易所 | `ExchangeInterface` | ccxt Bitget 封装 (OI/费率/止损/平仓) |
| 3 | 指标 | `IndicatorCalculator` | EMA/RSI/ATR/ADX/MACD/布林 纯本地计算 |
| 4 | AI | `DeepSeekAnalyst` | DeepSeek V4 审核信号/持仓 + 断路器 |
| 5 | 情绪 | `MarketContextManager` | CoinGecko热门 + F&G 指数缓存 |
| 6 | 日志 | `TradeLogger` | JSONL 按日分割 `logs/trades_YYYY-MM-DD.jsonl` |
| 7 | 风控 | `RiskMonitor` | 日内亏损锁(5%) + 日重置 + 手续费追踪 |
| 8 | 执行 | `TradeExecutor` | ADX 动态仓位 + 部分止盈 + Kelly 系数 |
| 9 | 时段 | `SessionManager` | 7 时段加密波动模型 (亚洲/欧洲/美股重叠/美股后半/亚洲凌晨/周末活跃/周末凌晨) |
| 10 | 主控 | `DeepSeekQuantBot` | 扫描→多TF→市场状态→AI→风控→下单 编排器 |

### 独立模块（11 个文件）

| 文件 | 类名 | 职责 |
|------|------|------|
| `self_learner.py` | `SelfLearner` `WisdomStore` `StrategyTracker` | 自学习引擎 — 交易反思/参数自调/智慧去重/紧急回顾 |
| `genetic_evolver.py` | `GeneticEvolver` | 遗传进化 — 7参数变异→反事实回测→DeepSeek择优 |
| `flow_monitor.py` | `FlowMonitor` | OI 订单流 — 持仓量背离检测 (OI↑价↓=空头加仓) |
| `trailing_sl.py` | `TrailingStopManager` | 移动止损 — 盈利后追踪 SL 锁定利润 |
| `grid_strategy.py` | `GridManager` | 震荡市布林网格 |
| `news_integration.py` | `CryptoPanicClient` | 新闻情绪 + F&G 极端阈值 + 情绪量化分数 |
| `portfolio_manager.py` | `PortfolioManager` | BTC 相关性 + 信号排名 |
| `safety_manager.py` | `SafetyManager` | API 熔断/总敞口检查/紧急平仓 |
| `health_monitor.py` | `HealthMonitor` | 全链路健康检查 (TPSL/信号流速/保证金) |
| `performance_tracker.py` | `PerformanceTracker` | Sortino/Calmar/策略/方向/币种拆分 |
| `time_utils.py` | — | 统一 Asia/Shanghai 时区 (now/today_str/now_iso) |
| `backtest.py` | — | Walk-forward 回测框架 |
| `dashboard.py` | — | 实时账户面板 |

### v3.7 策略体系（6 种信号）

| 策略 | 触发条件 | 适用市场 |
|------|---------|---------|
| `pullback` | RSI 超卖/超买 (干旱时自适应放宽) | 趋势回调 |
| `momentum` | ADX>25 + 价格突破近期高/低 | 强趋势突破 |
| `ema_cross` | EMA9 金叉/死叉 EMA21 | 趋势跟随 |
| `bollinger` | 触碰布林上下轨反弹 | 震荡均值回归 |
| `grid` | 低波动震荡 (GridManager 独立) | 横盘 |
| `counter_trend` | 趋势校验通过但逆势 | 极端反转 |

### v3.7 保护层 (7 层)

| # | 保护 | 触发条件 |
|---|------|---------|
| 1 | 止损 | 价格触及 SL |
| 2 | 浮亏止损 | ROI < -30% + 持仓 >1h |
| 3 | 僵尸仓退出 | 持仓 >4h + \|ROI\| < 2% |
| 4 | 波动熔断 | ATR >2.5x 均值 → SL 收紧到 1.0x |
| 5 | 日内亏损锁 | 日亏 >5% → 停止交易 |
| 6 | API 熔断 | 连续 5 次错误 → 暂停 |
| 7 | 方向开关 | 滚动 10 笔胜率 <30% → 暂停该方向 |

## 关键文件

| 文件 | 用途 |
|------|------|
| `.env` | API 密钥 + 50+ 策略参数 |
| `status.json` | 实时状态快照 |
| `health.json` | 健康检查报告 |
| `performance.json` | 绩效追踪数据 |
| `trading_wisdom.json` | 自学习智慧库 (LRU 淘汰, 最多 30 条) |
| `strategy_performance.json` | 策略/方向/币种绩效 |
| `logs/trades_YYYY-MM-DD.jsonl` | 按日分割的交易日志 |
| `grid_state.json` | 网格跨周期持久化 |

## 状态检查

```bash
# 账户面板 (直查交易所)
python dashboard.py              # 一次性快照
python dashboard.py --loop       # 每 30 秒刷新

# Bot 运行状态 (服务器)
ssh root@8.210.3.197 "ps aux | grep deepseek_quant | grep -v grep"
ssh root@8.210.3.197 "tail -30 /tmp/bot.log"
ssh root@8.210.3.197 "tail -5 /root/BitgetBot/logs/trades_2026-06-03.jsonl"

# 查看健康状态
ssh root@8.210.3.197 "grep HEALTHY /root/BitgetBot/health.json"

# 查看最近交易
python -c "import json; [print(json.loads(l)) for l in open('logs/trades.jsonl').readlines()[-20:] if 'POSITION_CLOSE' in l or 'AI_DECISION' in l or 'CYCLE' in l]"

# 10小时运行报告
python report.py  # 然后在服务器上运行
```

## 关键 gotcha

- **沙箱量数据不可靠** → 量比过滤自动跳过 (`if is_sandbox: vol_ratio=1.0`)
- **deepseek-v4-pro 回复在 `reasoning_content`** → `_merge_content_reasoning()` 合并两字段
- **Bitget 止损单** → `fetch_open_orders(stop=True)` 才能查到计划单
- **Hedge 模式** → 不能用 `reduceOnly`/`holdSide`，用 `close_position(symbol, posSide)`
- **place-pos-tpsl** → 一次调用设 SL+TP，自动替换旧单
- **Server UTC+8** → `time_utils.now()` 统一时区，ISO 带 `+08:00`
- **沙箱 OI 可用** → `fetch_open_interest()` 正常返回；L/S 多空比沙箱不可用 (实盘可用)
- **AI 解析回退** → 入口默认 REJECT (宁可错过)，出口默认 HOLD (保持现状)

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

# 策略核心
ADX_THRESHOLD=16
VOL_RATIO_THRESHOLD=1.5
MAX_CONCURRENT_POSITIONS=8
RSI_OVERSOLD=40               # 遗传进化自动调
RSI_OVERBOUGHT=60             # 遗传进化自动调

# 风控
MAX_DAILY_LOSS_PCT=0.05
STALE_EXIT_HOURS=4.0          # 僵尸仓退出
STALE_ROI_LIMIT=0.02          # |ROI|<2% 触发
AUTO_SL_ENABLED=true
AUTO_SL_ROI_THRESHOLD=-0.30

# v3.7 新功能
AI_POSITION_REVIEW_ENABLED=true
SELF_LEARNER_ENABLED=true
TRAILING_SL_ENABLED=true
```

## 依赖

```
ccxt pandas numpy requests python-dotenv
```

Python 3.10+ (服务器 Ubuntu 22.04, Python 3.10.12)
