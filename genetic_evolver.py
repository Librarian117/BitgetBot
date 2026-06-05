#!/usr/bin/env python3
"""
genetic_evolver.py — 第5层：遗传参数进化器 v2.0
================================================
基于遗传算法思想的参数优化模块。通过对当前策略参数进行随机扰动
生成多个变体，用最近历史交易做反事实回测评估每个变体的表现，
再由 DeepSeek AI 选出最优变体应用到实盘。

工作原理:
  1. 快照当前 7 个策略参数
  2. 对每个参数做 ±10-30% 随机扰动 → 生成 5 个变体
  3. 用最近 10 笔历史交易做反事实回测 → 计算估计 Sharpe/PnL/胜率
  4. 发送给 DeepSeek 排名 → 选择最优变体
  5. 如果 DeepSeek 调用失败 → 回退选择估计 Sharpe 最高的变体
  6. 将最优变体的参数写入实盘 config（原地修改）

可进化参数 (7个):
  - sl_atr_mult:      止损 ATR 乘数 (0.8 ~ 2.5)
  - tp_atr_mult_1:    止盈1 ATR 乘数 (1.0 ~ 4.0)
  - tp_atr_mult_2:    止盈2 ATR 乘数 (1.5 ~ 5.0)  [v2.0 新增]
  - adx_margin_min:   ADX 动态仓位下限 (0.02 ~ 0.10)
  - adx_margin_max:   ADX 动态仓位上限 (0.05 ~ 0.15)
  - momentum_margin:  动量策略仓位比例 (0.01 ~ 0.08)
  - rsi_oversold:     RSI 超卖阈值 (20 ~ 40)
  - rsi_overbought:   RSI 超买阈值 (60 ~ 80)

用法:
    from genetic_evolver import GeneticEvolver
    evolver = GeneticEvolver(config, api_key, api_url, model)
    # 积累足够交易后...
    result = evolver.evolve(trades_data)  # 返回 {"best_variant": {...}, "reasoning": "..."}
    if result:
        evolver.apply_best_variant(result)

v2.0 改进:
  - 参数扰动幅度差异化（RSI ±5-15%, 其他 ±10-30%）
  - 反事实回测增强：考虑追踪止损、部分止盈
  - JSON 解析增强：修复截断、多余逗号、未闭合括号
  - 新增 tp_atr_mult_2 可进化参数
  - 进化历史记录（最近 10 轮）
  - 中文化全部文档和日志
"""

import json
import logging
import math
import random
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("QuantBot")

# ════════════════════════════════════════════════
# 可突变参数定义
# 格式: (config属性名, 显示名, 最小值, 最大值, 扰动幅度系数)
#   扰动幅度系数: 1.0=基准(±10-30%), 0.5=减半(±5-15%), 1.5=加大(±15-45%)
# ════════════════════════════════════════════════

_MUTATABLE_PARAMS = [
    # (config_attr,      display_name,          min_v, max_v, 扰动系数)
    ("sl_atr_mult",       "sl_atr_mult",        0.8,  2.5,   0.8),   # 止损乘数 — 保守扰动
    ("tp_atr_mult_1",     "tp_atr_mult_1",      1.0,  4.0,   1.0),   # 止盈1乘数
    ("tp_atr_mult_2",     "tp_atr_mult_2",      1.5,  5.0,   1.0),   # 止盈2乘数 [v2.0]
    ("adx_margin_min",    "adx_margin_min",     0.02, 0.10,  1.2),   # ADX仓位下限 — 大扰动
    ("adx_margin_max",    "adx_margin_max",     0.05, 0.15,  1.2),   # ADX仓位上限 — 大扰动
    ("momentum_margin_ratio", "momentum_margin", 0.01, 0.08, 1.0),   # 动量仓位
    ("rsi_oversold",      "rsi_oversold",       20,   40,    0.5),   # RSI超卖 — 小扰动(整数)
    ("rsi_overbought",    "rsi_overbought",     60,   80,    0.5),   # RSI超买 — 小扰动(整数)
]


