# DeepSeekQuantBot v4.5 → Phase 2 实盘 Canary

Bitget **实盘** U本位合约 | 每5分钟扫描 | 量化决策 + AI研究层 | 6策略 | 10层风控  
**初始资金: 50 USDT** | 最大持仓: 2 | 目标: 验证执行链路（非盈利）

**阅读路径**: `docs/README.md` → `docs/architecture/overview.md` → `docs/design/strategy-design.md` → `AGENTS.md`  
**封存**: `archive/v4.5_phase1_20260613/` (模拟盘终态 -22.23%)  
**审计**: `PRE_LIVE_AUDIT.md` (部署前必读)

## 规则

- 全程中文 | 优先模块化/异步/风控 | 禁止未来函数 | 类型提示+dataclass+完整异常
- 每个信号证据只消费一次 (v4.5 核心原则)
- AI 不是决策者 — `DEEPSEEK_MASTER_SWITCH` 总开关控制
- 实盘 Canary: 验证执行链路，不追求收益

## ⚠️ 实盘 Canary 参数冻结 (至 2026-06-20)

以下参数 **禁止修改**，仅允许 Bug Fix / 日志增强 / 稳定性修复：

| 参数 | 冻结值 | 位置 |
|------|--------|------|
| RSI_PERIOD | 14 | `config_manager.py:65` |
| RSI_OVERSOLD | 40 | `config_manager.py:69` |
| RSI_OVERBOUGHT | 60 | `config_manager.py:70` |
| ADX_THRESHOLD | 16 | `config_manager.py:71` |
| KELLY_MULTIPLIER | 1.0 | `config_manager.py:200` (`.env.live` 默认) |
| Pullback 入场逻辑 | — | `deepseek_quant_bot.py:1007-1020` |
| Direction Block | — | `deepseek_quant_bot.py:1129-1171` |
| Kalman 滤波 | — | `indicator_calculator.py` kalman 函数 |
| Confidence 评分 | — | `deepseek_quant_bot.py:1191-1306` |

## 启动/部署

```bash
# 本地（沙箱/实盘取决于 .env 中 BITGET_SANDBOX）
python deepseek_quant_bot.py

# 实盘（使用 .env.live）
cp .env.live .env && python deepseek_quant_bot.py

# 部署 (Git → Push → Server)
git push origin main
ssh root@8.210.3.197 "cd /root/BitgetBot && git pull && bash deploy.sh"
```

## 关键 Gotcha

- `fetch_open_orders(stop=True)` 查不到 pos-tpsl | Hedge 不支持 `reduceOnly`
- 1h/4h K线用 `iloc[-2]` | 沙箱量数据不可靠
- 实盘用 `BITGET_SANDBOX=false` 切换，API 密钥在 `.env.live`
- 50 USDT 极小资金：部分高价币种（BTC/ETH）可能因 min_notional 被拒绝
