# DeepSeekQuantBot v4.5 系统架构总览

> 编排器: `deepseek_quant_bot.py` (~3500行) | 独立模块: 14个文件 | AI 研究层非决策 | 纯量化信号引擎

## 1. 数据流架构

```
┌──────────┐    ┌──────────────┐    ┌────────────┐    ┌──────────────┐
│  Market  │───>│ Exchange I/F │───>│ Indicators │───>│ 6 Strategies │
│ (Bitget) │    │ (ccxt 封装)   │    │ (EMA/RSI/   │    │ (pullback/   │
└──────────┘    │ OI/费率/TPSL  │    │  ATR/ADX/   │    │  momentum/   │
                └──────────────┘    │  MACD/布林)  │    │  ema_cross/  │
                                    └────────────┘    │  bollinger/   │
                                                      │  counter/     │
                                                      │  grid)        │
                                                      └──────┬───────┘
                                                             │
         ┌───────────────────────────────────────────────────┘
         v
┌─────────────────────────────────────────────────────────────┐
│                     Filter Pipeline (v4.5)                   │
│  每条证据只消费一次 · 无重复决策 · 无语义冲突                    │
│                                                             │
│  Regime ──> Kalman ──> Hurst ──> Confidence ──> OI/Flow     │
│  (市场状态)   (方向确认)  (趋势/回归) (综合评分)    (持仓背离)   │
│                                                             │
│  ──> Funding ──> BTC Linkage ──> Portfolio ──> Direction    │
│      (费率)       (系统性风险)     (集中度/相关)   (方向开关)    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           v
┌──────────────┐    ┌──────────────┐    ┌──────────────────┐
│ Risk Monitor │<───│ Execution    │───>│ Exit Protection  │
│ (日内锁/熔断) │    │ (动态仓位/    │    │ (TP/SL/追踪/     │
│              │    │  Kelly/ADX)  │    │  僵尸仓/波动熔断) │
└──────────────┘    └──────────────┘    └──────────────────┘
```

## 2. 模块清单

### 内嵌模块 (`deepseek_quant_bot.py` 内 10 类)

| 类名 | 职责 |
|------|------|
| `ConfigManager` | .env 加载 + 50+ 策略参数 + AI 总开关 |
| `ExchangeInterface` | ccxt Bitget 封装 (OI/费率/TPSL/平仓) |
| `IndicatorCalculator` | EMA/RSI/ATR/ADX/MACD/布林 纯本地计算 |
| `DeepSeekAnalyst` | **研究层** — 信号解释/复盘/过滤诊断 + 断路器 |
| `MarketContextManager` | CoinGecko 热门 + F&G 情绪指数 |
| `TradeLogger` | JSONL 按日分割交易日志 |
| `RiskMonitor` | 日内亏损锁 + 日重置 + 手续费追踪 |
| `TradeExecutor` | ADX 动态仓位 + 部分止盈 + Kelly 系数 |
| `SessionManager` | 7 时段波动模型 (亚洲/欧洲/美股重叠/...) |
| `DeepSeekQuantBot` | **编排器** — 扫描→多TF→市场状态→评分→风控→执行 |

### 独立模块 (14 文件)

| 文件 | 核心类 | 职责 |
|------|--------|------|
| `signal_scorer.py` | `SignalScorer` | 量化评分/置信度/Kalman 确认/冷却 |
| `self_learner.py` | `SelfLearner` | 交易反思/参数自调/智慧库 (LRU 30条) |
| `genetic_evolver.py` | `GeneticEvolver` | 7参数变异→反事实回测→纯量化评分 |
| `equity_auditor.py` | `EquityAuditor` | 独立资金审计，净值唯一真相 |
| `flow_monitor.py` | `FlowMonitor` | OI 背离检测 (OI↑价↓=空头加仓) |
| `trailing_sl.py` | `TrailingStopManager` | 盈利追踪 SL 锁定利润 |
| `grid_strategy.py` | `GridManager` | 震荡市布林网格 |
| `news_integration.py` | `CryptoPanicClient` | 新闻情绪 + 情绪量化分数 |
| `portfolio_manager.py` | `PortfolioManager` | BTC 相关性 + 信号排名 |
| `safety_manager.py` | `SafetyManager` | API 熔断/总敞口/紧急平仓 |
| `health_monitor.py` | `HealthMonitor` | TPSL/信号流速/保证金全链路健康 |
| `performance_tracker.py` | `PerformanceTracker` | Sortino/Calmar/策略/方向拆分 |
| `time_utils.py` | — | 统一 Asia/Shanghai 时区 |
| `dashboard.py` | — | 实时账户面板 |

