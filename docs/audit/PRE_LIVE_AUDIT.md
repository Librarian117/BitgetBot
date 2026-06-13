# PRE_LIVE_AUDIT.md — 实盘 Canary 部署前审计

**审计日期**: 2026-06-13  
**审计范围**: DeepSeekQuantBot v4.5 → Phase 2 实盘 Canary (50 USDT)  
**审计结论**: 🟡 **条件通过** — 4 项 Critical 已修复，需用户确认 Passphrase 后部署  

---

## 一、审计检查清单

### 1. 重复下单

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 去重机制 | 🟡 存在但非持久化 | `_closed_positions_done` 集合 `(symbol, entry_price, contracts, date)` 去重，但重启丢失 |
| 历史重复证据 | 🔴 确认存在 | `strategy_performance.json` 中同一交易重复记录多达 5 次（XRP 26笔去重后实际可能更少） |
| 每日清理 | 🟢 正常 | 每日清空去重集合（line 1692） |
| 平仓检测多周期 | 🟢 正常 | 3周期连续检测防漏 |

**风险等级**: 🟡 Medium  
**建议**: 实盘阶段监控 `POSITION_CLOSE` 事件数量是否与交易所持仓变化一致。

---

### 2. 漏平仓

| 检查项 | 状态 | 详情 |
|--------|------|------|
| AI 退出审核 | 🔴 已移除 | 代码注释 "AI 出场审核已移除 —— 全部解析失败"，所有退出现在仅依赖规则引擎 |
| 规则引擎止损 | 🔴 过松 | 紧急熔断 -50%、标准止损 -25%超1h。**首小时亏损 24% 无任何保护** |
| AUTO_SL | 🟡 已收紧 | `.env.live` 中设为 -15% / 0.5h（模拟盘是 -30% / 1h） |
| 移动止损 | 🟢 正常 | trailing_sl.py 在价格有利移动 > 1.0x ATR 时激活 |
| TPSL 健康检查 | 🟢 正常 | 每 3 周期检查并补挂缺失的 SL/TP |
| 僵尸仓退出 | 🟢 正常 | 持仓 > 4h 且 ROI < 2% 自动平仓 |

**风险等级**: 🟡 Medium  
**已缓解**: AUTO_SL 阈值从 -30% 收紧到 -15%、min_hours 从 1h 降到 0.5h  
**残留风险**: 规则引擎 `_review_open_positions` 仍使用旧阈值（-50%/-25%），应同步收紧

---

### 3. 持仓同步错误

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 开仓时间 | 🔴 重启丢失 | `_position_open_times` 是内存字典，重启后被设为 30 分钟前 |
| TPSL 缓存 | 🟡 10min TTL | 沙箱信任缓存跨重启，但可能返回过期数据 |
| 平仓检测 | 🟢 正常 | 对比 `_prev_positions` 与当前持仓检测平仓 |
| 仓位状态文件 | 🟡 仅持久化快照 | `positions_state.json` 保存持仓但不保存开仓时间 |
| 余额缓存 | 🟢 30s TTL | 缓存时间短，风险可控 |

**风险等级**: 🟡 Medium  
**建议**: 实盘阶段避免频繁重启 bot。如需重启，手动验证所有持仓的 SL/TP。

---

### 4. API 异常处理

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 断路器 | 🟢 正常 | DeepSeek 连续失败 3 次 → 熔断本周期 |
| API 超时 | 🟢 正常 | `requests` 默认超时 20s |
| 余额查询降级 | 🟡 沙箱特有 | 沙箱失败返回 10000 模拟值；实盘会重新抛出异常 |
| 市场数据加载 | 🟢 5次重试 | 启动时最多重试 5 次 |
| 静默异常 | 🟡 已部分升级 | 3 个关键位置已从 DEBUG → WARNING，仍有 ~22 处未升级 |
| TPSL 复核 | 🟢 3层检测 | 缓存 → positions → open_orders |
| 紧急平仓 | 🟢 3次重试 | 失败后记录 FATAL 日志 |

**风险等级**: 🟡 Medium  
**已缓解**: contract_size/min_amount 获取失败和持仓查询失败已升级日志级别

---

### 5. 止损失效

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 初始 SL 挂单 | 🟢 正常 | 开仓时通过 pos-tpsl API 同步挂载 |
| TPSL 失败保护 | 🟢 正常 | TPSL 附着失败 → 紧急平仓，不留裸仓 |
| 精度回退 | 🟢 **已修复** | 从 `round(price,2)` 改为从 market 对象查询精度 |
| SL 方向验证 | 🟢 正常 | 开仓后验证 SL/TP 方向相对于 mark price |
| 系统断连保护 | 🔴 缺失 | 无应用层断连保护，TPSL 依赖交易所服务器执行 |
| 交易所拒绝 SL | 🟡 可能 | 如果精度或价格不合理，交易所可能静默拒绝 |

**风险等级**: 🟢 Low（已修复关键问题）  
**注意**: 50 USDT 极小资金下，部分币种的合约面值可能导致止损距离不足 1 个 tick，应监控。

---

### 6. 资金计算错误

