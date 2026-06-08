# Changelog

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