## 3. 信号管线 (6 策略)

| 策略 | 触发条件 | 适用市场 |
|------|---------|---------|
| `pullback` | RSI 超卖/超买 (干旱自适应放宽) | 趋势回调 |
| `momentum` | ADX>25 + 价格突破近期高/低 | 强趋势突破 |
| `ema_cross` | EMA9 金叉/死叉 EMA21 | 趋势跟随 |
| `bollinger` | 触碰布林上下轨反弹 | 震荡均值回归 |
| `counter_trend` | 趋势校验通过但逆势 (先于方向阻止放行) | 极端反转 |
| `grid` | 低波动震荡 (GridManager 独立部署) | 横盘 |

## 4. v4.5 过滤器架构原则

**核心原则: 每条证据只消费一次 (Evidence Consumed Once)**

| 证据 | 消费位置 | 不再重复 |
|------|---------|---------|
| ADX 趋势强度 | Regime 判定 | 不做全局 ADX 硬过滤 (已移除) |
| Kalman 方向 | `SignalScorer` 方向确认 | 不再次做方向校验 |
| Hurst 环境 | 执行前趋势/回归阻断 | Regime 不再二次判定 |
| OI 背离 | `FlowMonitor` 入场前一次 | 持仓阶段不重复计算 |
| Funding | 入场前方向一致性 | 不参与持仓评分调整 |
| BTC 联动 | 非 BTC 标的入场前阻断 | 不重复做相关性校验 |

反模式已消除: Regime 与 Hurst 双重环境过滤、Kalman 与 EMA 双重方向判断、ADX 多处散落硬过滤。

## 5. 风控体系 (10 层)

| # | 层级 | 触发条件 |
|---|------|---------|
| 1 | 止损 | 价格触及 TPSL |
| 2 | 浮亏止损 | ROI < -30% + 持仓 >1h |
| 3 | 僵尸仓退出 | 持仓 >4h + |ROI| < 2% |
| 4 | 波动熔断 | ATR > 2.5x 均值 → SL 收紧 |
| 5 | 日内亏损锁 | 日亏 > `MAX_DAILY_LOSS_PCT` → 停交易 |
| 6 | API 熔断 | 连续 5 次错误 → 暂停 |
| 7 | 方向开关 | 滚动 10 笔胜率 < 30% → 暂停该方向 |
| 8 | Kalman 方向确认 | 信号方向与 Kalman 趋势冲突 → 拒绝 |
| 9 | 币种止损冷却 | 同币种止损后 30min 禁止重开 |
| 10 | 周末凌晨保护 | 低流动性时段只平仓不开仓 |

## 6. 执行与退出

**入场**: 候选信号经 Regime→Kalman→Hurst→Confidence→OI→Funding→BTC→Portfolio→Direction 逐层消费后, `TradeExecutor.execute()` 校验余额/持仓/保证金/强平距离 → 市价开仓 → 立即设 TPSL

**持仓保护 (每周期优先执行)**:
1. Emergency Guard (日内亏损锁/紧急平仓)
2. TPSL 健康检查 (无 SL 仓立即平)
3. Trailing Stop 更新
4. Profit Lock (ROI > 40% 止盈)
5. Stale Exit (僵尸仓退出)
6. Volatility Spike 检测
7. AI 周期性持仓审查 (研究层, 非阻塞)

**退出**: TPSL 触发 > 追踪止损 > Profit Lock > Stale Exit > 波动熔断 > 紧急全平 — 退出逻辑优先级高于新开仓。

## 关键设计决策

- **AI 是研究层，不是决策者**: `DeepSeekAnalyst` 仅解释/复盘/诊断，信号开仓由 `SignalScorer` 纯量化评分决定
- **量化遗传进化**: `GeneticEvolver` 使用 Sharpe 50% + Sortino 30% + PnL 20% 综合评分，AI 仅可选意见
- **出场优先**: 每周期先保护已有持仓，再决定新开仓 — 存活第一
- **1h/4h K 线用 `iloc[-2]`**: 最后一根已闭合 K 线，禁止未来函数
- **沙箱适配**: 量比自动跳过、OI 可用但 L/S 多空比不可用
- **Bitget pos-tpsl**: 止损是持仓级 TPSL 而非独立计划单，`place-pos-tpsl` 一次调用设 SL+TP
