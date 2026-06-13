# EQUITY_AUDIT.md — Account Equity 计算链专项审计

**审计日期**: 2026-06-13  
**审计范围**: API → Equity → Balance → Margin → Available → Unrealized 全链路  
**核心问题**: 9000→7769 的 -22.23% 是否正确？各模块是否混用字段？

---

## 一、Bitget 权威字段定义

| 字段 | Bitget API | ccxt | 含义 |
|------|-----------|------|------|
| `accountEquity` | `/v2/mix/account/account-list` | `fetch_balance()["USDT"]["total"]` | **总权益** = 钱包余额 + 未实现盈亏 |
| `available` | 同上 | `fetch_balance()["USDT"]["free"]` | **可用余额**（能转出/开仓的钱） |
| `locked` | 同上 | `total - free` | **冻结保证金** |
| `unrealizedPL` | `/v2/mix/position/all-position` | `fetch_positions()[].unrealizedPnl` | **未实现盈亏** |
| `marginSize` | 同上 | `fetch_positions()[].initialMargin` | **持仓保证金** |

**关键公式**:
```
accountEquity = available + locked + unrealizedPL
```

---

## 二、数据流：字段追踪

```
Bitget API
├── accountEquity ─────────────────────────────────────────────┐
│   (= available + locked + unrealizedPL)                      │
│                                                              │
├── available ────┐                                            │
│                 │                                            │
└── unrealizedPL ─┤                                            │
                  │                                            │
                  ▼                                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ccxt fetch_balance()                                                │
│   free = available                                                  │
│   total = accountEquity  ←─────────────────────────────────────────┤
│   used = total - free  (= locked)                                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
         ┌─────────────────┼──────────────────┐
         ▼                 ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│ dashboard.py │  │ exchange_    │  │ risk_monitor.py  │
│              │  │ interface.py │  │                  │
│ "权益"=total │  │ equity=total │  │ current_equity   │
│ "余额"=total │  │ total_bal    │  │ = acct["equity"] │
│  ← SAME!     │  │   =total     │  │ = accountEquity  │
│              │  │ ← SAME!     │  │                  │
│ "可用"=free  │  │ free=free    │  │ day_start_equity │
│              │  │              │  │ = acct["equity"] │
│ "累计-22.24%"│  │ equity=total │  │ = accountEquity  │
│ =equity/     │  │              │  │                  │
│  initial-1   │  │              │  │ daily_pnl_pct    │
│              │  │              │  │ =(eq_now -       │
│              │  │              │  │  day_start)/     │
│              │  │              │  │  day_start       │
└──────────────┘  └──────────────┘  └──────────────────┘
```

### 核心发现：整个系统中"权益"和"余额"是同一个值

```
accountEquity = fetch_balance()["USDT"]["total"]
equity        = total_balance = accountEquity   (exchange_interface.py:194)
"权益"        = equity = accountEquity          (dashboard.py:170)
"余额"        = total = accountEquity           (dashboard.py:171) ← SAME!
cycle_equity  = riskmon.current_equity          (trade_logger.py:114)
              = acct["equity"] = accountEquity
equity(日志)   = acct["equity"] = accountEquity  (trade_logger.py:115)
              ← cycle_equity == equity 永远相同!
```

**系统中的"余额"实际是"权益"**。真正的"余额"（Bitget `available`）在代码中叫 `free`。

---

## 三、逐消费方审计

### 3.1 Dashboard

| 显示 | 代码来源 | 实际含义 | 正确性 |
|------|----------|----------|--------|
| `权益: 7776.48` | `bal["total"]["USDT"]` = accountEquity | ✅ 确实是权益 | ✅ |
| `余额: 7776.48` | **同上** = accountEquity | 🔴 **不是余额！是权益！** | 🔴 标签错误 |
| `可用: 7384.31` | `bal["free"]["USDT"]` = available | ✅ 确实是可用余额 | ✅ |
| `累计: -22.24%` | `(equity/10000 - 1) × 100` | ✅ 累计收益率 | ✅ |
| `浮盈: +5.27` | `Σ per-position unrealizedPnl` | ✅ 未实现盈亏 | ✅ |
| `保证金: 78 (1.0%)` | `Σ initialMargin` | ✅ 已用保证金 | ✅ |
| `收益率: +19.3%` | `upl / mgn × 100` | 🟡 保证金ROI (不是权益ROI) | 🟡 需标注 |

**正确公式**:
```
权益  = accountEquity (= available + locked + unrealizedPL)
余额  = available       ← 这才是真正的"余额"！
可用  = available
保证金 = Σ marginSize
浮盈  = Σ unrealizedPL
累计% = (accountEquity / INITIAL_EQUITY - 1) × 100
```

