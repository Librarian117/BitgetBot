# Changelog

## 2026-06-11

### v4.5: Pullback Confirmation Attribution — 归因驱动的信号质量分层

> **核心原则**: 先归因，后修改。不新增防线。不预设根因。观测期至 06-25。

**背景 — 72h 归因分析 (51 笔平仓)**:
- 已排除: counter_trend/LONG/Regime/RSI/ADX 作为亏损主因
- **核心发现**: pullback SHORT 内部混入两种完全不同质量的信号
  - 类型 A (赚钱): 3连阴 + 强阴线 + MACD空头 (多重确认)，持仓 78min，+698
  - 类型 B (亏钱): MACD空头单独 或 +布林上轨 (弱确认)，持仓 29min，-1,040
- RSI/ADX/Confidence 几乎无法区分好坏信号 (差异 <5%)
- 信号结构 (确认层数) 有显著区分力: 强阴线 +40%, 3连阴 +30%

**新增观测能力 (不改策略行为)**:

#### 1. Regime Attribution 观测日志 (Phase 1)
- `EXIT_SNAPSHOT.regime_at_entry` 从 `_open_trade_factors.market_regime` 填充
- 新增 `REGIME_CHANGE` 事件 (from/to/duration_hours/cycle)
- `FILTER_STATS` 增加 `current_regime` 字段
- 纯观测，不影响任何交易决策

#### 2. counter_trend 防守强化 (基于熊市反弹陷阱分析)
- 最低置信度: 45 → **58** (常态), 死锁降级时 **65**
- **RSI 硬性门槛**: LONG 要求 RSI ≤ `CT_RSI_LONG_MAX`, SHORT 要求 RSI ≥ `CT_RSI_SHORT_MIN`
- **同质化限制**: `MAX_COUNTER_TREND_POSITIONS` 限制同时持仓数 (CT_CLUSTER_LIMIT)
- 目的: 拦截熊市中"假超卖→反弹失败"的陷阱信号

#### 3. PnL 跨重启不再累计
- 从 `_restore_state()` 中移除 `bot_closed_pnl_total` 恢复
- 启动权益由交易所实际余额决定，不再跨重启累计
- 消除"bot 预期 vs 实际"审计偏差

#### 4. TPSL 沙箱信任缓存修复
- 平仓时清除 `_tpsl_cache`，避免仓位已关仍认为有保护
- 消除误报"裸仓" (盘中检测到缺 SL 但实际已平仓)

#### 5. 日志优化
- 消除平仓日志重复 (`log_position_close` + `log_exit_snapshot` → 仅后者)
- `ENTRY_SNAPSHOT` 增加 try/except 异常保护
- 日亏损锁日志降频 (每小时最多一次，减少噪声)

**新增 .env 配置**:
```ini
MAX_COUNTER_TREND_POSITIONS=2   # counter_trend 同时最大持仓数
CT_RSI_LONG_MAX=35              # counter_trend LONG 最大 RSI (超卖确认)
CT_RSI_SHORT_MIN=65             # counter_trend SHORT 最小 RSI (超买确认)
```

**Phase 2 计划 (观测期 06-11 ~ 06-25)**:
- 收集 100-200 笔样本，验证"确认层数 vs PF"单调性
- 如果单调性成立 → 为 pullback 信号增加确认层数权重
- 如果单调性不成立 → 寻找下一个候选因子

### v4.4: 交易质量守卫 + 归因数据闭环

**Alpha Attribution — 7天实盘数据诊断**:
- SHORT 87笔 胜率67% +$3,435 | LONG 42笔 胜率15% -$2,991
- DOGE LONG 7笔全败 -$2,269 | LINK SHORT 12笔全胜 +$947
- 核心问题不是参数，而是 LONG 方向在熊市中系统性失效

**新增 3 个执行层守卫** (不改 Alpha 引擎):

