# DeepSeekQuantBot v4.5 策略设计

> 核心定位: 量化信号评分引擎 + Kalman/Regime 统一决策入口。AI 仅做研究解释，不做交易决策。

## 策略总览

| 策略 | Alpha | 适用市场 | 主要风险 |
|------|-------|---------|---------|
| Pullback | 趋势中回调会回归趋势方向 | 趋势市(bull/bear/strong_bull/strong_bear)，震荡市也可用 | 强单边中接飞刀；震荡转趋势时的假回调 |
| Counter Trend | 极端超买/超卖后价格短期回归 | 趋势末端、恐慌/贪婪极端区域 | 强单边中被碾压 |
| Momentum/Breakout | 强趋势中的突破会延续 | strong_bull/strong_bear，高 ADX 环境 | 假突破；低波动中频繁止损 |

> 另有 ema_cross (趋势跟随)、bollinger (震荡均值回归)、grid (低波动网格) 三个辅助策略，本文聚焦三大核心策略。

---

## Pullback (回调策略)

**代码位置**: `deepseek_quant_bot.py:1014-1020` (信号生成), `:1223-1244` (RSI 角色拆分), `:490-559` (Kalman 统一 resolve)

### Alpha 假设
趋势中的回调是暂时的，价格会回归趋势方向。顺势在回调末端入场，赚取回归段的利润。

### 适用市场
- 趋势市 (bull/bear/strong_bull/strong_bear) — 有明确趋势方向可回归
- 震荡市 (range) 也可用 — Regime 推荐策略中包含 pullback (`deepseek_quant_bot.py:730-738`)
- 干旱期 RSI 阈值自适应放宽 (`:960-962`)，避免完全无信号

### 失效场景
- **强单边趋势中逆势接飞刀**: 趋势极强时回调不回归，继续沿原方向运行。v4.5 通过 RSI 角色拆分缓解（入场触发不加分 = 不鼓励越极端越追）
- **震荡转趋势时假回调**: 震荡中出现的回调信号可能是趋势启动的早期假象

### 核心过滤器
1. **RSI 超卖/超买** — 入场触发条件 (`:1015-1020`): LONG 需 `rsi < effective_oversold`，SHORT 需 `rsi > effective_overbought`
2. **EMA 趋势方向** — 价格必须在 EMA 上方(LONG)或下方(SHORT)，确保回调方向与短期趋势一致
3. **Kalman 冲突降级** — `_resolve_kalman()` (`:490-559`): Kalman 与信号方向冲突时，策略降级为 counter_trend，提升 required_score 门槛
4. **Regime 允许方向** — `allowed_directions` (`:740-762`): 多 TF 趋势投票决定允许方向，均值回归策略豁免 (`:2022`)
5. **方向学习器降级** — 滚动胜率过低时降级 counter_trend + 半仓 (`:1173-1189`)

### 收益来源
趋势回归的均值修复 —— 价格从短期极端位置回到 EMA 趋势线附近。

### 历史问题 (v4.5 已修复)
- **v4.5 前 ADX + Kalman + Regime 三重拦截**: ADX 全局硬过滤 (`:917-920 已移除`) + Kalman 四重干预 (`:496-500 已统一`) + STRATEGY_ROUTE，导致 pullback 几乎无法触发
- **RSI 深度加分与入场条件重复**: v4.5 拆分 RSI 角色 —— 入场触发用 RSI，评分不再对 pullback 重复加分 (`:1233-1236`)。旧逻辑"RSI 越极端越加分"容易在趋势延续中接飞刀/摸顶

---

## Counter Trend (逆势策略)

**代码位置**: `deepseek_quant_bot.py:1126-1158` (趋势方向校验逆势通过), `:1400-1419` (极端 RSI 硬门槛), `:2246-2264` (半仓 + 同质化限制), `signal_scorer.py:52-61` (策略感知门槛)

### Alpha 假设
极端超买/超卖后市场情绪过度，价格会短期回归均值。在恐慌底部做多、贪婪顶部做空。

### 适用市场
- 趋势末端 (bear + Markov 偏空 → 推荐 counter_trend, `:737-738`)
- 恐慌/贪婪极端区域 (RSI < ct_rsi_long_max 或 > ct_rsi_short_min)
- 作为系统死锁逃生通道: pullback 被 Kalman/方向学习器降级时，以 counter_trend 身份半仓执行

### 失效场景
- **强单边趋势中逆势被碾压**: 趋势极强时极端 RSI 可以持续很久，逆势入场被趋势延续打损
- **熊市反弹陷阱**: 熊市中 RSI 深度超卖反弹后继续下跌 (v4.5 通过提高门槛 + 同质化限制缓解)