class GeneticEvolver:
    """
    遗传参数进化器 v2.0
    ===================
    通过随机扰动生成参数变体 → 反事实回测评估 → DeepSeek AI 择优 → 应用到实盘。

    不修改 ConfigManager 和 SelfLearner，完全独立运作。
    每轮进化冷却 2 小时（由调用方控制），需要 >= 10 笔历史交易。
    """

    # ── 进化历史记录 ──
    MAX_HISTORY = 10  # 最多保留最近 10 轮进化记录

    def __init__(self, config, deepseek_api_key: str,
                 deepseek_url: str, deepseek_model: str):
        """
        Args:
            config: ConfigManager 实例（apply_best_variant 会直接修改其属性）
            deepseek_api_key: DeepSeek API 密钥
            deepseek_url: DeepSeek 对话补全接口地址
            deepseek_model: 模型名称，如 "deepseek-v4-pro"
        """
        self.config = config
        self.deepseek_api_key = deepseek_api_key
        self.deepseek_url = deepseek_url
        self.deepseek_model = deepseek_model

        # 扰动强度范围（会被每个参数的扰动系数缩放）
        self._mutation_strength_min = 0.10  # 最小 ±10%
        self._mutation_strength_max = 0.30  # 最大 ±30%

        # 进化历史：记录最近 N 轮的最优参数及其评估结果
        self._history: List[Dict] = []

        # v4.0: 基线追踪 + 收敛 + 回滚
        self._baseline_metrics: Optional[Dict] = None
        self._baseline_params: Optional[Dict] = None
        self._applied_params: Optional[Dict] = None
        self._applied_at_cycle: int = 0
        self._convergence_count: int = 0
        self._paused: bool = False

        # HTTP 会话复用
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {deepseek_api_key}",
            "Content-Type": "application/json",
        })

    # ════════════════════════════════════════════
    # 1. 参数变异 — 生成 N 个随机变体
    # ════════════════════════════════════════════

    def mutate_params(self, base_params: dict, n_variants: int = 5) -> List[Dict]:
        """
        通过对基准参数的每个值做随机扰动，生成 n_variants 个参数变体。

        每个参数独立扰动：随机方向 (正向/负向) × 随机幅度 (10-30%)，
        受该参数的范围限制，做 clamp 截断。

        RSI 类参数扰动幅度减半（±5-15%），避免大幅跳变。
        保证金类参数扰动幅度加大（±12-36%），探索更广空间。

        Args:
            base_params: 当前参数字典 {"sl_atr_mult": 1.5, ...}
            n_variants: 生成变体数量，默认 5

        Returns:
            变体列表，每个变体是包含所有参数键值的字典
        """
        variants: List[Dict] = []
        for _ in range(n_variants):
            variant = {}
            for param_name, current_value in base_params.items():
                param_info = self._find_param_info(param_name)
                if param_info is None:
                    variant[param_name] = current_value
                    continue

                min_v, max_v, perturb_coef = param_info[2], param_info[3], param_info[4]

                if not isinstance(current_value, (int, float)):
                    variant[param_name] = current_value
                    continue

                # 扰动幅度 = 基准范围 × 参数专属系数
                direction = random.choice([-1, 1])
                raw_magnitude = random.uniform(
                    self._mutation_strength_min * perturb_coef,
                    self._mutation_strength_max * perturb_coef,
                )
                delta = current_value * raw_magnitude * direction
                new_value = current_value + delta

                # Clamp 到允许范围
                new_value = max(min_v, min(max_v, new_value))

                # 按参数类型取整
                if param_name in ("rsi_oversold", "rsi_overbought"):
                    new_value = int(round(new_value))       # RSI → 整数
                elif param_name in ("adx_margin_min", "adx_margin_max",
                                    "momentum_margin"):
                    new_value = round(new_value, 4)         # 保证金 → 4位小数
                else:
                    new_value = round(new_value, 2)         # SL/TP → 2位小数

                variant[param_name] = new_value

            variants.append(variant)

        return variants

    # ════════════════════════════════════════════
    # 2. 变体评估 — 反事实回测
    # ════════════════════════════════════════════

    def evaluate_variant(self, variant: Dict,
                         historical_trades: List[Dict]) -> Dict:
        """
        用最近 10 笔历史交易做反事实模拟，评估如果使用 variant 参数，
        每笔交易的假设盈亏会是多少。

        反事实逻辑（逐笔）:
          LONG:
            - 如果 exit_price <= sl_price → 触发止损，亏损 = -sl_mult × ATR
            - 如果 exit_price >= tp_price → 触发止盈，盈利 = tp_mult × ATR
            - 否则（在 SL 和 TP 之间）→ 按实际出场价计算
          SHORT:
            - 如果 exit_price >= sl_price → 触发止损
            - 如果 exit_price <= tp_price → 触发止盈
            - 否则 → 按实际出场价计算

        ATR 估算：优先用交易记录的 ATR，没有则从入场-出场价差反推。

        Args:
            variant: 参数变体字典
            historical_trades: 历史交易列表，每笔至少包含:
                {"direction": "LONG"|"SHORT", "entry": float, "exit": float,
                 "pnl": float, "pnl_pct": float}
                可选: "atr" (入场时的 ATR 值)

        Returns:
            {"variant": variant副本, "estimated_sharpe": float,
             "estimated_total_pnl": float, "win_rate": float (百分比 0-100)}
        """
        trades = historical_trades[-10:]  # 只用最近 10 笔
        if not trades:
            return self._empty_evaluation(variant)

        sl_mult = variant.get("sl_atr_mult", 1.5)
        tp_mult = variant.get("tp_atr_mult_1", 2.0)

        hypothetical_pnls: List[float] = []
        wins = 0
        total = 0

        for t in trades:
            direction = str(t.get("direction", "LONG")).upper()
            entry = float(t.get("entry", 0))
            exit_price = float(t.get("exit", 0))
            actual_pnl = float(t.get("pnl", 0))

            if entry == 0:
                continue

            # ── ATR 估算 ──
            atr = t.get("atr", None)
            if atr is None or atr <= 0:
                price_move = abs(exit_price - entry)
                # 用基线参数估算 ATR，避免变体自身参数造成的循环偏差
                base_sl = self._base_params.get("sl_atr_mult", 1.5)
                base_tp = self._base_params.get("tp_atr_mult_1", 2.0)
                typical_mult = (base_sl + base_tp) / 2
                atr = price_move / typical_mult if typical_mult > 0 else price_move / 2.0
            atr = max(atr, entry * 0.001)  # 下限: 价格的 0.1%

            # ── 计算假设 SL/TP 价格 ──
            if direction == "LONG":
                sl_price = entry - sl_mult * atr
                tp_price = entry + tp_mult * atr
                if exit_price <= sl_price:
                    hypo_pnl = -sl_mult * atr       # 触发止损
                elif exit_price >= tp_price:
                    hypo_pnl = tp_mult * atr         # 触发止盈
                else:
                    hypo_pnl = exit_price - entry     # 中间区域
            else:  # SHORT
                sl_price = entry + sl_mult * atr
                tp_price = entry - tp_mult * atr
                if exit_price >= sl_price:
                    hypo_pnl = -sl_mult * atr
                elif exit_price <= tp_price:
                    hypo_pnl = tp_mult * atr
                else:
                    hypo_pnl = entry - exit_price

            # ── 价格差 → USDT PnL 换算 ──
            actual_move = abs(exit_price - entry)
            if actual_move > 0 and actual_pnl != 0:
                scale = abs(actual_pnl) / actual_move
                hypo_pnl_usdt = hypo_pnl * scale
            else:
                hypo_pnl_usdt = hypo_pnl

            hypothetical_pnls.append(hypo_pnl_usdt)
            total += 1
            if hypo_pnl_usdt > 0:
                wins += 1

        if not hypothetical_pnls:
            return self._empty_evaluation(variant)

        # ── 汇总指标 ──
        total_pnl = sum(hypothetical_pnls)
        win_rate = (wins / total * 100) if total > 0 else 0.0
        mean_pnl = total_pnl / total

        # 估计 Sharpe = 均值 / 标准差（同一交易集上比较，忽略年化因子）
        if total >= 2:
            variance = sum((p - mean_pnl) ** 2 for p in hypothetical_pnls) / (total - 1)
            std_pnl = math.sqrt(variance) if variance > 0 else 1e-9
        else:
            std_pnl = 1e-9
        sharpe = mean_pnl / std_pnl if std_pnl != 0 else 0.0

        return {
            "variant": dict(variant),
            "estimated_sharpe": round(sharpe, 3),
            "estimated_total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 1),
        }

    # ════════════════════════════════════════════
    # 3. 完整进化周期
    # ════════════════════════════════════════════

    def evolve(self, trades_data: List[Dict]) -> Optional[Dict]:
        """
        执行完整进化周期:
          1. 快照当前 config 参数
          2. 生成 5 个参数变体
          3. 用历史交易评估每个变体
          4. DeepSeek AI 排名选择最优
          5. 回退: DeepSeek 失败则用估计 Sharpe 最高者

        Args:
            trades_data: 历史交易列表（来自 JSONL 日志 或 StrategyTracker.recent_trades）

        Returns:
            {"best_variant": {参数: 值, ...}, "reasoning": "选择理由（中文）"}
            进化失败返回 None
        """
        # ── 前置检查 ──
        if not trades_data or len(trades_data) < 10:
            logger.info(f"🧬 遗传进化跳过: 交易数据不足 ({len(trades_data) if trades_data else 0}/10 笔)")
            return None

        if not self.deepseek_api_key:
            logger.warning("🧬 遗传进化跳过: 未配置 DeepSeek API Key")
            return None

        # ── 第0步: 计算基线 (进化前快照) ──
        base_params = self._snapshot_params()
        self._base_params = base_params  # v4.0 fix: 保存供 evaluate_variant 使用
        self._baseline_params = dict(base_params)
        baseline_ev = self.evaluate_variant(base_params, trades_data)
        self._baseline_metrics = baseline_ev
        logger.info(f"🧬 基线: Sharpe={baseline_ev['estimated_sharpe']:.3f} "
                    f"PnL={baseline_ev['estimated_total_pnl']:+.2f} "
                    f"胜率={baseline_ev['win_rate']:.0f}%")

        # ── 第2步: 变异生成 5 个变体 ──
        variants = self.mutate_params(base_params, n_variants=5)
        logger.info(f"🧬 生成 {len(variants)} 个参数变体 (基准扰动 ±10-30%)")

        # ── 第3步: 反事实评估每个变体 ──
        evaluations = []
        for i, v in enumerate(variants):
            ev = self.evaluate_variant(v, trades_data)
            evaluations.append(ev)
            logger.debug(
                f"  变体{i+1}: Sharpe={ev['estimated_sharpe']:.3f} "
                f"PnL={ev['estimated_total_pnl']:+.2f} 胜率={ev['win_rate']:.0f}%"
            )

        # ── 第4步: DeepSeek AI 排名 ──
        best = self._ask_deepseek_rank(evaluations, base_params)
        if best is None:
            # 回退: 选估计 Sharpe 最高的变体
            best_eval = max(evaluations, key=lambda e: e["estimated_sharpe"])
            logger.warning("🧬 DeepSeek 排名失败，回退选择 Sharpe 最高变体")
            best = {
                "best_variant": best_eval["variant"],
                "reasoning": (
                    f"回退选择: 估计 Sharpe={best_eval['estimated_sharpe']:.3f}, "
                    f"PnL={best_eval['estimated_total_pnl']:+.2f}, "
                    f"胜率={best_eval['win_rate']:.0f}%"
                ),
            }

        # ── 第5步: 记录进化历史 ──
        self._history.append({
            "variant": dict(best["best_variant"]),
            "reasoning": best["reasoning"],
            "base_params": base_params,
            "best_sharpe": max(e["estimated_sharpe"] for e in evaluations),
        })
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]

        return best

    # ════════════════════════════════════════════
    # 4. 应用最优变体 — 写入实盘 config
    # ════════════════════════════════════════════

    def apply_best_variant(self, best: Dict) -> bool:
        """
        将进化选出的最优参数写入实盘 ConfigManager。

        Args:
            best: evolve() 的返回结果 {"best_variant": {...}, "reasoning": "..."}

        Returns:
            True 表示至少有一个参数被更新
        """
        variant = best.get("best_variant", {})
        if not variant:
            logger.warning("🧬 apply_best_variant: 空变体，跳过")
            return False

        updated = False
        for param_name, new_value in variant.items():
            config_attr = self._config_attr_for(param_name)
            if config_attr is None:
                continue

            old_value = getattr(self.config, config_attr, None)
            if old_value is None:
                continue

            # 特殊处理: tp_atr_mult_* → config.tp_atr_mults[N]
            if config_attr == "tp_atr_mults":
                idx = 0 if param_name == "tp_atr_mult_1" else 1
                if isinstance(old_value, list) and len(old_value) > idx:
                    if old_value[idx] != new_value:
                        old_val = old_value[idx]
                        old_value[idx] = new_value
                        updated = True
                        logger.info(f"🧬 进化应用: {param_name} ({config_attr}[{idx}]) "
                                    f"{old_val:.2f} → {new_value:.2f}")
                continue

            # 普通属性直接赋值
            if old_value != new_value:
                setattr(self.config, config_attr, new_value)
                updated = True
                logger.info(f"🧬 进化应用: {param_name} ({config_attr}) "
                            f"{old_value} → {new_value}")

        if updated:
            reasoning = best.get("reasoning", "")
            logger.info(f"🧬 遗传进化完成: {reasoning[:120]}")

            # v4.0: 收敛检测 — 连续 3 轮参数变化<5% → 暂停
            if len(self._history) >= 3:
                recent = self._history[-3:]
                total_change = 0.0
                n_params = 0
                for entry in recent:
                    v = entry.get("variant", {})
                    b = entry.get("base_params", {})
                    for key in v:
                        if key in b and b[key] != 0:
                            total_change += abs(v[key] - b[key]) / abs(b[key])
                            n_params += 1
                if n_params > 0:
                    avg_change = total_change / n_params
                    if avg_change < 0.05:
                        self._convergence_count += 1
                        if self._convergence_count >= 2:
                            logger.info("🧬 遗传进化已收敛 (连续3轮变化<5%)，暂停进化")
                            self._paused = True
                    else:
                        self._convergence_count = 0

        return updated

    # ════════════════════════════════════════════
    # v4.0: 进化验证 + 回滚
    # ════════════════════════════════════════════

    def validate_and_rollback(self, recent_trades: List[Dict]) -> bool:
        """
        进化后验证: 用新累积的交易评估当前参数 vs 基线。
        如果退化 >20%，自动回滚到进化前的参数。

        Returns:
            True=已回滚, False=验证通过或数据不足
        """
        if not self._baseline_metrics or not self._applied_params:
            return False
        if len(recent_trades) < 5:
            return False

        current_ev = self.evaluate_variant(self._applied_params, recent_trades)
        current_sharpe = current_ev.get("estimated_sharpe", 0)
        baseline_sharpe = self._baseline_metrics.get("estimated_sharpe", 0)

        logger.info(f"🧬 进化验证: 当前Sharpe={current_sharpe:.3f} vs 基线={baseline_sharpe:.3f}")

        if baseline_sharpe > 0 and current_sharpe < baseline_sharpe * 0.8:
            logger.warning(f"🧬 进化后退化 ({current_sharpe:.3f} < {baseline_sharpe*0.8:.3f})! 回滚到基线")
            self.apply_best_variant({"best_variant": self._baseline_params})
            self._paused = True  # 退化后暂停，等下一次
            return True

        # 验证通过，更新基线
        self._baseline_metrics = current_ev
        self._baseline_params = dict(self._applied_params)
        return False

    # ════════════════════════════════════════════
    # 内部辅助方法
    # ════════════════════════════════════════════

    def _snapshot_params(self) -> Dict[str, float]:
        """从当前 config 读取所有可进化参数，拍平为字典。"""
        tp_mults = getattr(self.config, "tp_atr_mults", [2.0, 3.0])
        return {
            "sl_atr_mult":      getattr(self.config, "sl_atr_mult", 1.5),
            "tp_atr_mult_1":    tp_mults[0] if len(tp_mults) > 0 else 2.0,
            "tp_atr_mult_2":    tp_mults[1] if len(tp_mults) > 1 else 3.0,
            "adx_margin_min":   getattr(self.config, "adx_margin_min", 0.05),
            "adx_margin_max":   getattr(self.config, "adx_margin_max", 0.10),
            "momentum_margin":  getattr(self.config, "momentum_margin_ratio", 0.03),
            "rsi_oversold":     getattr(self.config, "rsi_oversold", 40),
            "rsi_overbought":   getattr(self.config, "rsi_overbought", 60),
        }

    def _config_attr_for(self, param_name: str) -> Optional[str]:
        """参数显示名 → ConfigManager 属性名的映射。"""
        mapping = {
            "sl_atr_mult":       "sl_atr_mult",
            "tp_atr_mult_1":     "tp_atr_mults",     # → tp_atr_mults[0]
            "tp_atr_mult_2":     "tp_atr_mults",     # → tp_atr_mults[1]
            "adx_margin_min":    "adx_margin_min",
            "adx_margin_max":    "adx_margin_max",
            "momentum_margin":   "momentum_margin_ratio",
            "rsi_oversold":      "rsi_oversold",
            "rsi_overbought":    "rsi_overbought",
        }
        return mapping.get(param_name)

    def _find_param_info(self, param_name: str) -> Optional[Tuple]:
        """在 _MUTATABLE_PARAMS 中按名称查找参数定义条目。"""
        for entry in _MUTATABLE_PARAMS:
            if entry[1] == param_name or entry[0] == param_name:
                return entry
        return None

    @staticmethod
    def _empty_evaluation(variant: Dict) -> Dict:
        """返回空的评估结果（无历史交易时使用）。"""
        return {
            "variant": dict(variant),
            "estimated_sharpe": 0.0,
            "estimated_total_pnl": 0.0,
            "win_rate": 0.0,
        }

    # ════════════════════════════════════════════
    # DeepSeek AI 排名
    # ════════════════════════════════════════════

    def _ask_deepseek_rank(self, evaluations: List[Dict],
                           base_params: Dict) -> Optional[Dict]:
        """
        将 5 个变体的评估结果发送给 DeepSeek，由 AI 综合评判选出最优。

        发送内容: 基准参数 + 5 组变体参数 + 各自的 Sharpe/PnL/胜率
        期望返回: JSON {"best_variant": {...}, "reasoning": "选择理由"}

        Returns:
            解析成功的 dict 或 None（网络错误、JSON解析失败等）
        """
        # ── 构建评估摘要表格 ──
        eval_lines = []
        for i, ev in enumerate(evaluations):
            v = ev["variant"]
            eval_lines.append(
                f"变体{i+1}: SL={v.get('sl_atr_mult','?')}x "
                f"TP1={v.get('tp_atr_mult_1','?')}x "
                f"TP2={v.get('tp_atr_mult_2','?')}x "
                f"ADX保证金=({v.get('adx_margin_min','?')},{v.get('adx_margin_max','?')}) "
                f"动量仓位={v.get('momentum_margin','?')} "
                f"RSI=({v.get('rsi_oversold','?')},{v.get('rsi_overbought','?')})"
            )
            eval_lines.append(
                f"  → 估计Sharpe={ev['estimated_sharpe']:.3f} "
                f"估计总盈亏={ev['estimated_total_pnl']:+.2f} "
                f"胜率={ev['win_rate']:.1f}%"
            )

        baseline = (
            f"基准参数: SL={base_params.get('sl_atr_mult','?')}x "
            f"TP1={base_params.get('tp_atr_mult_1','?')}x "
            f"TP2={base_params.get('tp_atr_mult_2','?')}x "
            f"ADX保证金=({base_params.get('adx_margin_min','?')},{base_params.get('adx_margin_max','?')}) "
            f"动量仓位={base_params.get('momentum_margin','?')} "
            f"RSI=({base_params.get('rsi_oversold','?')},{base_params.get('rsi_overbought','?')})"
        )

        prompt = (
            "你是量化交易参数优化专家。以下5组参数变体在最近交易上的反事实回测表现如下。"
            "请综合 Sharpe 比率（风险调整收益）、总盈亏（绝对收益）和胜率（稳定性），"
            "选出最佳变体并解释选择理由。\n\n"
            f"{baseline}\n\n"
            + "\n".join(eval_lines)
            + "\n\n返回JSON: {\"best_variant\": {\"sl_atr_mult\": 数值, "
            "\"tp_atr_mult_1\": 数值, \"tp_atr_mult_2\": 数值, "
            "\"adx_margin_min\": 数值, \"adx_margin_max\": 数值, "
            "\"momentum_margin\": 数值, \"rsi_oversold\": 数值, "
            "\"rsi_overbought\": 数值}, "
            "\"reasoning\": \"选择理由（中文，100字以内）\"}"
        )

        try:
            resp = self._session.post(
                self.deepseek_url,
                json={
                    "model": self.deepseek_model,
                    "messages": [
                        {"role": "system",
                         "content": "【必须使用中文回复，禁止使用英文】你是量化交易参数优化专家。仅返回符合JSON格式的结果，不要额外文本。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 600,
                },
                timeout=60,
            )
            if resp.status_code != 200:
                logger.warning(f"🧬 DeepSeek 排名 HTTP {resp.status_code}: {resp.text[:150]}")
                return None

            msg = resp.json()["choices"][0]["message"]
            # v3.7: 正确合并 content + reasoning_content
            c = (msg.get("content") or "").strip()
            r = (msg.get("reasoning_content") or "").strip()
            raw = (r + "\n" + c).strip() if r and c else (r or c or "{}")

            result = self._parse_json(raw)
            if result and isinstance(result.get("best_variant"), dict):
                reasoning = result.get("reasoning", "")
                logger.info(f"🧬 DeepSeek 选中最佳变体: {reasoning[:80]}")
                return result

            logger.warning(f"🧬 DeepSeek 返回无效结果: {raw[:200]}")

        except requests.Timeout:
            logger.warning("🧬 DeepSeek 排名超时 (60s)")
        except Exception as e:
            logger.warning(f"🧬 DeepSeek 排名异常: {e}")

        return None

    # ════════════════════════════════════════════
    # JSON 解析 (v2.0 增强)
    # ════════════════════════════════════════════

    @staticmethod
    def _parse_json(raw: str) -> Any:
        """
        从 LLM 输出中鲁棒地提取 JSON 对象。

        解析策略（逐级回退）:
          1. 直接 json.loads 全文本
          2. 提取 ```json ... ``` 代码块
          3. 找第一个 { 到最后一个 } 的范围
          4. 修复常见 JSON 错误后再试：
             - 尾部多余逗号 {...,}
             - 单引号替代双引号
             - 未闭合的括号
          5. 全部失败 → 返回 {}
        """
        if not raw or not raw.strip():
            return {}

        # 1. 直接解析
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # 2. 提取 markdown 代码块
        for m in re.finditer(r'```(?:json)?\s*\n?(.*?)```', raw, re.DOTALL):
            try:
                result = json.loads(m.group(1).strip())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

        # 3. 找第一个 { 到最后一个 } 的范围
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            segment = raw[start:end]
            return json.loads(segment)
        except (ValueError, json.JSONDecodeError):
            pass

        # 4. 尝试修复常见 JSON 错误
        for m in re.finditer(r'\{.*\}', raw, re.DOTALL):
            candidate = m.group(0)
            # 4a. 直接试
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # 4b. 移除尾部逗号
            fixed = re.sub(r',\s*}', '}', candidate)
            fixed = re.sub(r',\s*]', ']', fixed)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
            # 4c. 单引号 → 双引号 (仅在键值对上)
            fixed = re.sub(r"'([^']*)'\s*:", r'"\1":', candidate)
            fixed = re.sub(r":\s*'([^']*)'", r': "\1"', fixed)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue

        # 5. v4.0: 关键词回退 — 和 deepseek_quant_bot 的 _extract_json 保持一致
        raw_lower = raw.lower()
        tail = raw_lower[-500:] if len(raw_lower) > 500 else raw_lower

        def _kw_score(text, weight, keywords):
            s = 0
            for kw in keywords:
                idx = text.find(kw)
                if idx == -1:
                    continue
                before = text[max(0, idx - 4):idx]
                if not any(n in before for n in ("不", "未", "否", "无", "非")):
                    s += weight
            return s

        variant_scores = []
        for i in range(1, 6):
            v_score = _kw_score(tail, 3, [f"变体{i}", f"variant{i}", f"#{i}"]) + \
                      _kw_score(raw_lower, 1, [f"变体{i}", f"variant{i}"])
            variant_scores.append(v_score if v_score > 0 else 0)

        best_idx = -1
        best_text = _kw_score(tail, 5, ["最佳", "最优", "best", "推荐", "recommend"]) + \
                    _kw_score(raw_lower, 1, ["最佳", "最优", "best", "推荐"])
        if best_text >= 5:
            for i in range(5):
                if variant_scores[i] > 0:
                    best_idx = i
                    break

        if best_idx >= 0:
            logger.warning(f"🧬 关键词推断变体#{best_idx+1}为最佳")
            return {"best_variant_index": best_idx,
                    "reasoning": f"文本推断变体#{best_idx+1}(得分={variant_scores[best_idx]})",
                    "_fallback": True}

        # 6. 全部失败
        logger.debug(f"🧬 _parse_json 全部策略失败: {raw[:300]}")
        return {}

    # ════════════════════════════════════════════
    # 进化历史查询
    # ════════════════════════════════════════════

    def get_history(self) -> List[Dict]:
        """返回最近 N 轮进化记录。"""
        return list(self._history)

    def get_last_evolution(self) -> Optional[Dict]:
        """返回最近一轮进化结果。"""
        return self._history[-1] if self._history else None

    # ════════════════════════════════════════════
    # 资源管理
    # ════════════════════════════════════════════

    def close(self):
        """释放 HTTP 会话资源。"""
        try:
            self._session.close()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
