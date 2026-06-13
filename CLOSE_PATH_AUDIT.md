# CLOSE_PATH_AUDIT.md — 平仓路径统一出口重构

**重构日期**: 2026-06-13  
**变更文件**: `deepseek_quant_bot.py`, `trade_logger.py`

---

## 一、修复前 vs 修复后

### 修复前: 12 条路径各自为政

| # | 路径 | PnL累加 | perf | learner | 去重 | 费用 | funding | ledger |
|---|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 | pos-tpsl 检测 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| 2-5 | 规则引擎 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 6 | AUTO_SL | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 7 | STALE_EXIT | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 8 | Rotation | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 9 | Daily Loss | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 10 | Emergency | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 11 | 启动同步 | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |

**后果**: `_bot_closed_pnl_total` 仅捕获 ~52% PnL，`perf` 仅 ~52% 交易，重复记录 2-5x。

### 修复后: 全部走 `_finalize_closed_position`

| # | 路径 | close_reason | 统一出口 |
|---|------|-------------|:---:|
| 1 | pos-tpsl 检测 | `MARKET_CLOSE` | ✅ |
| 2 | 规则引擎 TP | `RULE_TP` | ✅ |
| 3 | 规则引擎 Emergency | `RULE_EMERGENCY` | ✅ |
| 4 | 规则引擎 SL | `RULE_SL` | ✅ |
| 5 | 规则引擎 Stale | `RULE_STALE` | ✅ |
| 6 | AUTO_SL | `AUTO_SL` | ✅ |
| 7 | STALE_EXIT | `STALE_EXIT` | ✅ |
| 8 | Rotation | `ROTATION_CLOSE` | ✅ |
| 9 | Daily Loss | `DAILY_LOSS_CLOSE` | ✅ |
| 10 | Emergency | `EMERGENCY_STOP` | ✅ |
| 11 | 启动同步 | `STARTUP_SYNC` | ✅ |

**效果**: 每条路径自动获得: API PnL获取、费用记录、funding记录、去重、ledger写入、PnL来源标记。

---

## 二、架构变更

### 新增方法 (deepseek_quant_bot.py)

| 方法 | 行数 | 功能 |
|------|------|------|
| `_build_dedup_key()` | ~15 | 三级去重键 (订单ID > 时间戳 > 回退) |
| `_write_trade_ledger()` | ~25 | 唯一交易账本写入 (logs/trade_ledger.jsonl) |
| `_finalize_closed_position()` | ~160 | 统一平仓出口 (幂等 + API重试 + 全部记录) |

### 三大约束

1. **幂等**: 同一 dedup_key 多次调用 → 返回缓存结果
2. **PnL 来源标记**: 每条记录含 `pnl_source: "API" | "FALLBACK"`
3. **单一写入入口**: `_bot_closed_pnl_total +=`, `perf.record_trade()`, `riskmon.record_closed_trade()` 等全部仅在 `_finalize_closed_position` 内部

### 核心流程

```
任何平仓触发
  → create_market_order_close()  (不变)
  → _finalize_closed_position()
      ├── 幂等检查 (dedup_key in cache?)
      ├── API PnL 获取 (0.5s/1s/2s/3s 重试)
      ├── PnL 来源决策 (API 成功→"API", 失败→"FALLBACK")
      ├── 费用记录 (仅此一处)
      ├── 风险监控 record_closed_trade
      ├── 绩效跟踪 perf.record_trade
      ├── 权益累加 _bot_closed_pnl_total
      ├── 自学习 learner
      ├── 日志 POSITION_CLOSE + EXIT_SNAPSHOT (含 pnl_source)
      ├── Trade Ledger
      ├── 清理 (_position_open_times, 计划单)
      └── 缓存结果 (幂等)
```

---

## 三、数据流统一

### 修复前

```
平仓 → 各路径独立处理
  ├── perf.record_trade()      # 仅路径1
  ├── _bot_closed_pnl_total    # 路径1+11
  ├── riskmon.record_closed    # 仅路径1
  ├── learner                  # 路径1+11
  ├── log_position_close       # 路径2-7
  └── 费用/funding             # 仅路径1

POSITION_CLOSE 重复记录: 主动平仓 + 周期检测 = 2-5x
```

### 修复后

```
平仓 → _finalize_closed_position()  (唯一入口)
  ├── perf.record_trade()          ✅ 所有路径
  ├── _bot_closed_pnl_total +=     ✅ 所有路径
  ├── riskmon.record_closed_trade  ✅ 所有路径
  ├── learner                      ✅ 所有路径
  ├── log_position_close           ✅ 所有路径 (含 funding_fee + pnl_source)
  ├── log_exit_snapshot            ✅ 所有路径
  ├── _write_trade_ledger          ✅ 所有路径
  └── 费用/funding记录             ✅ 所有路径

POSITION_CLOSE 只记录一次: 幂等 + 去重键保护
```

---

## 四、变更统计

| 项目 | 行数 |
|------|------|
| 新增 `_finalize_closed_position` | ~160 |
| 新增 `_build_dedup_key` | ~15 |
| 新增 `_write_trade_ledger` | ~25 |
| 新增 `_closed_trades_cache` 初始化 | ~3 |
| 路径 1 改造 (替换 ~150行 → ~20行) | -130 |
| 路径 2-5 改造 (_review_open_positions) | -10 |
| 路径 6-7 改造 (AUTO_SL + STALE) | -25 |
| 路径 8 改造 (Rotation) | +10 |
| 路径 9 改造 (Daily Loss) | +10 |
| 路径 10 改造 (Emergency — 重写) | +15 |
| 路径 11 改造 (Startup) | -20 |
| trade_logger.py `pnl_source` 字段 | +2 |
| **净变化** | **~+55 行** |

---

## 五、验收清单

| 验收项 | 状态 |
|--------|:---:|
| 所有 Python 语法检查通过 | ✅ |
| 11 条路径全部接入 `_finalize_closed_position` | ✅ |
| 幂等保护: `_closed_trades_cache` 去重 | ✅ |
| `pnl_source` 标记 (API/FALLBACK) | ✅ |
| 单一写入入口: 所有写操作仅在 finalize 内 | ✅ |
| Trade Ledger: `logs/trade_ledger.jsonl` | ✅ |
| `close_reason` 由调用方传入 (禁止推断) | ✅ |
| API 重试: 0.5s/1s/2s/3s | ✅ |
| Emergency 快速重试 (2次) | ✅ |
| 去重三级键: order_id > ts > fallback | ✅ |
| 旧 `POSITION_CLOSE` 重复记录消除 | ✅ (同 dedup_key 幂等) |
| 不改变交易逻辑/触发条件 | ✅ |

---

## 六、Trade Ledger 格式

```jsonl
{"event":"TRADE_CLOSE","trade_id":"LINK-SHORT-1718234400","symbol":"LINK",
 "side":"SHORT","strategy":"pullback","entry":7.898,"exit":7.74,
 "pnl":6.01,"pnl_pct":7.9,"pnl_source":"API","fee":0.12,"funding":0.0,
 "close_reason":"TAKE_PROFIT","open_ts":1718234100,"close_ts":1718234400,
 "duration_min":5.0}
```

后续 `performance_tracker`、`EquityAuditor`、dashboard 可统一从此读取。

---

**重构完成日期**: 2026-06-13
