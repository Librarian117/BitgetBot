# Project Structure v2

> 当前文档以真实运行数据流为核心，而不是以文件列表为核心。所有架构判断以当前代码行为为准；旧文档仅作为历史参照。

## 1. System Boundary

### In Scope

当前 live bot runtime 包括:

- `deepseek_quant_bot.py` as main orchestrator
- `exchange_interface.py` as Bitget/ccxt adapter
- `trade_executor.py` as entry execution and position sizing
- `risk_monitor.py` as daily loss / streak / fee state
- `equity_auditor.py` as exchange-equity reconciliation
- `performance_tracker.py` as current performance metrics store
- `trade_logger.py` as structured event log
- `signal_scorer.py` as strategy-aware scoring
- `indicator_calculator.py` and `quant_math.py` as indicator/quant signal providers
- optional modules: `flow_monitor.py`, `portfolio_manager.py`, `self_learner.py`, `trailing_sl.py`, `grid_strategy.py`, `safety_manager.py`, `health_monitor.py`, `genetic_evolver.py`, `news_integration.py`

External systems:

- Bitget Futures API through ccxt
- DeepSeek research layer
- optional news / sentiment / market context providers

Runtime data files:

- `data/status.json`
- `data/positions_state.json`
- `data/closed_trades_keys.txt`
- `logs/trade_ledger.jsonl`
- `logs/trades_YYYY-MM-DD.jsonl`
- `performance.json`
- `logs/equity_audit.json`
- `logs/equity_alarms.jsonl`

### Out Of Scope

- `archive/` is historical snapshot.
- `freqtrade/` is an independent migration/experiment area, not current live bot runtime.
- `docs/archive/` is historical design material.
- `audit_trades.py` and some `tools/` scripts are offline/legacy analysis utilities unless explicitly wired into the live flow.

## 2. System Architecture

The current system is a monolithic orchestrator with extracted helper modules.

```text
Market Data
  -> Indicators / Quant Signals
  -> Strategy Candidate
  -> Filter / Scoring
  -> Risk Gate
  -> Position Sizing
  -> Entry Execution
  -> Open Position Protection
  -> Exit Trigger
  -> Close Execution
  -> Finalize
  -> Ledger / Logs / Performance / Equity Audit
```

### Main Modules

| Module | Responsibility | Relationship |
|---|---|---|
| `DeepSeekQuantBot` | Coordinates cycle phases, signal scan, filters, execution, protection, close detection, finalize | Calls every runtime module |
| `ExchangeInterface` | Bitget market/account/order/TPSL/PnL adapter | SSoT reader for account, position, market, close PnL |
| `IndicatorCalculator` / `quant_math` | EMA, RSI, ATR, ADX, MACD, Bollinger, Kalman, Hurst, vol cone | Feed strategy/filter layer |
| `SignalScorer` | Strategy-aware confidence and required-score calculation | Consumes signal, regime, Kalman conflict, flow, portfolio, learner |
| `TradeExecutor` | Position sizing, leverage, SL/TP construction, market entry | Uses exchange adapter; returns entry result |
| `RiskMonitor` | Daily loss, streak cooldown, fee/funding counters | Reads account equity; updated by finalize |
| `SafetyManager` | API safety, emergency drawdown detection, legacy emergency close | Main loop uses drawdown check; old close-all path is legacy |
| `TrailingStopManager` | Profit lock and trailing SL updates | Runs during position protection |
| `EquityAuditor` | Compares exchange account equity vs bot expected equity | Uses exchange equity as truth |
| `PerformanceTracker` | Current metrics store in `performance.json` | Updated by finalize, but not ledger-derived yet |
| `TradeLogger` | Daily structured JSONL logs | Audit log, not the new ledger |
| `GridManager` | Optional range/grid subsystem | Not fully ledger-first |
| `PortfolioManager` | Candidate ranking and correlation/position multiplier | Runs before execution |
| `FlowMonitor` | OI/order-flow signal and position multiplier | Filter and sizing input |
| `SelfLearner` | Learns from closed trades and direction outcomes | Updated by finalize |
| `HealthMonitor` | Health status and protection checks | Monitoring support |

## 3. Single Source Of Truth Map

