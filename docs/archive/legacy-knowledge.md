# 历史知识库 (v4.5 文档重组后提取)

> 以下内容来自 `project_structure.md` 和 `project_understanding.md`，
> 未被 `architecture/overview.md` 和 `design/strategy-design.md` 覆盖的独特信息。
> 2026-06-12 提取，重叠度 ~75%。

---

## 1. Dead Components (来自 project_structure.md §7)

### 未使用或弱连接模块

| 组件 | 状态 | 说明 |
|------|------|------|
| `backtest.py` 内置信号逻辑 | 与主系统脱节 | 只覆盖简化版 Pullback/Momentum，缺少 EMA Cross/Bollinger/Counter Trend/Kalman/Hurst/Regime/OI/Portfolio 等实时逻辑 |
| `backtest.py` 内置 `IndicatorCalculator` | 重复实现 | 项目已有独立 `indicator_calculator.py`，回测文件重复实现指标计算，存在逻辑漂移风险 |
| `freqtrade/strategies/*` 与 `freqtrade/user_data/strategies/*` | 重复策略副本 | 两组目录存在同名策略文件，`user_data` 更像部署副本，容易版本漂移 |
| `BitgetHybridStrategy` | 旧版/高风险实验策略 | 文档中已有过交易和废弃倾向说明；当前更保守的 Freqtrade 路径是 `BitgetSimpleStrategy` |
| `TradeExecutor.pop_rr_shadow()` | 未观察到主流程调用 | RR shadow 有记录接口，但主流程中没有明显消费路径 |

### 永远无法触发或基本不会生效的逻辑

| 逻辑 | 状态 | 说明 |
|------|------|------|
| `TradeExecutor.REGIME_MULTIPLIERS` | 多数不会命中 | key 使用 `strong_trend`/`weak_trend`/`ranging`，而实际 Regime 输出是 `strong_bull`/`bull`/`range`/`bear`/`strong_bear`/`panic`，名称不匹配时退回默认 1.0 |
| `_phase_execute` 中 `len(pos_list) - longs` | 无效果表达式 | 该表达式没有赋值或使用结果，对运行行为没有影响 |
| 全局 ADX hard filter | v4.5 已移除 | 相关 skip 统计保留为兼容，ADX 仅通过 Regime.trend_strength 影响系统 |
| Optional modules 默认关闭路径 | 配置性休眠 | Grid/Portfolio/News sentiment 等默认可关闭，不是死代码但在默认配置下不参与完整交易链路 |

---

## 2. Known Failure Modes (来自 project_understanding.md)

1. **Lookahead / 未完成K线依赖**: 使用形成中的K线导致信号不可靠
2. **策略冲突**: RSI均值回归、ADX趋势强度、Kalman趋势状态、pullback/breakout逻辑可能产生矛盾信号
3. **Pullback 身份冲突**: pullback 在一层被当作趋势延续，在另一层被当作均值回归
4. **Counter trend 泄漏**: 普通 pullback 被降级为 counter_trend 但风险假设未完全切换
5. **缺失止损路径**: 任何在止损确认前开仓的路径都会产生临时裸仓
6. **仓位放大**: 杠杆+最小合约+Kelly乘数+市场状态乘数+组合乘数可叠加为超大暴露
7. **参数耦合**: RSI/ADX/EMA/Kalman 同时控制同一行为，边缘脆弱 → v4.5 已大幅缓解
8. **紧急退出不确定性**: 紧急平仓必须验证仓位实际已关闭，而非仅提交了平仓单
9. **回测可信度风险**: 参数可能仅适配狭窄历史窗口，需 Walk-forward + Regime 切片验证
10. **AI 角色混淆**: AI 文档声明为研究层，任何让 AI 成为阻塞或确认决策者的路径都改变预期架构
11. **Bitget TPSL 可见性缺口**: 持仓级 pos-tpsl 可能不在 open stop-order 查询中出现
12. **方向失衡**: Hedge 模式支持双向，但系统仍需防止单向暴露过度集中
13. **过度过滤死锁**: ADX/Kalman/策略路由/方向阻止/多TF 过滤器可组合压制有效信号 → v4.5 已修复
14. **回测过度自信**: Freqtrade 记录明确警告——短验证窗口/僵尸仓出场/单一状态测试不应视为实盘证据

---

## 3. 架构风险 (来自 project_structure.md §8)

- **最复杂模块**: `deepseek_quant_bot.py` — 同时承担策略层、过滤层和编排层 (~3500行)
- **最关键模块**: `TradeExecutor` + `ExchangeInterface` — 决定仓位/杠杆/TPSL/保证金安全
- **最大架构风险 (v4.5前)**: 决策层过多且部分语义不一致，同一候选信号被 RSI/ADX/EMA/Kalman/Hurst/Regime/Confidence/OI/Funding/Portfolio/Direction Learner 多次修改或阻断 → v4.5 通过"每条证据只消费一次"原则系统性修复