### 核心过滤器
1. **极端 RSI 硬门槛** (`:1400-1419`): 主动 counter_trend LONG 要求 `rsi <= ct_rsi_long_max` (默认 35)，SHORT 要求 `rsi >= ct_rsi_short_min` (默认 65)。被动降级 (原 pullback) 豁免此门槛
2. **更高 required_score** (`signal_scorer.py:59-60`): 常态 58，Kalman 冲突时 65。pullback/动量只需 50
3. **半仓** (`:2261-2264`): `pos_mult *= 0.5`，所有 counter_trend 自动半仓
4. **同质化限制** (`:2246-2260`): `MAX_COUNTER_TREND_POSITIONS` (默认 2)，防止同时持有过多逆势仓位
5. **Kalman 降级入口**: `_resolve_kalman()` 在 Kalman 与信号方向冲突时，将 pullback 降级为 counter_trend (`:525-533`)

### 收益来源
极端情绪的均值回归 —— 恐慌抛售后的反弹，或贪婪追高后的回调。

### 历史问题 (v4.5 已修复)
- **被动降级 vs 主动 counter_trend RSI 门槛混淆**: v4.5 通过 `_original_strategy` 追踪 (`:1010-1012`) + `_is_degraded` 标记 (`:1402`) 区分，被动降级跳过极端 RSI 硬门槛
- **Kalman 四重干预**: 旧逻辑 Kalman 同时做 4 件事 (`:496-500`)，v4.5 统一为 `_resolve_kalman()` 单一入口，一次计算一次消费

---

## Momentum/Breakout (动量突破)

**代码位置**: `deepseek_quant_bot.py:1022-1039` (信号生成), `config_manager.py:85` (MOMENTUM_ADX 配置), `:1223-1230` (量价确认)

### Alpha 假设
强趋势中的突破会延续 —— 价格突破近期高/低点后，趋势加速段提供惯性利润。

### 适用市场
- strong_bull/strong_bear — 高 ADX 环境 (ADX > momentum_adx_threshold，默认 25)
- Regime 推荐 momentum 的市场状态

### 失效场景
- **假突破**: 价格短暂突破后快速反转，止损被触发
- **低波动震荡中频繁止损**: 震荡市中 ADX 不足时强行突破，缺乏趋势惯性支撑

### 核心过滤器
1. **ADX > momentum_adx_threshold** (`:1025, :1033`): v4.5 移除 ADX 全局硬过滤后，momentum 的专属 ADX 阈值是唯一入口 (`config_manager.py:85`，默认 25)
2. **RSI 动量区间** (`:1025`): LONG 需 `rsi > momentum_rsi_min` 且 `< momentum_rsi_max`，确保不在超买/超卖极端区
3. **价格突破近期高/低点** (`:1028-1029, :1037-1038`): 必须触及或突破近期极值 (0.2% 容差)
4. **Regime 推荐**: 仅 Regime 推荐包含 momentum 时才放行

### 收益来源
趋势加速段的惯性 —— 突破后资金追入推动价格继续沿趋势方向运行。

### 历史问题 (v4.5 设计要点)
- **ADX 全局硬过滤被移除** (`:917-920`): v4.5 前 ADX 同时做信号层硬过滤 + Regime 分类 + Confidence 加分，三重冗余扼杀 momentum 和 ema_cross。移除全局硬过滤后，momentum 通过专属 `momentum_adx_threshold` 独立控制，不与 pullback 的 ADX 需求耦合

---

## v4.5 设计原则

### 1. 每个信号证据只消费一次
v4.5 前同一趋势证据被多次消费: 信号层 ADX 硬过滤 → Regime 趋势分类 → Confidence 加分，三重冗余导致 ADX 成为伪瓶颈。修复: ADX 仅通过 Regime.trend_strength 传递到评分系统 (`:772-773`)。Kalman 同样: 旧逻辑同时改策略/扣 confidence/提门槛/改方向 (`:496-500`)，v4.5 统一为 `_resolve_kalman()` 单一决策入口。

### 2. 入场触发 =/= 评分加分 (RSI 角色拆分)
Pullback 由 RSI 触发入场 (`:1015-1020`)，评分阶段不再对 pullback 做 RSI 深度加分 (`:1233-1236`)。旧逻辑"RSI 越极端 → 入场 + 加分"是双重消费: RSI 越极端加分越多，鼓励在最危险的位置加大仓位。拆分后 RSI 只决定"进不进"，确认信号质量由独立维度 (MACD/量价/K线结构/ATR) 评估。

### 3. 单一决策入口
两个统一入口取代碎片化检查:
- **Kalman**: `_resolve_kalman()` (`:490-559`) — 一次计算返回 action/penalty/new_strategy，下游只读一次
- **Regime**: `allowed_directions` + `recommended` (`:740-762, :2000-2022`) — 替代旧 STRATEGY_ROUTE + TF_MISMATCH + _check_trend_alignment 三重过滤

### 4. 策略感知门槛 (required_score 按策略分化)
`SignalScorer.get_required_score()` (`signal_scorer.py:52-61`): counter_trend 需要 58/65，其他策略只需 50。执行层只检查一次 (`:1391-1398`)，不再单独维护 min_conf 变量。门槛值由策略风险和 Kalman 冲突状态决定，而非全局统一。
