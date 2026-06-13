# SignalScorer v4.5 评分模型

## 8 维度加权评分

v4.5 移除了量比(quant)维度（死权重，Kalman 由主流程统一处理），权重重新归一化至 1.00。

| 维度 | 权重 | 说明 |
|---|---|---|
| technical | 0.27 | EMA/RSI/ADX/MACD/布林 — 本地指标综合 |
| regime | 0.12 | 市场状态匹配（扫描阶段固定返回 50） |
| sentiment | 0.12 | Fear & Greed + CryptoPanic 情绪量化 |
| oi_flow | 0.11 | 持仓量背离检测（OI↑价↓=空头加仓） |
| markov | 0.11 | 马尔可夫状态转移概率得分 |
| strategy | 0.10 | 策略类型基础分（趋势策略 > 震荡策略） |
| direction | 0.10 | 方向历史胜率加权 |
| portfolio | 0.07 | BTC 相关性排名（仅排序，不再修改置信度） |
| **合计** | **1.00** | |

## 决策分层

| 层级 | 条件 | 动作 |
|---|---|---|
| AUTO | 置信度 > 70 | 直接开仓，跳过 AI 审核 |
| AI | 置信度 ≥ required_score | 量化通过，AI 可选解释 |
| REJECT | 置信度 < required_score | 自动拒绝，不触发 AI |

## required_score 按策略区分

| 策略 | required_score | 说明 |
|---|---|---|
| pullback / momentum / ema_cross / bollinger | 50 | 标准门槛 |
| counter_trend | 58 | 逆势信号要求更严格 |
| counter_trend + kalman 冲突 | 65 | Kalman 确认冲突时门槛进一步提高 |

## v4.5 关键修复

1. **Quant 维度移除** — 死权重占位符，Kalman 由 `_resolve_kalman()` 统一处理
2. **required_score 统一** — 之前 `min_conf` 分散在 3 处（SignalScorer / 主循环 / TradeExecutor），v4.5 统一到 `get_required_score()`
3. **Regime 维度扫描阶段返回 50** — `regime_info=None` 时不参与决策，仅执行阶段做路由
4. **Pullback RSI 深度奖励移除** — 入场触发条件不等于评分加分，避免 RSI 过度加权
5. **Portfolio 排名不再修改置信度** — 仅做排序参考，不污染评分体系

参考实现: `signal_scorer.py:33-175`
