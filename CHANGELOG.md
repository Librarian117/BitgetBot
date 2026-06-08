# Changelog

## 2026-06-08

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