#### 1. 禁止同币种对锁 (`ALLOW_HEDGE=false`)
- `_phase_execute()` 开仓前检查同一币种是否已有反向持仓
- 如有 → `FILTER_REJECT: OPPOSITE_HELD` + 跳过
- 配置化：设为 `true` 恢复 Hedge 对锁（未来组合策略用）

#### 2. R:R 最小盈利门槛 — 影子模式 (`MIN_RR_RATIO=1.5`)
- 预期TP1盈利 / 预估手续费 < 1.5x → 只记录不拦截
- 新日志事件: `WOULD_REJECT_RR` (开仓时) + `RR_OUTCOME` (平仓时)
- `ENTRY_SNAPSHOT.rr_ratio` 持久化 (跨重启安全)
- 48h后按 RR 区间统计实际 PnL，数据驱动校准阈值

#### 3. 沙箱风控统一 (`SANDBOX_SAFETY=true`)
- `trade_executor.py` 安全校验从 `_skip_safety` 扩展为 `sandbox_safety`
- 沙箱=实盘同一套: 最小仓位 + 费后利润 + 限价后备 + 成交确认
- 消除 sandbox≠live 导致的回测失真

**新增日志事件类型**:
| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `WOULD_REJECT_RR` | 开仓 RR 不足 | 48h影子统计 → 校准阈值 |
| `RR_OUTCOME` | 平仓后回填 | 关联 actual_pnl → 按 RR 区间归因 |
| `ENTRY_SNAPSHOT.rr_ratio` | 开仓快照 | 跨重启持久化 |

**配置新增** (`.env`):
```ini
ALLOW_HEDGE=false       # 禁止同一币种多空对锁
MIN_RR_RATIO=1.5        # 最小风险回报比 (影子模式)
SANDBOX_SAFETY=true     # 沙箱启用安全校验
```

**下一步 (48h 数据收集后)**:
- WOULD_REJECT_RR 按区间统计 → 确定真正有效的 RR 阈值
- EXIT_SNAPSHOT × strategy × direction → 定位 LONG 亏损的具体策略组合
- 如果 counter_trend × LONG 主导亏损 → 关闭该模块的 LONG 方向

### 日志系统 v4.3 — 新事件类型

**新增 3 种日志事件** (写入 `logs/trades_YYYY-MM-DD.jsonl`):

#### ENTRY_SNAPSHOT — 开仓完整快照
```json
{
  "event": "ENTRY_SNAPSHOT",
  "symbol": "ETH", "direction": "SHORT", "strategy": "pullback",
  "score": 72, "entry_price": 1978.0, "sl": 2010.0, "tp": 1930.0,
  "ema": 1975.0, "rsi": 68.0, "adx": 42.0,
  "ema_trend": "BEAR",       // EMA50 vs EMA200: BULL/BEAR/NEUTRAL
  "kalman_dir": "up",        // Kalman 方向: up/down/flat
  "kalman_score": 0.75,      // Kalman 信号强度
  "session": "US_OVERLAP",   // 7 时段标签
  "market_regime": "TRENDING", // 市场状态
  "bonuses": ["MACD底背离", "布林下轨"],  // 信号加成
  "amount_contracts": 5
}
```

#### EXIT_SNAPSHOT — 平仓完整快照
```json
{
  "event": "EXIT_SNAPSHOT",
  "symbol": "ETH", "direction": "SHORT", "strategy": "pullback",
  "entry_price": 1978.0, "exit_price": 2051.0,
  "pnl": -99.64, "pnl_pct": -30.0,
  "exit_reason": "STOP_LOSS",    // STOP_LOSS/TAKE_PROFIT/TIME_EXIT/BREAKEVEN_STOP/MARKET_CLOSE
  "hold_minutes": 265.0,         // 持仓时长(分钟)
  "score_at_entry": 72,          // 入场时评分
  "ema_trend_at_entry": "BEAR",  // 入场时 EMA 趋势
  "kalman_at_entry": "up",       // 入场时 Kalman 方向
  "regime_at_entry": "RANGING"   // 入场时市场状态
}
```

