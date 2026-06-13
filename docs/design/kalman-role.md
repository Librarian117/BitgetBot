# Kalman 在 v4.5 中的角色定义

## 唯一职责：噪声过滤后的趋势方向估计

Kalman 滤波器在 v4.5 中只做一件事：对最近 50 根收盘价进行噪声过滤，输出一个平滑的方向估计。

- **输入**: 最近 50 根 K 线的收盘价序列
- **输出**: `kalman_dir` (up / down / flat) + `kalman_score` (0-1 置信度)
- **唯一消费者**: `_resolve_kalman()` — `deepseek_quant_bot.py:488`

## `_resolve_kalman()` 决策表

| 冲突类型 | 得分 | 动作 | 策略路由 | 扣分 | Scorer 标记 |
|---|---|---|---|---|---|
| Signal-Kalman | >0.8 | 降级 | counter_trend | score × 0.75 | True |
| Signal-Kalman | 0.5-0.8 | 降级 | counter_trend | score × 0.80 | True |
| Signal-Kalman | 0.2-0.5 | 扣分 | 不变 | score × 0.75 | False |
| Signal-Kalman | ≤0.2 | 放行 | 不变 | 0 | False |
| EMA-Kalman (pullback) | >0.5 | 降级 | counter_trend | ≤ 20 分 | True |
| EMA-Kalman (pullback) | ≤0.5 | 降级 | counter_trend | 0 | False |
| EMA-Kalman (非pullback) | >0.5 | 扣分 | 不变 | ≤ 20 分 | False |
| EMA-Kalman (非pullback) | ≤0.5 | 放行 | 不变 | 0 | False |

## v4.5 修复：从 4 件事到 1 个决策

**v4.4 问题**: Kalman 在 4 个分散位置做 4 件不同的事：
1. 修改策略类型
2. 扣除置信度分数
3. 提高 ADX 阈值门槛
4. 修改方向阻止块

**v4.5 修复**: 全部收敛到 `_resolve_kalman()` 单一决策点。一个函数，一张表，一种行为。

## 测试覆盖

`tests/test_kalman_resolve.py` — 119 个测试用例覆盖所有分支组合。
