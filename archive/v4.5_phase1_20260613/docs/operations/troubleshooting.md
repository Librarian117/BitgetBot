# 故障排除

## 常见问题

1. **Bot 无法启动**
   - 检查 `deploy.log` 部署日志
   - 确认 Python 版本 >= 3.10
   - 检查 `.env` 文件是否存在且 API 密钥有效

2. **无交易信号**
   - 检查 `.env` 中 `ADX_THRESHOLD` (当前仅影响 momentum 策略)
   - 检查 `RSI_OVERSOLD` / `RSI_OVERBOUGHT` 有效阈值
   - 查看 `health.json` 中的 `signal_flow_rate`

3. **AI 解析失败**
   - 确认 `DEEPSEEK_MODEL` 模型名称正确 (如 `deepseek-v4-pro`)
   - 检查 `DEEPSEEK_API_KEY` 是否有效
   - AI 解析失败时默认放行，不影响决策；查看 `last_raw_info` 诊断

4. **止损单未检测到**
   - pos-tpsl 不会出现在 `fetch_open_orders(stop=True)` 中
   - 使用 `_has_position_tpsl()` 双重检测: 先查 orders，再查 position.info

5. **方向失衡 (做多过多/做空过少)**
   - 检查 Direction Learner 统计: 滚动胜率 < 30% 会触发方向开关暂停
   - 检查 Kalman 方向冲突率是否过高

6. **日亏损锁触发**
   - 检查 `.env` 中 `MAX_DAILY_LOSS_PCT` 阈值 (默认 3%)
   - 日亏损锁在每日 00:00 (Asia/Shanghai) 自动重置

## 日志位置

| 日志 | 路径 |
|------|------|
| 运行时日志 | `/tmp/bot.log` |
| 交易记录 | `logs/trades_YYYY-MM-DD.jsonl` |
| 部署历史 | `deploy.log` |

## 健康检查

```bash
grep HEALTHY health.json         # 全链路健康状态
python dashboard.py              # 账户快照
python dashboard.py --loop       # 每 30 秒刷新
python audit_trades.py           # 离线交易审计
```

> 相关文件: `health_monitor.py`, `risk_monitor.py`, `deepseek_quant_bot.py`, `health.json`