#### FILTER_STATS — 每周期过滤器统计
```json
{
  "event": "FILTER_STATS",
  "cycle": 5,
  "stats": {
    "EMA_KALMAN_CONFLICT": 17,
    "ADX_TOO_LOW": 4,
    "DIRECTION_BLOCK": 2
  }
}
```

#### 出场原因分类 (替代旧的 `DETECTED`)
| exit_reason | 判定条件 |
|-------------|---------|
| `STOP_LOSS` | PnL ≤ -25% |
| `TAKE_PROFIT` | PnL ≥ 30% |
| `TIME_EXIT` | 持仓 > 4h 且 |PnL| < 2% |
| `BREAKEVEN_STOP` | |PnL| < 1% 且持仓 < 10min |
| `MARKET_CLOSE` | Bitget pos-tpsl 触发 (其他情况) |

#### POSITION_CLOSE 增强
- 新增 `hold_minutes` 字段 (替代原来永远为 0 的 `holding_hours`)
- `close_reason` 改为上述分类值

### EMA-Kalman 冲突降分 v4.3

- **EMA_KALMAN_CONFLICT**: 从硬拒绝改为置信度扣分(max 20) + counter_trend 降级
- **KALMAN_CONFLICT (>0.8)**: 从硬拒绝改为重度扣分(max 25) + counter_trend 降级
- **趋势偏向放宽**: Kalman 冲突时 `block_long`/`block_short` 不再拦截
- **新增 KALMAN_VS_EMA 统计事件**: 记录 EMA方向/Kalman方向/当前价格, 用于离线分析谁更准

### Bug 修复
- **Dashboard SL/TP 双重检测**: 新增 `fetch_open_orders(stop=True)` 计划单兜底, 对齐 `_has_position_tpsl()` 逻辑。沙箱 pos-tpsl 在 `fetch_positions().info` 中不返回 stopLoss, 计划单可补充检测。裸仓从 4 降到 2

## 2026-06-08

