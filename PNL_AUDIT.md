# PNL_AUDIT.md — PnL 计算链专项审计

**审计日期**: 2026-06-13  
**审计范围**: Bitget API → Position对象 → 风控模块 → 日志模块 → Attribution模块 → 日报模块  
**参考文档**: Bitget API v2 官方文档 (Get Single Position, Get Account List, Get All Positions, PnL Calculation)

---

## 目录

1. [Bitget API 字段权威定义](#一bitget-api-字段权威定义)
2. [PnL 计算链完整追踪](#二pnl-计算链完整追踪)
3. [10 项逐项审计](#三十项逐项审计)
4. [问题汇总](#四问题汇总)
5. [修正建议](#五修正建议)

---

## 一、Bitget API 字段权威定义

### 1.1 账户接口 (`/api/v2/mix/account/account-list`)

| 字段 | 类型 | 定义 | 来源 |
|------|------|------|------|
| `accountEquity` | String | 账户权益 = walletBalance + unrealizedPL | [Bitget API](https://www.bitgetapp.com/api-doc/contract/account/Get-Account-List) |
| `available` | String | 可用余额 | 同上 |
| `crossedMaxAvailable` | String | 全仓最大可用 | 同上 |
| `usdtEquity` | String | USDT 计价权益 | 同上 |
| `unrealizedPL` | String | 账户级未实现盈亏 | 同上 |
| `crossedUnrealizedPL` | String | 全仓未实现盈亏 | 同上 |
| `locked` | String | 冻结保证金 | 同上 |

**关键公式**:
```
accountEquity = available + locked + unrealizedPL
```

### 1.2 持仓接口 (`/api/v2/mix/position/single-position`)

| 字段 | 类型 | 定义 | 来源 |
|------|------|------|------|
| `unrealizedPL` | String | 未实现盈亏（基于标记价格） | [Bitget API](https://www.bitget.live/api-doc/contract/position/get-all-position) |
| `achievedProfits` | String | **已实现盈亏（不含手续费和资金费率！）** | 同上 |
| `totalFee` | String | 累计资金费率（持仓期间的 funding fee 累加） | 同上 |
| `deductedFee` | String | 累计交易手续费（持仓期间的 taker/maker fee） | 同上 |
| `marginSize` | String | 保证金金额（保证金币种计价） | 同上 |
| `openPriceAvg` | String | 平均开仓价 | 同上 |
| `markPrice` | String | 标记价格 | 同上 |
| `breakEvenPrice` | String | 盈亏平衡价 | 同上 |
| `total` | String | 总持仓量 (available + locked) | 同上 |
| `leverage` | String | 杠杆倍数 | 同上 |

**关键公式** (Bitget 官方):
```
LONG  unrealizedPL = (markPrice - openPriceAvg) × 合约张数 × 合约面值
SHORT unrealizedPL = (openPriceAvg - markPrice) × 合约张数 × 合约面值

完整持仓盈亏 = achievedProfits + totalFee + deductedFee
```

### 1.3 历史平仓接口 (`/api/v2/mix/position/history-position`)

| 字段 | 定义 |
|------|------|
| `totalPnl` | 总盈亏 = 平仓利润 + 手续费 + 资金费（完整） |
| `netPnl` | 净盈亏 |
| `fee` | 手续费 |
| `fundingFee` | 资金费率 |
| `openPrice` | 开仓价 |
| `closePrice` | 平仓价 |

**来源**: [Bitget PnL Calculation](https://www.bitget.com/asia/support/articles/12560603825004)

---

## 二、PnL 计算链完整追踪

### 追踪路径

```
┌──────────────────────────────────────────────────────────────────────┐
│  Bitget API                                                         │
│  ├── /v2/mix/account/account-list → accountEquity, available       │
│  ├── /v2/mix/position/all-position → unrealizedPL, marginSize      │
│  └── /v2/mix/position/history-position → totalPnl, fee             │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ ccxt 封装
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  exchange_interface.py                                              │
│  ├── get_account_summary()                                          │
│  │   equity = fetch_balance()["USDT"]["total"]     ← accountEquity │
│  │   unrealized_pnl = Σ per-position unrealizedPnl ← 持仓API       │
│  ├── get_open_positions()                                           │
│  │   返回: symbol, contracts, side, entryPrice, markPrice,          │
│  │         unrealizedPnl, initialMargin                             │
│  └── fetch_closed_position_pnl()                                    │
│      返回: { pnl, fee, open_fee, close_fee, exit_price } ← 历史API │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
┌─────────────────┐ ┌──────────────┐ ┌──────────────────┐
│ risk_monitor.py │ │ trade_logger │ │ deepseek_quant   │
│                 │ │              │ │ _bot.py          │
│ daily_pnl_pct = │ │ log_cycle()  │ │                  │
│ (eq_now -       │ │ balance ← eq │ │ _detect_closed   │
│  day_start) /   │ │ (mislabel!)  │ │ _positions()     │
│  day_start      │ │              │ │ pnl ← API.pnl    │
│                 │ │ log_position │ │   ← 或 fallback  │
│ cumulative_fees │ │ _close()     │ │   (NO FEES!)     │
│ tracked         │ │ pnl_pct ←    │ │                  │
│ separately      │ │ pnl/margin   │ │ _review_open     │
│                 │ │              │ │ _positions()     │
│ equity includes │ │              │ │ roi = upl/margin │
│ unrealized PnL  │ │              │ │ 规则引擎止损     │
└─────────────────┘ └──────────────┘ └──────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  performance_tracker.py / dashboard.py                               │
│  ├── total_pnl = Σ 所有交易 PnL (已实现，来源混合)                   │
│  ├── win_rate, avg_win, avg_loss, sharpe                            │
│  └── dashboard: equity = bal["total"]; all_time% = equity/init - 1  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 三、10 项逐项审计

---

### 审计项 1: ROI 计算公式

**代码位置**: `deepseek_quant_bot.py:815`, `dashboard.py:148`

**代码公式**:
```python
roi = upl / margin  # 所有上下文
```

**Bitget 定义**: 持仓 ROI = unrealizedPL / marginSize（保证金回报率）

**审计结论**: 🟢 **公式正确**

ROI 对保证金计算。这是行业内标准做法（ROI = return on investment，investment = margin）。但需注意：

| 上下文 | 分母 | 含义 |
|--------|------|------|
| 持仓 ROI (`_review_open_positions`) | margin | 保证金回报率 |
| 日 ROI (`risk_monitor.daily_pnl_pct`) | day_start_equity | 日初权益回报率 |
| 累计 ROI (dashboard) | initial_equity | 初始权益回报率 |

**标记**: 🟢 Low — 公式正确，但 **两种 ROI 概念（保证金 vs 权益）混用在系统中**，容易误解。

---

### 审计项 2: PnL 计算公式

**代码位置**: `deepseek_quant_bot.py:1573-1595`

**代码有两条路径**:

**路径 A — API 数据**:
```python
real_pnl = self.exchange.fetch_closed_position_pnl(sym)
pnl = real_pnl["pnl"]  # Bitget API 返回的 totalPnl
```
来源: Bitget `/v2/mix/position/history-position` → `totalPnl`

**路径 B — 估算（API 不可用时）**:
```python
# line 1588
pnl = (mark - entry) * contracts * csize if side == "LONG" else (entry - mark) * contracts * csize
```

**审计结论**: 🟡 Medium

- 路径 A (Bitget API) 的 `pnl` 已包含手续费和资金费率 → ✅ 正确
- 路径 B (估算) **未扣除任何手续费** → 🔴 高估 PnL
- 路径 B 还有一段启发式代码：`if abs(last_upnl) > abs(pnl) * 0.5` 会用 `last_upnl` 替代估算值 — 这可能导致使用错误的数值
- 两种路径下同一笔交易可能记录不同的 PnL

**标记**: 🟡 Medium — 双路径不一致导致 PnL 记录可能偏差（估算值偏高）

---

### 审计项 3: 未实现盈亏 (Unrealized PnL)

**代码位置**: `exchange_interface.py:173-181`

**代码逻辑**:
```python
for pos in positions:
    upnl = float(pos.get("unrealizedPnl", 0) or 0)
    total_upnl += upnl
```

来源: ccxt `fetch_positions()` → Bitget 持仓 API `unrealizedPL` 字段

**Bitget 公式**:
```
LONG  unrealizedPL = (markPrice - openPriceAvg) × contracts × contractSize
SHORT unrealizedPL = (openPriceAvg - markPrice) × contracts × contractSize
```

**审计结论**: 🟢 **正确**

代码直接使用交易所返回的 `unrealizedPL`，不自行计算。这避免了本地计算错误。

但是有一个细微问题：`get_account_summary()` 中 `equity = total_balance`（来自 `fetch_balance()["USDT"]["total"]`），这个 total 是 `accountEquity`，已经包含了 unrealized PnL。然后 `unrealized_pnl` 又被单独返回。如果消费方把 `equity` 和 `unrealized_pnl` 相加会重复计算 — 但目前代码中没有这样做。

**标记**: 🟢 Low — 直接使用交易所数据，正确。但耦合消费方需注意不重复加总。

---

### 审计项 4: 已实现盈亏 (Realized PnL)

**代码位置**: `exchange_interface.py:442-479`, `deepseek_quant_bot.py:1572-1591`

**代码逻辑**: 通过 `fetch_closed_position_pnl()` 调用 Bitget V2 历史仓位 API，获取 `pnl` 字段

**Bitget 定义**: `achievedProfits` = 已实现盈亏 **不含** 手续费和资金费率

**代码实际使用的字段**: 代码使用的是 `pnl` 字段（来自 V2 历史 API 的 `totalPnl`），**不是** `achievedProfits`

**审计结论**: 🟡 Medium

- ✅ 代码没有直接用 `achievedProfits`（那个才不含手续费）
- ✅ 代码从历史 API 获取 `totalPnl`（已含手续费）
- 🔴 但**回退路径不扣手续费** — 当 API 不可用时，估算 PnL = `(mark - entry) × contracts × size`，不扣任何费用
- 🔴 `_review_open_positions` 规则平仓路径直接用 `upl`（未实现盈亏）作为平仓盈亏记录

**标记**: 🟡 Medium — API 路径正确，回退路径缺失手续费扣除

---

### 审计项 5: 手续费计入

**代码位置**: `risk_monitor.py:114-115`, `deepseek_quant_bot.py:1576-1579`

**手续费跟踪**:
```python
# 平仓检测时 (line 1579)
close_fee = real_pnl.get("fee", 0)
if close_fee > 0:
    self.riskmon.record_trade_fees(close_fee)

# 开仓时 (trade_executor 调用 line 2281)
self.riskmon.record_trade_fees(result["total_fee"])
```

**审计结论**: 🔴 **Critical — 手续费可能被双重计入**

问题详细分析:

1. **开仓时** — `trade_executor.execute()` 在开仓成功后立即调用 `record_trade_fees(estimated_fee)`。这个 `estimated_fee` 是 **估算值**（`position_value × taker_fee × 2`），而不是实际发生的手续费。

2. **平仓时** — `_detect_closed_positions()` 从 Bitget API 获取的 `pnl` 字段（`totalPnl`）**已经包含了实际手续费**。然后代码又把 API 返回的 `close_fee` 加到了 `cumulative_fees`。

3. **结果**: 同一笔交易的手续费被记录了两次：
   - 开仓时：估算费（`record_trade_fees(estimated_fee)`）
   - 平仓时：实际费（`record_trade_fees(close_fee)`）
   - 同时 API 的 `pnl` 已经扣过费

4. **EquityAuditor 的累加**: `expected_equity = initial_equity + bot_realized_pnl - bot_fees`，其中 `bot_fees` 可能被重复累加。

**实际影响**: `cumulative_fees` 被高估（可能接近 2x），`EquityAuditor` 的对账偏差会被放大。

**标记**: 🔴 Critical — 手续费双重计入导致 `cumulative_fees` 和 `EquityAuditor` 偏差

---

### 审计项 6: Funding Fee 计入

**代码位置**: 全局搜索 `totalFee`, `fundingFee`, `funding` — **不存在**

**审计结论**: 🔴 **Critical — 资金费率完全未被跟踪**

- Bitget 持仓 API 返回 `totalFee` 字段（累计资金费率），但代码从未读取
- Bitget 历史平仓 API 的 `totalPnl` 包含资金费率（如果 Bitget 将其计入），但代码未单独记录
- `fetch_funding_rate()` 函数存在但仅用于"是否开仓"判断（费率极端时阻止开仓），不用于 PnL 计算
- 当回退公式计算 PnL 时（`(mark-entry)*contracts*size`），完全不包含资金费率

**实际影响**:
- 在正费率环境中，做空持仓收取资金费 → 实际 PnL 高于记录值
- 在负费率环境中，做多持仓支付资金费 → 实际 PnL 低于记录值
- 长时间持仓的 Funding Fee 可能累计到显著金额

**标记**: 🔴 Critical — 无 Funding Fee 跟踪，长时间持仓的 PnL 偏差可能很大

---

### 审计项 7: 仓位 ROI vs 账户 ROI 混用

**审计结论**: 🟡 **Medium — 确认混用且不规范**

| 计算位置 | 公式 | 分母 | 标签 |
|----------|------|------|------|
| `_review_open_positions:815` | `upl / margin` | 保证金 | "持仓ROI" |
| `_detect_closed_positions:1595` | `pnl / margin × 100` | 保证金 | "pnl_pct" |
| `dashboard.py:148` | `upl / mgn × 100` | 保证金 | "收益率" |
| `risk_monitor:110-112` | `(eq_now - day_start) / day_start` | 日初权益 | "daily_pnl_pct" |
| `risk_monitor:204-205` | `(eq_now - init) / init × 100` | 初始权益 | "all_time_pnl_pct" |
| `log_cycle()` | `balance` = `current_equity` | — | **字段名误导** |

**最关键问题**: `log_cycle()` 的参数名叫 `balance` 但实际传入的是 `current_equity`。日志 JSON 中的 `"balance"` 字段不是余额而是权益。

**标记**: 🟡 Medium — 标签混乱，`balance` ≠ `equity` 但日志中互换使用

---

### 审计项 8: 杠杆对 ROI 显示的影响

**审计结论**: 🔴 **High — 保证金 ROI 未标注杠杆，易误读**

持仓 ROI 公式: `roi = unrealizedPL / margin`

在 20x 杠杆下，价格波动 1% → 保证金波动 20%。所以：
- 价格涨 1% → 保证金 ROI = +20%（做多）
- 价格跌 1% → 保证金 ROI = -20%（做多）

**系统中 ROI 的消费方**:

1. **风控规则** (`_review_open_positions:832-839`): 使用保证金 ROI
   - "ROI > 40% → 止盈" 意味着价格波动 2% 就触发（在 20x 杠杆下）
   - "ROI < -50% → 紧急熔断" 意味着价格波动 2.5% 就触发

2. **仪表盘**: `收益率` 列显示保证金 ROI → 看起来盈亏很大，但实际只是价格微动

3. **日志**: `pnl_pct` 是保证金 ROI → 记录的是"杠杆后百分比"

**与非杠杆 ROI 的混淆**:
- 20x 杠杆下，保证金 ROI 5% = 价格波动 0.25%
- 报表中显示"盈利率 50.02%"实际是保证金 ROI，对应价格波动约 2.5%

**标记**: 🔴 High — `pnl_pct` 和 `收益率` 不标注杠杆倍数，数字被显著放大，外部阅读者会严重误判盈利能力

---

### 审计项 9: LONG/SHORT 计算对称性

**代码位置**: `deepseek_quant_bot.py:1588`, `deepseek_quant_bot.py:3287`

**代码逻辑**:
```python
# LONG
pnl = (mark - entry) * contracts * csize
# SHORT  
pnl = (entry - mark) * contracts * csize
```

**审计结论**: 🟢 **公式对称正确**

- LONG: 价格上涨→盈利 ✅
- SHORT: 价格下跌→盈利 ✅
- 两个方向的公式在数学上完全对称

但有一处不对称需要关注：**SL/TP 方向修正的温度检查** (`exchange_interface.py:728-739`):
```python
# LONG SL: sl_val < mark → 安全
if hold_side == "long" and sl_val >= mark:
    sl_str = self.exchange.price_to_precision(symbol, mark * 0.995)
# SHORT SL: sl_val > mark → 安全
elif hold_side == "short" and sl_val <= mark:
    sl_str = self.exchange.price_to_precision(symbol, mark * 1.005)
```
这里使用固定的 0.5% 偏移修正，对 LONG/SHORT 对称。✅

**标记**: 🟢 Low — 计算公式对称正确

---

### 审计项 10: API 字段与本地计算是否存在重复

**审计结论**: 🔴 **High — 权益重复风险 + 手续费双重计数**

**问题 A — equity 和 unrealized_pnl 同时返回**:
```python
# exchange_interface.py get_account_summary()
equity = total_balance           # ← 已包含 unrealized PnL
unrealized_pnl = total_upnl      # ← 独立求和
```
如果消费方错误地计算 `"真实权益" = equity + unrealized_pnl` 会重复计算。目前没有发现这种错误用法，但接口设计有隐患。

**问题 B — 手续费重复计数** (见审计项 5):
```
开仓: record_trade_fees(estimated_fee)  → cumulative_fees += est
平仓: record_trade_fees(close_fee)      → cumulative_fees += close_fee
API:  pnl 已扣费                         → bot_closed_pnl_total += pnl (已扣费)
```
结果：cumulative_fees 累加了两次，但 bot_closed_pnl_total 只累加了一次（已扣费）。

**问题 C — Bitget API pnl 字段含义**:
代码从 `fetch_closed_position_pnl()` 获取的 `pnl` 字段对应 Bitget API 的 `totalPnl`（完全结算后），但代码在回退路径中不扣费。两条路径的 PnL 内涵不一致。

**标记**: 🔴 High — 手续费可能双重计入，导致 `cumulative_fees` 虚高

---

## 四、问题汇总

| # | 审计项 | 严重级别 | 简述 |
|---|--------|----------|------|
| 5 | 手续费计入 | 🔴 **Critical** | 开仓估费 + 平仓实费双重记录，cumulative_fees 虚高 |
| 6 | Funding Fee | 🔴 **Critical** | 完全未跟踪，长时间持仓 PnL 偏差 |
| 8 | 杠杆 ROI 显示 | 🔴 **High** | pnl_pct 是保证金ROI不标注杠杆，严重误导 |
| 10 | API/本地重复计算 | 🔴 **High** | 手续费双重计入 + equity/unrealized 并发返回 |
| 2 | PnL 双路径不一致 | 🟡 **Medium** | API路径扣费，回退路径不扣费 |
| 4 | 已实现盈亏回退 | 🟡 **Medium** | 回退公式无手续费扣除 |
| 7 | 仓位ROI vs 账户ROI | 🟡 **Medium** | 两种ROI混用，balance字段名误导 |

---

## 五、修正建议

### 优先级 1: 修复手续费重复计入 (Critical)

**位置**: `deepseek_quant_bot.py:2281` (开仓) + `deepseek_quant_bot.py:1576-1579` (平仓)

**当前逻辑**:
```
开仓时: record_trade_fees(estimated_fee)     # → cumulative_fees
平仓时: record_trade_fees(api_close_fee)     # → cumulative_fees (重复!)
```

**建议修改**:
```python
# 开仓时不记录估算手续费到 cumulative_fees
# 仅记录到交易对象的本地字段，用于 EquityAuditor 的对账

# 平仓时从 API 获取实际手续费（已包含在 totalPnl 中）
# record_trade_fees() 仅在 API 不可用时使用估算值

# 修改后:
# 开仓: trade["estimated_fee"] = estimated_fee  (仅本地记录)
# 平仓: 从 API 获取 totalPnl (已含实际手续费)
#       cumulative_fees += actual_fee_from_api
```

### 优先级 2: 添加 Funding Fee 跟踪 (Critical)

**建议修改**:
```python
# exchange_interface.py 新增方法
def fetch_funding_fee_history(self, symbol: str, since: int) -> float:
    """获取持仓期间累计 funding fee"""
    # 使用 Bitget /v2/mix/account/account-bill 查询 funding 类型账单

# deepseek_quant_bot.py _detect_closed_positions 中
funding_fee = self.exchange.fetch_funding_fee_history(sym, open_time)
pnl_net = pnl - funding_fee  # 或从 API 获取 netPnl
self.tlogger.log_position_close(..., funding_fee=funding_fee)
```

### 优先级 3: PnL 呈现标注杠杆 (High)

**建议修改**:
```python
# trade_logger.py log_position_close
"pnl_pct_margin": round(pnl_pct, 2),      # 保证金 ROI
"pnl_pct_equity": round(pnl / equity * 100, 2),  # 权益 ROI (新增)

# dashboard.py 显示
"收益率(保证金)": f"{roi:+.1f}%",   # 标注"保证金"
"收益率(权益)":   f"{pnl/equity*100:+.1f}%",  # 新增

# log_cycle 中
"balance" → 改为 "equity" (修正字段名)
```

### 优先级 4: 统一 PnL 计算路径 (Medium)

**建议修改**:
```python
# deepseek_quant_bot.py 回退路径也扣除手续费
if not real_pnl:  # API 不可用
    pnl = (mark - entry) * contracts * csize  # gross PnL
    # 减去估算手续费
    estimated_fee = entry * contracts * csize * taker_fee * 2
    pnl -= estimated_fee  # ← 新增: 回退路径扣费
```

### 优先级 5: 修正错误字段标签 (Medium)

| 当前 | 应改为 |
|------|--------|
| `log_cycle()` 的 `"balance"` | `"equity"` |
| `log_cycle()` 的 `"pnl"` (实际是 daily_pnl_pct) | `"daily_pnl_pct"` |
| dashboard `"收益率"` | `"保证金收益率"` |

---

## 附录: 公式速查表

### Bitget 官方公式

| 公式 | 定义 |
|------|------|
| `accountEquity` | `available + locked + unrealizedPL` |
| `LONG unrealizedPL` | `(markPrice - openPriceAvg) × contracts × contractSize` |
| `SHORT unrealizedPL` | `(openPriceAvg - markPrice) × contracts × contractSize` |
| `完整持仓盈亏` | `achievedProfits + totalFee + deductedFee` |
| `history.totalPnl` | 平仓利润 + 开仓费 + 平仓费 + 资金费率 |

### 当前代码公式

| 位置 | 公式 | 正确性 |
|------|------|--------|
| `equity` | `fetch_balance()["USDT"]["total"]` | ✅ = accountEquity |
| `unrealized_pnl` | `Σ pos.unrealizedPnl` | ✅ |
| `roi (持仓)` | `unrealizedPnl / margin` | ✅ |
| `daily_pnl_pct` | `(eq_now - day_start) / day_start` | ✅ |
| `pnl (API路径)` | `history.totalPnl` | ✅ |
| `pnl (回退路径)` | `(mark - entry) × contracts × size` | 🔴 无手续费 |
| `pnl_pct` | `pnl / margin × 100` | 🟡 需标注杠杆 |
| `cumulative_fees` | `Σ 开仓估费 + Σ 平仓实费` | 🔴 双重计入 |
| `funding_fee` | 未跟踪 | 🔴 完全缺失 |

---

**审计完成日期**: 2026-06-13  
**审计人**: Claude (Bitget API v2 官方文档 + 代码全链路追踪)
