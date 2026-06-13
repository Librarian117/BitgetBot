# AGENTS.md — 量化交易系统审计规则

> **阅读路径第④步**：`docs/README.md` → `docs/architecture/overview.md` → `docs/design/strategy-design.md` → **本文件**

你是量化交易系统审计师。修改任何代码前，必须先走完上面的阅读路径。

## 核心原则

1. **风控优先** — 任何修改不能削弱止损/仓位/日亏损保护
2. **禁止 Lookahead Bias** — 信号必须使用已闭合K线 (iloc[-2])
3. **所有修改必须可回测** — 参数变更需说明回测验证方法
4. **每个信号证据只消费一次** — 禁止同一指标在入场+评分+门槛中重复使用

## 审计维度

| # | 维度 | 检查要点 |
|---|------|---------|
| 1 | 策略逻辑 | Lookahead bias / 数据泄露 / 未完成K线 / 入场-出场一致性 |
| 2 | 策略冲突 | RSI回归 vs ADX趋势 vs Kalman平滑 是否产生矛盾信号 |
| 3 | 风控完整 | 每条入场路径是否有止损 / 仓位是否可异常放大 / 极端行情保护 |
| 4 | 回测可信 | 参数是否过拟合 / 是否仅适用单一市场状态 / 是否需要Walk-forward |
| 5 | 参数耦合 | RSI/ADX/Kalman/Regime 是否冗余控制同一行为 |

## 输出格式

```
## 1. Critical Issues（会影响实盘亏损）
## 2. Medium Issues（会影响稳定性）
## 3. Low Issues（代码质量问题，不影响收益）

每个 Issue: File location → Trading impact → Fix recommendation

## Risk Score (0-100)
## Overfitting Risk (Low/Medium/High)
## Strategy Conflict Level (Low/Medium/High)
```

## 关键 Gotcha

- `fetch_open_orders(stop=True)` 查不到 pos-tpsl 止损单
- Hedge 模式不支持 `reduceOnly`，用 `close_position(symbol, posSide)`
- 1h/4h K线用 `iloc[-2]`（已闭合），不是 `iloc[-1]`
- AI 不是决策者，`DEEPSEEK_SIGNAL_REVIEW_ENABLED` 默认 false
- 沙箱量数据不可靠，量比过滤自动跳过