| Domain | Current SSoT | Duplicate Sources | Status |
|---|---|---|---|
| Market price / OHLCV | Bitget API | `_tf_cache`, candidate logs | OK |
| Open positions | Bitget `fetch_positions()` | `_prev_positions`, `data/positions_state.json`, `_position_open_times` | OK with cache caveat |
| Account equity | Bitget `accountEquity` via ccxt total balance | `RiskMonitor.current_equity`, `data/status.json` | OK |
| Entry order | Bitget order/position | `TRADE`, `ENTRY_SNAPSHOT`, `_this_cycle_trades` | Mostly OK |
| Close event | `_finalize_closed_position()` result | `POSITION_CLOSE`, `EXIT_SNAPSHOT`, performance, ledger | Partially aligned |
| Closed trade ledger | `logs/trade_ledger.jsonl` (write + performance read) | `performance.json` (fallback cache), daily logs (audit only), memory counters | Partially aligned — performance reads ledger; equity/watchdog not yet fully ledger-derived |
| Realized PnL | API history-position first, fallback second | daily logs, performance, `_bot_closed_pnl_total` | Aligned — P0-4 fixed totalFunding/netProfit mapping; P0-5 fixed fee sign |
| Funding fee | Bitget history-position `totalFunding` + open-position fallback | — | FIXED (P0-4) — adapter now maps `totalFunding`; ledger `funding` field populated |
| Performance | `logs/trade_ledger.jsonl` (primary read) | `performance.json` (compatibility cache, fallback when ledger missing) | Ledger-first since P1 Commit 2; `performance.json` retained as fallback |
| Equity audit | Exchange accountEquity vs dual-basis expected equity | — | FIXED (P1 Commit 3) — realized and with-upl bases; open UPL no longer triggers false alarm |
| Dashboard live state | Bitget direct query | status file | OK for live state, not ledger view |
| Watchdog | `data/status.json` + `trade_ledger.jsonl` TRADE_CLOSE | old daily logs (fallback) | FIXED (P1 Commit 4) |
| Offline audit | `trade_ledger.jsonl` TRADE_CLOSE with trade_id dedup | — | FIXED (P1 Commit 4) |

## 4. Strategy Layer

### Strategy Overview

| Strategy | Role | Trigger Conditions | Suitable Market | Main Filters |
|---|---|---|---|---|
| Pullback | Trend pullback continuation | LONG: price > EMA and RSI below effective oversold. SHORT: price < EMA and RSI above effective overbought | bull, bear, strong_bull, strong_bear, also range with caution | RSI, EMA trend, Regime allowed directions, Kalman resolver, Confidence, BTC, Funding, Direction learner |
| Momentum / Breakout | Trend acceleration | Price above/below EMA, RSI in momentum band, ADX above momentum threshold, breakout of recent high/low | strong_bull, strong_bear, strong ADX range | ADX strategy threshold, RSI momentum band, recent high/low breakout, Regime, Confidence, BTC |
| EMA Cross | Fast/slow trend-following | EMA9 crosses EMA21, with EMA200 / RSI sanity check | mild trend and range-to-trend transitions | EMA cross, EMA200, RSI sanity, Regime, Confidence |
| Bollinger | Mean reversion | Price touches lower/upper Bollinger band, band width not too narrow, RSI not extreme | range / high enough band width | Bollinger width, RSI, Regime, Confidence |
| Counter Trend | Controlled reversal / deadlock escape | Active: extreme RSI. Passive: Pullback downgraded by Kalman or direction learner | trend exhaustion, panic/reversal, conflict state | extreme RSI, higher required score, Kalman resolver, half position, cluster limit |
| Grid | Range harvesting | Low-volatility/range environment, deployed through `GridManager` | low-volatility range | Regime range, Bollinger/grid levels, order lifecycle checks |

### Strategy Notes

- Strategy generation is not separated into individual classes. Most strategy logic is inside `DeepSeekQuantBot.scan_single_symbol()`.
- Grid is separate and optional; it is not fully integrated into finalize/ledger.
- DeepSeek is not the decision-maker. The current execution path treats AI as research/commentary, while quant score gates decide.

## 5. Filter Layer

### Filter Overview