| 检查项 | 状态 | 详情 |
|--------|------|------|
| 仓位价值计算 | 🟢 正常 | `position_value = margin * leverage` |
| 合约数 floor | 🟡 边界情况 | `math.floor(amount_contracts)` 可能将小值归零，有回退保护 |
| 50 USDT 下 BTC | 🔴 可能拒绝 | BTC 最小 1 张合约 × 100k × 0.001 = 100 USDT → 超出 50 总资金 |
| 50 USDT 下 ETH | 🟡 临界 | ETH 最小 1 张合约 × 2000 × 0.01 = 20 USDT → 可能可行 |
| 50 USDT 下 山寨币 | 🟢 正常 | DOGE/DOT/LINK 等低单价币种 1 张合约通常 1-10 USDT |
| 手续费計算 | 🟢 正常 | taker_fee × position_value，假设最坏情况 |

**风险等级**: 🟡 Medium  
**已缓解**: 新增 `min_notional` 检查，低于交易所最小下单额的交易会被拒绝  
**注意**: 50 USDT 可能只能在低单价币种（DOGE, DOT, ADA, XRP, LINK）上交易

---

### 7. 最小下单量和精度

| 检查项 | 状态 | 详情 |
|--------|------|------|
| min_amount | 🟢 已升级日志 | 获取失败时 WARNING + 回退到 1.0 |
| min_notional | 🟢 **新增** | `get_min_notional()` 查询交易所最小下单额 |
| price_to_precision | 🟢 **已修复** | 回退时从 market.precision.price 计算小数位数 |
| contract_size | 🟢 已升级日志 | 获取失败时 WARNING + 回退到 1.0 |

**风险等级**: 🟢 Low（已全部修复）

---

## 二、实盘配置审查

### .env.live 关键参数

| 参数 | 值 | 审查 |
|------|-----|------|
| INITIAL_EQUITY | 50 USDT | ✅ |
| MAX_CONCURRENT_POSITIONS | 2 | ✅ |
| RISK_PER_TRADE_PCT | 0.02 (2%) | ✅ 单笔风险 = 1 USDT |
| MAX_DAILY_LOSS_PCT | 0.03 (3%) | ✅ 日亏 1.5 USDT 触发 |
| AUTO_SL_ROI_THRESHOLD | -0.15 (-15%) | ✅ 已收紧 |
| TP_ATR_MULTS | 2.0,3.0 | ✅ 已恢复正常 |
| MIN_POSITION_VALUE | 5 USDT | ✅ 适配 50 资金 |
| GRID_ENABLED | false | ✅ 关闭网格 |
| SELF_LEARNER_ENABLED | false | ✅ 关闭自学习 |
| PORTFOLIO_MANAGER_ENABLED | false | ✅ 关闭组合管理 |
| BITGET_SANDBOX | false | ✅ 实盘模式 |

### 部署前必须确认

| 项目 | 状态 |
|------|------|
| BITGET_PASSPHRASE | ⚠️ `.env.live` 中为 `CHANGE_ME`，需用户填入 |
| API 密钥权限 | ⚠️ 需确认实盘 API 有交易权限（非只读） |
| 服务器部署 | ⚠️ 需修改 deploy.sh 使用 `.env.live` 替换 `.env` |

---

## 三、残留风险（未修复但可接受）

| 风险 | 等级 | 原因 |
|------|------|------|
| 规则引擎止损阈值仍为 -50%/-25% | Medium | 已有初始 SL+AUTO_SL -15% 双重保护，规则引擎只作兜底 |
| ~22 处静默异常仍未升级 | Low | 非关键路径，不影响交易执行 |
| 开仓时间重启丢失 | Low | 实盘计划持续运行，不频繁重启 |
| 系统断连无应用层保护 | Low | Bitget TPSL 在服务端执行，不依赖 bot 连接 |
| 每日损失锁使用系统时间 | Low | 实盘运行在服务器上，时钟稳定 |

---

## 四、审计结论

### 🟡 条件通过 — 可以部署，但需先完成：

1. ✅ **用户确认实盘 Passphrase**（`.env.live` 中 `CHANGE_ME`）
2. ✅ **确认实盘 API 有交易权限**（非只读 key）
3. ⚠️ **建议同步收紧规则引擎止损**（`_review_open_positions` 中 -50% → -20%, -25% → -12%）
4. ✅ **删除 runtime_params.json** 已完成
5. ✅ **`.env.live` 已排除出 git** 已完成

### 模拟盘教训回顾

- XRP 26胜0负 → 实盘应优先分配给趋势明确的币种
- DOT 12连败 → 单币种连续亏损应触发暂停
- 多头 0%胜率 → Direction Block 仍可能允许做多，需监控
- 平均亏损是平均盈利的 1.9x → 止损收紧是关键

---

## 五、部署后监控清单

部署后请在第一个小时内逐项检查：

- [ ] bot 启动无异常（日志无 ERROR）
- [ ] 交易所连接成功（`📊 当前持仓` 日志出现）
- [ ] 第一轮扫描完成（`🔄 第 1 轮扫描`）
- [ ] 如有开仓，验证 SL/TP 已挂载
- [ ] 验证 dashboard.py 显示正确（50 USDT 初始权益）
- [ ] 验证手续费统计递增
- [ ] 检查 `health.json` 状态为 healthy
- [ ] 运行 `check_balance.py` 对账