### 重大变更: Freqtrade 迁移试点
- **调研**: Bitget U本位合约 Freqtrade 官方支持 (Isolated only, One-way Mode 强制, stoploss_on_exchange 可用, Hedge 模式不支持)
- **环境**: `E:\freqtrade\` — Freqtrade 2026.5.1 + Python 3.14 + 10 币种 OHLCV 数据 (5m + 1h, 2024.06-2026.06)
- **中文路径 Bug**: Python 3.14 multiprocessing + 中文用户名路径导致 Hyperopt 崩溃 → `run.ps1` 包装脚本绕过 (TEMP=E:\freqtrade\temp + PYTHONUTF8=1)
- **项目已搁置**: 原 bot 服务器继续跑, Freqtrade 本机保留环境暂不切换

### Freqtrade 回测迭代记录
| 版本 | K线 | 策略 | Hyperopt | 盈亏 | 胜率 | 笔数 | 结论 |
|------|-----|------|---------|------|------|------|------|
| v4 | 1h | bollinger+pullback | 200 epochs | **+2.09%** | 48.5% | 478 | 🏆 当前最优 |
| v5 | 1h | +独立pullback参数+新僵尸仓 | 100 epochs | -7.33% | 42.5% | 398 | 🔴 多变量同时改导致崩盘 |
| v4+RSI43 | 1h | 仅改RSI 33→43 | 无 | +2.42% | 48.7% | 487 | pullback活了但不赚钱 |

### 单变量实验结论 (20,080根1h K线)
- **pullback v4 = 0 信号** (RSI<33/67 + ADX>27 完全压死)
- **瓶颈是 RSI, 不是 ADX**: ADX 27→20 单独改 = 0 信号; RSI 33→43 单独改 = 26 信号
- **pullback 即使活了也不赚钱**: 18笔全僵尸仓出场, -0.16%, 38.9%胜率
- **最有用的方法是 Hyperopt**: -0.06% → +2.09% 的改进来自参数优化, 不是策略逻辑改动
- **教训: 单变量实验, 先统计再改码**

### Bug 修复
- **STRATEGY_ROUTE 误杀信号**: range 市场推荐策略从 `["pullback", "bollinger"]` 扩展为 `["pullback", "bollinger", "ema_cross"]`。死锁修复后 STRATEGY_ROUTE 成为新主导瓶颈——ema_cross/momentum 信号被全部拦截（6月7日晚至8日上午0笔交易），而 pullback 和 bollinger 在当前行情下无法触发（RSI未达极端值）。range≠完全横盘，当前温和偏向市场中 ema_cross 能有效捕捉小趋势。
- **Markov 微调硬编码修正**: bear→range 和 bull→range 降级时推荐列表同步更新为 `["pullback", "bollinger", "ema_cross"]`

### 审计
- 6月7日 23:40 部署至 6月8日 10:30 — **0 笔开仓**
- 候选信号全部被 STRATEGY_ROUTE 拦截（10+ 次 ema_cross/momentum rejection）
- EMA_KALMAN_CONFLICT 持续占主导（BTC/XRP/ADA 每周期触发）
- 市场长期判定为 "range" + "no_signal"（多 TF 无共识 + ADX 弱势）

## 2026-06-07

### Bug 修复
- **DIRECTION_BLOCK 三重死锁**: counter_trend 作为死锁逃生信号现在豁免 `is_direction_viable()` 方向胜率检查。之前 counter_trend 已通过 EMA-Kalman 冲突降级和方向偏见豁免，但仍被学习器方向暂停拦截 (SHORT 胜率 20%<30%)，导致完全死锁
- **资金审计报警噪音**: `EquityAuditor._alarm()` 添加重复报警抑制 — 偏差变化 <20% 且 <100 USDT 时不重复写报警日志，避免每周期 (5 分钟) 重复报警 240+ 次

## 2026-06-06

### 架构重构
- **Phase 1**: 5 个服务类提取为独立模块 (indicator_calculator, market_context, deepseek_analyst, trade_logger, risk_monitor), 主文件 4094→2989 行
- **Phase 2A**: `run_once()` 1389 行拆分为 6 阶段函数 (39 行编排器), 零行为变更

### Bug 修复
- **Kalman 短窗口误覆盖 EMA200**: Kalman(50bar≈4h) 冲突时不再覆盖 EMA200 长期趋势, 避免小时级反弹误翻方向
- **遗传进化器产出 MIN>MAX 导致启动崩溃**: 三层防护 — mutate_params 交叉校验 + config_validator 自动修正 + runtime_params 持久化保护
- **干旱计数器重启归零**: `_drought_cycles` 持久化到 `positions_state.json`, 重启后恢复
- **TPSL holdSide 方向映射错误**: 统一所有调用方传持仓方向 ("long"/"short"), 修复 TP 丢失问题
- **contracts 变量使用前未定义**: 平仓检测中 `contracts` 在定义前被引用, 重构暴露的预存 Bug

### 新功能
- **FILTER_REJECT 统一过滤器日志**: 9 种过滤原因结构化记录到 JSONL (EMA_KALMAN_CONFLICT, KALMAN_CONFLICT, ADX_TOO_LOW, VOL_TOO_LOW, DIRECTION_BLOCK, COOLDOWN, HURST, STRATEGY_ROUTE, TF_MISMATCH)
- **EMA-Kalman 死锁逃生**: 冲突时降级为 counter_trend, 置信度≥65 放行, 半仓, 解决系统死锁 (81% 过滤率→信号恢复)

### 统计
- EMA_KALMAN_CONFLICT 占所有过滤的 81%, 是当前最大信号瓶颈