| Filter | Input | Output | Affects Strategies |
|---|---|---|---|
| RSI | Closed candle RSI, adaptive drought thresholds | Pullback trigger, counter_trend extreme gate, partial score component for non-pullback | Pullback, Counter Trend, Momentum, Bollinger, EMA Cross |
| ADX | Closed candle ADX | Regime trend_strength; momentum-specific threshold | Momentum, Regime-driven strategy routing |
| EMA trend | EMA50/EMA200, EMA9/EMA21, price vs EMA | bullish/bearish bias, block/downgrade, EMA cross signal | Pullback, EMA Cross, Counter Trend |
| Kalman | Kalman direction and score | `allow`, `penalize`, or `downgrade` to counter_trend | Pullback, Counter Trend, Momentum, EMA Cross |
| Hurst | Hurst regime from `compute_quant_signals()` | trend / mean-reversion context | Mainly scoring and entry validation |
| Regime | Higher timeframe trend, ATR%, ADX, Markov bias, volume ratio | regime label, recommended strategies, allowed directions | All strategies |
| Confidence / SignalScorer | Candidate, bonuses, regime, flow, portfolio rank, learner, Kalman conflict | score and required_score | All strategies |
| Volume | Closed candle volume ratio vs session threshold | reject or pass; skipped in sandbox | All strategies |
| BTC linkage | BTC timeframe change | blocks non-BTC longs during BTC dump or shorts during BTC pump | All non-BTC strategies |
| Funding | Funding rate per candidate | direction/funding risk input | Mainly directional futures entries |
| OI / Flow | Open interest, flow signal, divergence | score adjustment and position multiplier | Mostly momentum and directional entries |
| Portfolio | correlation/rank among candidates | candidate rank and position multiplier | All strategies |
| Direction learner | rolling direction win rate | direction pause or downgrade | Pullback, Momentum, EMA Cross |
| News/Sentiment | optional news context and sentiment score | research context / sentiment signal | Informational; not primary decision source |

### Filter Architecture Principle

The current code already attempts “evidence consumed once”:

- ADX no longer acts as global hard filter, Regime score boost, and confidence boost simultaneously.
- Kalman no longer modifies strategy, confidence, required_score, and direction gate in scattered places; `_resolve_kalman()` is the single decision point.
- RSI is split: Pullback uses RSI for entry trigger, not repeated RSI deep bonus.

Remaining risk:

<span style="color:red"><strong>ARCHITECTURE MISALIGNMENT</strong></span>: Filters are conceptually layered but still implemented mostly as scattered logic inside `deepseek_quant_bot.py`, making future drift likely.

## 6. Risk Layer

### Position Sizing

Position sizing happens in `TradeExecutor.execute()` before entry order placement.

Inputs:

- available USDT balance
- session multiplier
- ADX-derived margin ratio
- Kelly multiplier
- portfolio rank multiplier
- OI multiplier
- counter_trend half-size multiplier
- per-trade max margin cap
- total margin cap
- exchange min amount / contract size

Protects:

- single-position oversizing
- portfolio overexposure
- min-contract forced oversizing
- excessive leverage liquidation risk

### Stop Loss

Stop loss is attached immediately after entry through position-level TPSL where possible.

Flow:

```text
Entry market order
  -> set_position_sl_tp()
  -> pos-tpsl primary path
  -> Place TPSL Order fallback
  -> if no SL confirmed, protective close
```

Protects:

- naked position risk
- exchange-side price move when bot is offline
- catastrophic single-trade loss

Risk:

<span style="color:green"><strong>FIXED (P0-2)</strong></span>: protective close after TPSL failure now returns close context to bot layer, which calls `_finalize_closed_position()`. Naked position protection is preserved; the close is no longer a silent accounting black hole.

### Direction Block

Direction blocking happens before execution.

Inputs:

- Regime allowed directions
- EMA/Kalman conflict
- BTC linkage
- Direction learner win rate
- portfolio direction concentration

Protects:

- fighting higher timeframe trend
- over-concentration long or short
- repeating a direction with poor recent results

### ROI Protection

ROI protection happens in the position-protection phase before new entries.

Mechanisms:

- rule TP for high ROI
- rule emergency for extreme negative ROI
- auto floating-loss stop after minimum hold time
- stale/zombie exit for capital release
- trailing stop and profit lock updates

Protects:

- giving back large floating gains
- holding dead capital
- leaving losing positions too long
- account-level drag from stale positions

### Emergency Exit

Emergency exit happens at the start of every cycle.

Mechanisms:

- emergency drawdown window via `SafetyManager.check_emergency_stop()`
- daily loss guard via `RiskMonitor.can_trade_with_data()`
- `DAILY_LOSS_CLOSE_ALL`
- retry loop if positions remain after close

Protects:

- account-level drawdown
- exchange/API failure during liquidation attempt
- daily hard-loss breach

## 7. Execution Layer

### Entry

Entry path:

```text
candidate passes filters
  -> _phase_execute()
  -> TradeExecutor.execute()
  -> position size and leverage validation
  -> market order with tradeSide=open
  -> set SL/TP
  -> log TRADE and ENTRY_SNAPSHOT
  -> store _position_open_times / _position_strategies / _open_trade_factors
```

Current entry SSoT:

- Bitget filled order and Bitget open position.

Local entry snapshots are audit context, not truth.

### Exit

Active exit path:

```text
exit condition
  -> exchange.create_market_order_close()
  -> _finalize_closed_position()
```

Passive exit path:

```text
Bitget position disappears
  -> _phase_post_cycle() detects previous vs current position diff
  -> _finalize_closed_position(close_order_result=None)
```

Finalize close reasons include:

- `RULE_TP`
- `RULE_SL`
- `RULE_EMERGENCY`
- `RULE_STALE`
- `MARKET_CLOSE`
- `ROTATION_CLOSE`
- `DAILY_LOSS_CLOSE`
- `AUTO_SL`
- `STALE_EXIT`
- `EMERGENCY_STOP`
- `STARTUP_SYNC`

### Order Management

Current order management responsibilities:

- entry market order through ccxt
- position TPSL primary path through Bitget private endpoint
- fallback TPSL through `Place Tpsl Order` style `pos_loss` / `pos_profit`
- residual reduce-only stop-order cancellation in finalize
- position cache and TPSL memory cache
- grid TP/SL management in `GridManager`

API alignment risks:

- <span style="color:green"><strong>FIXED (P0-1)</strong></span>: hedge close fallback direction now correctly maps `long -> buy`, `short -> sell` per Bitget Place Order docs. Primary path uses `ccxt.close_position()`.
- <span style="color:red"><strong>REMAINING</strong></span>: `reduceOnly` is documented as one-way mode only; grid still uses `reduceOnly=True`.

## 8. Unified Close / Finalize Layer

`_finalize_closed_position()` is the current hidden core of the system.

Responsibilities:

1. Build dedup key.
2. Prevent duplicate processing.
3. Fetch API PnL from Bitget history-position.
4. Fallback to `upl - estimated_fee`.
5. Record fees and funding fees.
6. Compute `pnl_pct`.
7. Clear TPSL cache.
8. Cancel residual stop orders.
9. Update `RiskMonitor`.
10. Update `PerformanceTracker`.
11. Update `_bot_closed_pnl_total`.
12. Update `SelfLearner`.
13. Write `POSITION_CLOSE`.
14. Write `EXIT_SNAPSHOT`.
15. Write `logs/trade_ledger.jsonl`.
16. Persist dedup key.

P0/P1 additions:

17. <span style="color:green"><strong>P0-3</strong></span>: Exception fallback now writes minimal `TRADE_CLOSE` ledger + persists dedup key (`pnl_source=FALLBACK_EXCEPTION`).
18. <span style="color:green"><strong>P0-2</strong></span>: TPSL failure protective close now returns close context, routed through finalize (`close_reason=TPSL_FAILURE_PROTECTIVE_CLOSE`).
19. <span style="color:green"><strong>P0-5</strong></span>: Fee recording now uses `abs(api_fee)` — Bitget returns negative fee values; previously `>0` check silently skipped all API-path fee recording.

This is the most important hidden refactor in the current codebase.

<span style="color:orange"><strong>PARTIALLY ADDRESSED</strong></span>: Finalize is the single write point. Performance now reads from ledger (P1). Equity audit has dual-basis (P1). Watchdog and audit_trades read from ledger (P1). But self-learner, risk-monitor memory counters, and multi-dimensional performance splits still use non-ledger sources.

## 9. Ledger System

Ledger file:

```text
logs/trade_ledger.jsonl
```

Ledger event:

```text
TRADE_CLOSE
```

Ledger role:

- intended unique closed-trade ledger
- one line per finalized close
- stores PnL source and close reason
- deduped by close order id or open timestamp/contracts

Current limitations (P1 status):