**当前公式 vs 正确公式**:
```
当前: "余额" = accountEquity   ← 错! 应该 = available
正确: "余额" = available      ← 可用资金
```

**差异影响**: Dashboard 显示的"余额"虚高（包含了未实现盈亏和保证金）。但对交易无实际影响，因为开仓用的是 `available`。

---

### 3.2 risk_monitor.py (日内风控)

| 计算 | 公式 | 基准 | 正确性 |
|------|------|------|--------|
| `daily_pnl_pct` | `(eq_now - day_start) / day_start` | 日初权益 | ✅ |
| `all_time_pnl` | `eq_now - initial_equity` | 初始权益 | ✅ |
| `all_time_pnl_pct` | `(all_time_pnl / initial_equity) × 100` | 初始权益 | ✅ |
| `hard_loss_pct` | daily_pnl_pct < -3% | 日初权益 | ✅ |

**正确公式** (与 Bitget 一致):
```
日收益率  = (当日权益 - 日初权益) / 日初权益
累计收益率 = (当前权益 - 初始权益) / 初始权益 × 100
```

risk_monitor **公式完全正确**。所有计算均基于同一个基准（accountEquity），没有混用。

---

### 3.3 trade_logger.py (CYCLE 日志)

| JSON 字段 | 值来源 | 实际含义 | 正确性 |
|-----------|--------|----------|--------|
| `cycle_equity` | riskmon.current_equity | accountEquity | ✅ |
| `equity` | acct["equity"] | accountEquity | ✅ |
| `unrealized_pnl` | acct["unrealized_pnl"] | Σ per-position unrealizedPL | ✅ |
| `daily_pnl_pct` | riskmon.daily_pnl_pct | 日内收益率 | ✅ |

**两个字段完全相同** — `cycle_equity` 和 `equity` 都是 accountEquity。冗余但正确。

**已修复**: v4.5→Phase2 将 `"balance"` 重命名为 `"cycle_equity"`，消除了字段名误导。

---

### 3.4 performance_tracker.py (策略绩效)

**当前公式**:
```python
total_pnl = sum(t["pnl"] for t in trades)     # line 85
win_rate  = wins / total                       # line 82
max_drawdown = peak_to_trough(cumulative_pnl)  # line 201-215
```

**正确性**: 🔴 **完全不正确** — 见下方发现 5 和发现 6。

---

## 四、关键发现

### 🔴 Critical 发现 1: `_bot_closed_pnl_total` 仅追踪 1/7 的平仓路径

**问题**: EquityAuditor 的对账基准 `_bot_closed_pnl_total` 只在 `_detect_closed_positions` 中累加。

**7 个平仓路径中只有 1 个累加 `_bot_closed_pnl_total`**:

| # | 平仓路径 | 位置 | 累加 `_bot_closed_pnl_total`? | 调用 `perf.record_trade`? |
|---|----------|------|:---:|:---:|
| 1 | `_detect_closed_positions` (周期对比) | line ~1670 | ✅ | ✅ |
| 2 | `_review_open_positions` (规则引擎) | line ~860 | ❌ | ❌ |
| 3 | AUTO_SL (浮亏止损) | line ~2932 | ❌ | ❌ |
| 4 | 僵尸仓退出 (>4h) | line ~2976 | ❌ | ❌ |
| 5 | 日亏损清仓 (`DAILY_LOSS_CLOSE_ALL`) | line ~2638 | ❌ | ❌ |
| 6 | 紧急停止 (`emergency_close_all`) | safety_manager | ❌ | ❌ |
| 7 | Rotation 轮换平仓 | line ~2449 | ❌ | ❌ |

**影响**:
```
EquityAuditor:
  expected_equity = initial + _bot_closed_pnl_total - fees - funding
  实际权益   = 7776
  bot记录的PnL = 只有路径1的PnL (~50%的实际PnL)
  预期权益   = 10000 + (部分PnL) - fees → 系统性偏低
  偏差       = 7776 - 预期 → 系统性偏大 → 频繁误报 ALARM!
```

**正确公式**: 所有 7 个平仓路径都应调用 `_accumulate_closed_pnl(pnl)`。

**修复**: 提取公共方法 `_accumulate_closed_pnl(pnl)`，在所有平仓路径调用。

---

### 🔴 High 发现 2: performance_tracker 丢失 ~48% 的交易

