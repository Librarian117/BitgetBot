#!/usr/bin/env python3
"""
markov_regime.py — 马可夫状态检测器 v1.0
==========================================
基于 3 状态马可夫链的市场状态检测。纯本地计算，零 API。

算法:
  1. 标签化: 滚动窗口内收益率 > +threshold → Bull
                                 < -threshold → Bear
                                 否则 → Sideways
  2. 转移矩阵: 从已标注序列统计 3×3 P(当前→下一步)
  3. 平稳分布: 转移矩阵的左特征向量 (长期均衡)
  4. 前向滚动: 每步只用历史数据重算 (无未来信息)
  5. 信号分数: P(Bull) - P(Bear) ∈ [-1, 1]

用途:
  - market_regime 增强: 与现有多 TF 趋势检测互补
  - 仓位过滤: 熊市概率 >40% → 减仓或不做多
  - 信号确认: signal > 0 才做多, signal < 0 才做空

用法:
  from markov_regime import MarkovRegime
  mr = MarkovRegime(window=20, threshold=0.03)
  result = mr.detect(prices)  # prices 是 close 价格序列
  # result["current_regime"] → "bull"/"bear"/"sideways"
  # result["signal"] → -1.0 ~ +1.0
  # result["next_probs"] → {"bull": 0.6, "sideways": 0.3, "bear": 0.1}
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("QuantBot")


class MarkovRegime:
    """3 状态马可夫状态检测器"""

    def __init__(self, window: int = 20, threshold: float = 0.03, min_train: int = 50):
        """
        Args:
            window: 滚动窗口大小 (用于计算收益率)
            threshold: 状态判定阈值, ±3% 默认
            min_train: 最小训练数据点数
        """
        self.window = window
        self.threshold = threshold
        self.min_train = min_train
        # 缓存: (symbol, len(prices)) → 结果
        self._cache: Dict[str, Dict] = {}
        # v4.0: 持久化路径
        self.STATE_FILE = "markov_state.json"

    # ════════════════════════════════════════════
    # 核心算法
    # ════════════════════════════════════════════

    def detect(self, prices: List[float], symbol: str = "") -> Dict[str, Any]:
        """
        对价格序列做马可夫状态检测。

        Args:
            prices: close 价格列表 (按时间升序)
            symbol: 可选标识符

        Returns:
            {
                "current_regime": "bull"/"bear"/"sideways",
                "signal": float in [-1, 1],   # + = 多头倾向
                "next_probs": {regime: probability},
                "transition_matrix": 3×3 list,
                "persistence": {regime: prob},  # 停留在当前状态的概率
                "stationary_dist": {regime: prob},
                "confidence": 0-100,
                "detail": str,
            }
        """
        if len(prices) < self.min_train:
            return self._empty_result()

        # v4.0: 缓存用价格区间代替 hash，避免 PYTHONHASHSEED 导致的缓存失效
        latest = prices[-1] if prices else 0
        price_bucket = int(latest / max(latest * 0.005, 0.01)) if latest > 0 else 0
        cache_key = f"{symbol}_{len(prices)}_{price_bucket}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # ── 1. 标签化 ──
        returns = self._compute_returns(prices)
        labels = self._label_states(returns)

        # ── 2. 转移矩阵 (全量，用于平稳分布) ──
        tm = self._build_transition_matrix(labels)
        if tm is None:
            return self._empty_result()

        # ── 3. 平稳分布 ──
        stationary = self._stationary_distribution(tm)

        # ── 4. 前向滚动重建 (最后一步，用历史数据，无未来信息) ──
        walk_forward_tm = None
        if len(labels) > self.min_train:
            walk_forward_tm = self._build_transition_matrix(labels[:-1]) or tm

        current_tm = walk_forward_tm or tm
        current_label = labels[-1]

        # ── 5. 下一步概率 ──
        state_idx = {"bear": 0, "sideways": 1, "bull": 2}
        idx = state_idx.get(current_label, 1)
        next_probs = {
            "bear": round(current_tm[idx][0], 3),
            "sideways": round(current_tm[idx][1], 3),
            "bull": round(current_tm[idx][2], 3),
        }

        # ── 6. 信号分数 ──
        signal = round(next_probs["bull"] - next_probs["bear"], 3)

        # ── 7. 持续性 ──
        persistence = {
            "bear": round(tm[0][0], 3),
            "sideways": round(tm[1][1], 3),
            "bull": round(tm[2][2], 3),
        }

        # ── 8. 置信度 ──
        diag = persistence.get(current_label, 0.5)
        confidence = int(diag * 100)

        # ── 9. 描述 ──
        detail = self._make_detail(current_label, signal, stationary, persistence)

        result = {
            "current_regime": current_label,
            "signal": signal,
            "next_probs": next_probs,
            "transition_matrix": tm,
            "persistence": persistence,
            "stationary_dist": {
                "bear": round(stationary[0], 3),
                "sideways": round(stationary[1], 3),
                "bull": round(stationary[2], 3),
            },
            "confidence": confidence,
            "detail": detail,
        }

        self._cache[cache_key] = result
        if len(self._cache) > 20:
            # 淘汰最旧的
            self._cache.pop(next(iter(self._cache)))

        # v4.0: 持久化 (每 10 次调用存一次)
        if not hasattr(self, '_save_counter'):
            self._save_counter = 0
        self._save_counter += 1
        if self._save_counter % 10 == 0:
            self._save_state(symbol, tm, labels)

        return result

    # ════════════════════════════════════════════
    # v4.0: 持久化
    # ════════════════════════════════════════════

    def _save_state(self, symbol: str, tm: List[List[float]], labels: List[str]):
        """保存转移矩阵和最后状态，重启后恢复。"""
        try:
            state = {
                "symbol": symbol,
                "transition_matrix": tm,
                "last_label": labels[-1] if labels else "sideways",
                "n_samples": len(labels),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = self.STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.STATE_FILE)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

    def load_state(self, symbol: str = "") -> Optional[Dict]:
        """加载持久化的马尔可夫状态。超过 24h 视为过期。"""
        if not os.path.exists(self.STATE_FILE):
            return None
        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            saved = state.get("saved_at", "")
            if saved:
                saved_ts = datetime.fromisoformat(saved)
                if (datetime.now(timezone.utc) - saved_ts).total_seconds() > 86400:
                    logger.debug("Markov 状态过期 (>24h)，丢弃")
                    return None
            logger.info(f"📊 已恢复 Markov 状态: {state.get('last_label','?')} "
                        f"(n={state.get('n_samples',0)})")
            return state
        except Exception as e:
            logger.debug(f"Markov 状态加载失败: {e}")
            return None

    # ════════════════════════════════════════════
    # 内部方法
    # ════════════════════════════════════════════

    def _compute_returns(self, prices: List[float]) -> List[float]:
        """计算滚动窗口收益率"""
        returns = []
        for i in range(self.window, len(prices)):
            ret = (prices[i] - prices[i - self.window]) / prices[i - self.window]
            returns.append(ret)
        return returns

    def _label_states(self, returns: List[float]) -> List[str]:
        """标注每个时间点的状态"""
        labels = []
        for ret in returns:
            if ret > self.threshold:
                labels.append("bull")
            elif ret < -self.threshold:
                labels.append("bear")
            else:
                labels.append("sideways")
        return labels

    def _build_transition_matrix(self, labels: List[str]) -> Optional[List[List[float]]]:
        """从标注序列构建 3×3 转移矩阵"""
        state_idx = {"bear": 0, "sideways": 1, "bull": 2}
        counts = [[0] * 3 for _ in range(3)]
        row_counts = [0] * 3

        for i in range(len(labels) - 1):
            frm = state_idx.get(labels[i])
            to = state_idx.get(labels[i + 1])
            if frm is None or to is None:
                continue
            counts[frm][to] += 1
            row_counts[frm] += 1

        # 归一化
        tm = []
        for r in range(3):
            if row_counts[r] > 0:
                tm.append([counts[r][c] / row_counts[r] for c in range(3)])
            else:
                # 无观测 → 均匀分布
                tm.append([1.0 / 3] * 3)

        if all(sum(row) > 0.99 for row in tm):
            return tm
        return None

    def _stationary_distribution(self, tm: List[List[float]]) -> List[float]:
        """
        通过幂迭代计算平稳分布。
        π = π × P，迭代至收敛。
        """
        pi = [1.0 / 3, 1.0 / 3, 1.0 / 3]
        for _ in range(100):
            new_pi = [0.0, 0.0, 0.0]
            for j in range(3):
                for i in range(3):
                    new_pi[j] += pi[i] * tm[i][j]
            # 收敛检查
            diff = sum(abs(new_pi[i] - pi[i]) for i in range(3))
            pi = new_pi
            if diff < 1e-8:
                break
        return pi

    def _make_detail(self, regime: str, signal: float,
                     stationary: List[float],
                     persistence: Dict[str, float]) -> str:
        """生成中文摘要"""
        names = {"bear": "🐻熊市", "sideways": "📊震荡", "bull": "🐂牛市"}
        parts = [f"当前: {names.get(regime, regime)}"]

        # 平稳分布: 哪个占主导
        dom_idx = max(range(3), key=lambda i: stationary[i])
        dom_names = ["熊市主导", "震荡主导", "牛市主导"]
        bear_pct = stationary[0] * 100
        bull_pct = stationary[2] * 100
        parts.append(f"长期: {dom_names[dom_idx]}(熊{bear_pct:.0f}%/牛{bull_pct:.0f}%)")

        # 持续性
        pers = persistence.get(regime, 0.5) * 100
        if pers > 70:
            parts.append(f"高持续({pers:.0f}%)")
        elif pers < 40:
            parts.append(f"低持续({pers:.0f}%)→可能切换")

        # 信号方向
        if signal > 0.3:
            parts.append("→偏多")
        elif signal < -0.3:
            parts.append("→偏空")
        else:
            parts.append("→中性")

        return " | ".join(parts)

    # ════════════════════════════════════════════
    # v4.0: 6 状态映射
    # ════════════════════════════════════════════

    def get_six_state(self, prices: List[float], symbol: str = "",
                      adx: float = 0, atr_pct: float = 0) -> Dict[str, Any]:
        """
        将 Markov 3 状态 + 辅助指标映射为 v4.0 6 状态。

        映射逻辑:
          Markov bull + ADX 强势  → strong_bull
          Markov bull + ADX 一般  → bull
          Markov bear + ADX 强势  → strong_bear
          Markov bear + ADX 一般  → bear
          Markov sideways          → range
          极端 ATR% (>2.5%)      → panic (覆盖以上)

        Returns:
            与 _detect_market_regime 兼容的 dict, 包含 regime/recommended/direction 等
        """
        result = self.detect(prices, symbol)
        regime_3 = result.get("current_regime", "sideways")
        signal = result.get("signal", 0)

        # 极端波动 → panic 覆盖
        if atr_pct >= 2.5:
            return {
                "regime": "panic",
                "direction": "neutral",
                "volatility": "extreme",
                "trend_strength": "strong" if adx >= 25 else "moderate",
                "atr_pct": round(atr_pct, 3),
                "consensus": 0.0,
                "recommended": [],
                "confidence": 85,
                "detail": "🚨Markov:极端波动-仅观望",
                "markov_bias": signal,
            }

        # 3→6 状态映射
        adx_strong = adx >= 25

        if regime_3 == "bull":
            if adx_strong:
                regime = "strong_bull"
                recommended = ["momentum"]
                detail = "🐂Markov牛市+ADX强势→强牛"
            else:
                regime = "bull"
                recommended = ["pullback", "ema_cross"]
                detail = "📈Markov牛市→回调做多"
            direction = "bullish"
        elif regime_3 == "bear":
            if adx_strong:
                regime = "strong_bear"
                recommended = ["momentum"]
                detail = "🐻Markov熊市+ADX强势→强熊"
            else:
                regime = "bear"
                recommended = ["pullback", "counter_trend"]
                detail = "📉Markov熊市→回调做空"
            direction = "bearish"
        else:  # sideways
            regime = "range"
            recommended = ["grid", "bollinger", "pullback"]
            direction = "neutral"
            detail = "📊Markov震荡→网格/布林"

        return {
            "regime": regime,
            "direction": direction,
            "volatility": "extreme" if atr_pct >= 1.0 else ("normal" if atr_pct >= 0.3 else "low"),
            "trend_strength": "strong" if adx_strong else "moderate",
            "atr_pct": round(atr_pct, 3),
            "consensus": abs(signal),  # Markov signal 绝对值视为共识度
            "recommended": recommended,
            "confidence": result.get("confidence", 50),
            "detail": detail,
            "markov_bias": signal,
            "_source": "markov",  # 标记来源，便于区分
        }

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        return {
            "current_regime": "sideways",
            "signal": 0.0,
            "next_probs": {"bear": 0.33, "sideways": 0.34, "bull": 0.33},
            "transition_matrix": [[0.33, 0.33, 0.33] for _ in range(3)],
            "persistence": {"bear": 0.5, "sideways": 0.5, "bull": 0.5},
            "stationary_dist": {"bear": 0.33, "sideways": 0.34, "bull": 0.33},
            "confidence": 30,
            "detail": "数据不足-默认震荡",
        }