- <span style="color:green"><strong>FIXED</strong></span>: performance `get_metrics()` reads from ledger first, `self.trades` as fallback (P1 Commit 2). `performance.json` retained as compatibility cache.
- <span style="color:green"><strong>FIXED</strong></span>: watchdog now counts `TRADE_CLOSE` in `trade_ledger.jsonl` (P1 Commit 4).
- <span style="color:green"><strong>FIXED</strong></span>: `audit_trades.py` now reads `trade_ledger.jsonl`, deduplicates by `trade_id` (P1 Commit 4).
- <span style="color:orange"><strong>REMAINING</strong></span>: equity audit does not rebuild from ledger — uses `_bot_closed_pnl_total` memory (but dual-basis reduces noise).
- <span style="color:orange"><strong>REMAINING</strong></span>: dashboard does not show ledger realized PnL — shows live exchange state only.

Required architecture rule:

```text
Only Finalize may create a TRADE_CLOSE ledger record.
All downstream closed-trade analytics must derive from TRADE_CLOSE.
```

## 10. PnL Calculation System

### API PnL

Primary source:

- Bitget `Get History Position`

Current adapter reads (P0-4):

- `pnl` (gross realized PnL — confirmed via sandbox)
- `netProfit` (net profit — audit field in ledger)
- `totalFunding` → `funding_fee` (accumulated funding cost)
- `closeAvgPrice` → `exit_price`
- `closeFee` + `openFee` → `fee`
- `positionId` (audit trace)

<span style="color:green"><strong>FIXED (P0-4 + P0-5)</strong></span>: `totalFunding` and `netProfit` are now mapped. Fee sign bug fixed — `abs()` used for recording. Sandbox confirmed `pnl` is gross: `netProfit = pnl - |openFee| - |closeFee| - |totalFunding|`.

### Fallback PnL

Fallback source:

```text
pnl = unrealized_pnl_at_detection - estimated_round_trip_taker_fee
```

Used when:

- Bitget history-position is unavailable
- API returns no record
- API PnL is near zero
- startup sync has weak context

Fallback risk:

- stale UPL
- missing funding fee
- final close price unknown

## 11. Equity System

Current equity truth:

- Bitget account equity through `ExchangeInterface.get_account_summary().equity`

Official alignment:

- Bitget account equity includes unrealized PnL.
- Current code correctly avoids adding unrealized PnL twice.

Equity auditor (P1 Commit 3 — dual-basis):

```text
expected_realized = initial + realized_pnl - fees - funding       (bot internal)
expected_with_upl = expected_realized + open_upl                  (aligns with accountEquity)
deviation_realized  = exchange_equity - expected_realized
deviation_with_upl  = exchange_equity - expected_with_upl
alarm = |deviation_realized| > threshold AND (|deviation_with_upl| > threshold OR open_positions == 0)
```

<span style="color:green"><strong>FIXED (P1 Commit 3)</strong></span>: dual-basis audit. Open UPL no longer triggers false alarms. Realized drift still detected. Both deviations recorded in `equity_alarms.jsonl`.

## 12. Performance System

Current (P1 Commit 1+2):

- `PerformanceTracker.record_trade()` still called by finalize — writes `self.trades` + `performance.json`.
- `get_metrics()` (runtime) reads from `trade_ledger.jsonl` first (mtime-based cache); falls back to `self.trades` if ledger missing/empty.
- `load_trades_from_ledger()` / `get_metrics_from_ledger()` available for offline rebuild.
- Computes win rate, profit factor, Sharpe, Sortino, Calmar, drawdown, strategy splits.

Status:

<span style="color:orange"><strong>PARTIALLY MIGRATED</strong></span>: runtime `get_metrics()` is ledger-first. `record_trade()` and `performance.json` retained for backward compatibility. Multi-dimensional splits (`get_strategy_metrics`, etc.) still use `self.trades`, not ledger.

## 13. Data Flow Diagram

### CURRENT (P0 + P1 Commit 1-4)

```text
+-------------+
| Market Data |
| Bitget      |
+------+------+
       |
       v
+-------------+
| Signal      |
| Strategies  |
+------+------+
       |
       v
+-------------+
| Filter      |
| Regime etc. |
+------+------+
       |
       v
+-------------+
| Risk        |
| account and |
| signal gate |
+------+------+
       |
       v
+-------------+
| Position    |
| sizing      |
+------+------+
       |
       v
+-------------+
| Execution   |
| entry order |
| TPSL attach |
+------+------+
       |
       v
+-------------+
| Open        |
| Position    |
+------+------+
       |
       v
+-------------+
| Exit        |
| trigger     |
+------+------+
       |
       v
+-------------+
| Finalize    |
| PnL + dedup |
+------+------+
       |
       +-----> Ledger (TRADE_CLOSE)
       |           |
       |           +-----> Performance (ledger-first read)
       |           +-----> Watchdog (TRADE_CLOSE count)
       |           +-----> audit_trades (trade_id dedup)
       |
       +-----> RiskMonitor (memory)
       +-----> PerformanceTracker (compat write)
       +-----> SelfLearner
       +-----> POSITION_CLOSE / EXIT_SNAPSHOT (audit log)
       +-----> EquityAuditor (dual-basis, uses bot memory counters)
```