**证据**:
```
strategy_performance.json total_pnl = -1,148.13 USDT  (仅 pullback)
实际权益变化                        = -2,222.56 USDT  (10000 → 7777)
未解释差异                          = -1,074.43 USDT  (48.4%!)
```

**根因**: `perf.record_trade()` 仅在 `_detect_closed_positions` (路径1) 中调用。其他 6 个平仓路径不调用。

**影响**:
- `total_pnl` 被严重低估（仅捕获 ~52% 的亏损）
- `win_rate` 不准确（样本不完整）
- `avg_win / avg_loss` 扭曲
- `sharpe_ratio` 偏差
- `profit_factor` 偏差
- 所有基于 `total_pnl` 的指标都不可信

**正确公式**:
```python
total_pnl = sum(ALL closed trades' pnl)  # 应覆盖7个路径
```

**修复**: 所有平仓路径统一调用 `perf.record_trade()`。

---

### 🔴 High 发现 3: max_drawdown 追踪 PnL 序列而非权益曲线

**当前公式** (performance_tracker.py:201-215):
```python
cumulative_pnl = []
for t in trades: cumulative_pnl.append(sum so far)
max_drawdown = max peak-to-trough of cumulative_pnl
```

**正确公式**:
```python
# 应使用权益曲线
max_drawdown = max(peak_equity - trough_equity) / peak_equity
```

**示例说明偏差**:
```
场景: 开仓后立即浮亏 -500 USDT, 然后价格回升, 最终盈利 +100 平仓

正确回撤: (10000→9500) = -5%
当前公式: cumulative_pnl = [+100], max_drawdown = 0  ← 完全忽略浮亏阶段!
```

**影响**: max_drawdown 被系统性低估，Calmar Ratio 虚高。

**修复**: 应从 `log_cycle` 的 `cycle_equity` 序列计算回撤，而非从已平仓 PnL 序列。

---

### 🟡 Medium 发现 4: Dashboard "权益" = "余额" (显示混淆)

**当前代码** (dashboard.py:156, 170-171):
```python
equity = total                         # line 156
print(f"权益: {equity}")              # line 170 — accountEquity ✅
print(f"余额: {total}")              # line 171 — accountEquity ❌ 标签错误
```

**问题**: "余额"在中文交易语境中通常指可用资金（可提现/可开仓的钱），但这里显示的是总权益（包含保证金+浮盈）。

**正确显示**:
```
权益: 7776.48 USDT     (= accountEquity)
余额: 7384.31 USDT     (= available/free, 可提现余额)
可用: 7384.31 USDT     (= available/free)
保证金: 78 USDT (1.0%)
```

**修复**: "余额" 改为显示 `free`（available balance）。

---

### 🟡 Medium 发现 5: 规则引擎/AUTO_SL 平仓使用未实现盈亏

**当前代码** (deepseek_quant_bot.py:866):
```python
# 规则引擎平仓时
self.tlogger.log_position_close(
    pnl=upl,            # ← 未实现盈亏 (平仓决策时的快照)
    pnl_pct=roi * 100,  # ← 保证金ROI
)
```

**问题**: 平仓时记录的是决策时的未实现盈亏，不是实际成交后的已实现盈亏。市价单可能有滑点，实际 PnL 会不同。

**正确做法**: 平仓成功后从 Bitget API 获取实际 `totalPnl`。

**影响**: 中等 — 通常差异不大（滑点 0.01-0.1%），但在剧烈波动时可能差很多。

---

### 🟡 Medium 发现 6: POSITION_CLOSE 事件重复记录

**问题**: 同一个平仓可能被多个路径检测到并记录:
1. 规则引擎/AUTO_SL 主动平仓 → 记录 POSITION_CLOSE
2. 下一个周期 `_detect_closed_positions` 发现仓位消失 → 再记录一次 POSITION_CLOSE

**证据**: `strategy_performance.json` 中同一交易重复 2-5 次（PHASE1_SUMMARY 已记录）。

**影响**: `performance_tracker` 中已不完整的交易数据还被重复计数，进一步扭曲指标。

**修复**: `_detect_closed_positions` 开始前检查 `_closed_positions_done` 是否已包含该仓位。

---

### 🟢 Low 发现 7: status.json 字段冗余

status.json 中 `account.equity`、`account.total_balance`、`balance` 三个字段值相同（都是 accountEquity）。冗余但不算错误。

---

## 五、9000→7769 与 -22.23% 的正解

### 两个数字都正确，只是基准不同

```
-22.23% = (7776.48 / 10000 - 1) × 100    ← 累计收益率 (从初始10000算起)
-13.7%  = (7776.48 / 9000 - 1) × 100     ← 本周收益率 (从周初9000算起)
```

