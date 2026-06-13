# 下一步计划

## 短期 (1-2 周)

1. **实盘验证 v4.5 过滤器架构** — 观察信号量是否恢复、胜率是否改善，重点监控 `signal_scorer.py` 新参数实际表现
2. **方向失衡修复** — 当前做多 3 / 做空 0，需恢复空头信号通路，检查 `DirectionSwitch` 状态和 Kalman 冲突率
3. **Freqtrade Hyperopt 重新运行** — 基于 v4.5 参数在 `E:\freqtrade\` 回测验证，对比基线结果

## 中期 (1-2 个月)

4. **Walk-forward 回测框架** — 滚动窗口验证，避免过拟合，参考 `genetic_evolver.py` 现有反事实回测
5. **BNB/DOGE/XRP 止损优化** — 当前 BNB 亏损严重，需检查 `trailing_sl.py` 止损距离和 ATR 倍数
6. **沙箱到实盘迁移** — 确认沙箱表现稳定后，`.env` 中 `BITGET_SANDBOX=false` 切换

## 长期

7. **多交易所支持** — 抽象 `ExchangeInterface`，接入 Binance/Bybit
8. **强化学习仓位管理** — 替代当前 ADX 动态仓位，基于市场状态自适应 Kelly

> 相关文件: `deepseek_quant_bot.py`, `signal_scorer.py`, `genetic_evolver.py`, `trailing_sl.py`, `.env`