### TARGET (post-P1: ledger as single read source)

```text
Finalize -> Ledger -> Performance / Equity / Dashboard / Watchdog
```

## 14. Deprecated / Legacy / Dead Components

| Component | Status | Reason |
|---|---|---|
| `SafetyManager.emergency_close_all()` | Legacy bypass | Direct close without finalize; main loop now inlines emergency close and calls finalize |
| `audit_trades.py` | <span style="color:green">FIXED (P1)</span> | Now reads `trade_ledger.jsonl`, filters `TRADE_CLOSE`, deduplicates by `trade_id` |
| `bot_watchdog.py` | <span style="color:green">FIXED (P1)</span> | Now reads `data/status.json` and counts `TRADE_CLOSE` in `trade_ledger.jsonl`; falls back to old logs if ledger missing |
| `TradeExecutor.pop_rr_shadow()` | Likely dead | Defined but not used by main close/finalize path |
| `TradeExecutor.REGIME_MULTIPLIERS` | Non-functional | Keys (`strong_trend`, `ranging`, etc.) do not match regime labels (`strong_bull`, `bull`, `range`, etc.) — default multiplier ALWAYS used |
| `GridManager` close lifecycle | Not ledger-first | TP/SL orders are managed independently and use `reduceOnly=True` in hedge context |
| `performance.json` as trade store | <span style="color:orange">Compatibility cache</span> | Runtime reads from ledger; `performance.json` retained as fallback. Multi-dimensional splits still use `self.trades` |
| daily `POSITION_CLOSE` logs as accounting source | Audit log | No longer a downstream source — `trade_ledger.jsonl` is the closed-trade SSoT |
| `freqtrade/` | Separate system | Not current live architecture |
| `archive/` | Historical | Not runtime |

## 15. Architecture Summary

### Most Complex Module

`deepseek_quant_bot.py` is the most complex module. It contains orchestration, strategy generation, filters, close detection, finalize, status writing, and several risk phases. The highest architectural load is concentrated in one file.

### Most Critical Module

The most critical module is now `_finalize_closed_position()`. It is the only place where closed trade PnL, fees, risk state, performance, logs, learner, and ledger are intended to be committed.

Second critical dependency:

- `ExchangeInterface`, because Bitget account equity, close execution, TPSL, and history-position PnL all pass through it.

### Biggest Architecture Risk

The biggest remaining architecture risk is that not all downstream analytics read from ledger. Self-learner, risk-monitor memory counters, and multi-dimensional performance splits still use non-ledger sources.

<span style="color:orange"><strong>PARTIALLY ADDRESSED (P0+P1)</strong></span>: P0 fixed all known accounting gaps (fallback direction, TPSL bypass, exception black hole, fee sign, funding mapping). P1 made performance, equity audit, watchdog, and audit_trades read from ledger. Remaining gaps: self-learner, risk-monitor internal state, dimensional performance splits.

### Current State Verdict

```text
Finalize unified exit:        implemented (P0: all paths covered)
Ledger write path:             implemented
Ledger read model:             partially implemented (performance, watchdog, audit)
API PnL path:                  aligned (pnl gross, netProfit, totalFunding, fee sign)
Funding PnL path:              aligned (P0-4: totalFunding mapped)
Equity truth:                  exchange account equity
Equity audit:                  dual-basis (P1 Commit 3)
Performance truth:             ledger-first with performance.json fallback (P1 Commit 1+2)
Watchdog:                      aligned to ledger (P1 Commit 4)
Offline audit:                 aligned to ledger (P1 Commit 4)
Dashboard truth:               exchange live API
```

The system is closer to a robust trading lifecycle architecture than the old module-layer docs, but it must be documented as a finalize-centered system, not as a pure strategy-layer system.