**数据流**:
```
dashboard -22.23%:
  equity = fetch_balance()["USDT"]["total"] = 7776.48
  initial = INITIAL_EQUITY (env) = 10000
  → (7776.48/10000 - 1) × 100 = -22.2352% → 显示 -22.24%

PHASE1_SUMMARY -22.23%:
  same formula, 最后快照时的权益为 7777.44
  → (7777.44/10000 - 1) × 100 = -22.2256% → 显示 -22.23%

两者公式完全一致，微小差异来自快照时间不同（7777.44 vs 7776.48）
```

### 但 CYCLE 日志中的权益与实际权益不一致！

从 hook 数据看:
- Cycle 40-45: equity = 10436 ~ 10881（高于初始 10000！）
- Dashboard 同时显示: equity = 7776

**原因**: Hook 中的 CYCLE 数据是从 `logs/trades.jsonl` 历史文件摘要的，来自不同时间段。Cycle 40-45 时的权益确实在 10000+，之后发生了大幅回撤到 7776。两者不矛盾。

---

## 六、修复建议

### 优先级 1: 统一平仓 PnL 累加 (Critical)

创建公共方法，所有 7 个平仓路径统一调用:
```python
def _accumulate_closed_pnl(self, pnl: float, base: str, strategy: str,
                           entry: float, exit_p: float, pnl_pct: float,
                           reason: str, dur_min: float):
    """所有平仓路径的统一 PnL 累加点"""
    self._bot_closed_pnl_total += pnl
    self._bot_closed_trade_count += 1
    self.riskmon.record_closed_trade(pnl, base)
    if self.perf:
        self.perf.record_trade(...)
```

在以下位置调用:
- `_review_open_positions` (line ~860)
- AUTO_SL (line ~2932)
- 僵尸仓退出 (line ~2976)
- 日亏损清仓 (line ~2638)
- 紧急停止 (safety_manager.py)
- Rotation 轮换 (line ~2449)

### 优先级 2: 修复 max_drawdown (High)

改为基于 CYCLE 日志中的 `cycle_equity` 序列:
```python
def _max_drawdown_from_equity(self, equity_history: list):
    peak = equity_history[0]
    max_dd = 0.0
    for eq in equity_history:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
```

### 优先级 3: 修复 Dashboard 显示 (Medium)

```python
# dashboard.py
"权益": bal["total"]["USDT"]    # ← accountEquity (不变)
"余额": bal["free"]["USDT"]     # ← available (修复!)
```

### 优先级 4: 防重复平仓记录 (Medium)

```python
# _detect_closed_positions 入口
if sym in self._closed_positions_done:
    continue  # 本周期已由主动平仓路径处理
```

---

## 七、公式速查表

### 正确公式 (Bitget 标准)

| 指标 | 公式 | 基准 |
|------|------|------|
| 权益 | `accountEquity = available + locked + unrealizedPL` | — |
| 可用余额 | `available` (Bitget API) | — |
| 已用保证金 | `Σ marginSize` (各仓位) | — |
| 未实现盈亏 | `Σ unrealizedPL` (各仓位) | — |
| 已实现盈亏 | `Σ totalPnl` (Bitget 历史API) | — |
| 累计收益率 | `(currentEquity / initialEquity - 1) × 100` | 初始权益 |
| 日收益率 | `(equityToday - equityDayStart) / equityDayStart` | 日初权益 |
| 周收益率 | `(equityWeekEnd - equityWeekStart) / equityWeekStart` | 周初权益 |
| 持仓ROI (保证金) | `unrealizedPL / marginSize × 100` | 保证金 |
| 持仓ROI (权益) | `unrealizedPL / accountEquity × 100` | 权益 |
| 最大回撤 | `max(peak_equity - trough_equity) / peak_equity` | 权益峰值 |

### 当前实现的问题

| 指标 | 当前公式 | 问题 |
|------|----------|------|
| 已实现盈亏 (累积) | 仅路径1的 PnL | 缺失 ~48% (6个路径未累加) |
| strategy total_pnl | 仅路径1 + 重复记录 | 严重低估且扭曲 |
| max_drawdown | peak-to-trough of PnL seq | 不是权益回撤，忽略浮亏 |
| Dashboard "余额" | accountEquity | 标签错误，应为 available |
| 规则平台 PnL | upl (未实现) | 不是最终已实现 PnL |

---

**审计完成日期**: 2026-06-13  
**审计人**: Claude (代码全链路追踪 + Bitget API 文档对照)
