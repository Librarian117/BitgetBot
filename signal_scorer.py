#!/usr/bin/env python3
"""
signal_scorer.py — 统一加权评分引擎 v2.0
==========================================
将所有决策维度整合为单一 0-100 分数，消除串行加减混乱。

v2.0 新增: 组合排名维度 (8 维度)

评分公式:
  final = 技术面×28% + 市场状态×14% + 情绪×14% + OI×10% + 马可夫×10% + 策略×9% + 方向×9% + 组合×6%

三层决策:
  score > 70  → AUTO    (自动开仓，跳过 AI 审核，省 API)
  score 50-70 → AI      (DeepSeek 做最终 CONFIRM/REJECT)
  score < 50  → REJECT  (自动拒绝，不浪费 AI 调用)

用法:
  from signal_scorer import SignalScorer
  scorer = SignalScorer()
  result = scorer.score(sig, regime_info, sentiment, flow, markov, learner,
                        portfolio_rank=1, portfolio_total=1)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("QuantBot")


class SignalScorer:
    """统一加权评分引擎"""

    # 权重配置 (可调, sum MUST = 1.0)
    WEIGHTS = {
        "technical":    0.28,   # 技术面 (MACD/RSI/布林/趋势)
        "regime":       0.14,   # 市场状态 (趋势强度+波动率)
        "sentiment":    0.14,   # 情绪量化 (F&G+新闻)
        "oi_flow":      0.10,   # 订单流 (OI背离)
        "markov":       0.10,   # 马可夫状态
        "strategy":     0.09,   # 策略权重 (历史表现)
        "direction":    0.09,   # 方向开关 (历史胜率)
        "portfolio":    0.06,   # v2.0: 组合排名
    }

    # 决策阈值
    AUTO_THRESHOLD = 70     # >70 自动开仓
    AI_THRESHOLD = 50       # 50-70 AI 审核

    def score(self, sig: Dict[str, Any],
              regime_info: Optional[Dict] = None,
              sentiment: Optional[Dict] = None,
              flow_signal: Optional[Dict] = None,
              markov_result: Optional[Dict] = None,
              learner: Any = None,
              portfolio_rank: int = 1,
              portfolio_total: int = 1) -> Dict[str, Any]:
        """
        统一评分入口 v2.0。

        Args:
            sig:              信号字典 (来自 scan_single_symbol)
            regime_info:      市场状态检测结果
            sentiment:        情绪量化信号
            flow_signal:      OI 订单流信号
            markov_result:    马可夫状态结果
            learner:          SelfLearner 实例
            portfolio_rank:   v2.0: 组合排名 (1=最优)
            portfolio_total:  v2.0: 候选总数
        """

        direction = sig.get("direction", "LONG")
        strategy = sig.get("strategy", "pullback")
        confidence = sig.get("confidence", 50)
        bonuses = sig.get("bonuses", [])

        breakdown = {}
        total = 0.0

        # ── 1. 技术面 (28%) ──
        tech_score = self._score_technical(confidence, bonuses)
        breakdown["technical"] = {
            "score": tech_score,
            "detail": f"置信度={confidence} 加成={bonuses}",
        }
        total += tech_score * self.WEIGHTS["technical"]

        # ── 2. 市场状态 (14%) ──
        regime_score = self._score_regime(regime_info, direction, strategy)
        breakdown["regime"] = {
            "score": regime_score,
            "detail": regime_info.get("detail", "") if regime_info else "无数据",
        }
        total += regime_score * self.WEIGHTS["regime"]

        # ── 3. 情绪量化 (14%) ──
        sent_score = self._score_sentiment(sentiment, direction)
        breakdown["sentiment"] = {
            "score": sent_score,
            "detail": sentiment.get("detail", "") if sentiment else "无数据",
        }
        total += sent_score * self.WEIGHTS["sentiment"]

        # ── 4. OI 订单流 (10%) ──
        oi_score = self._score_oi_flow(flow_signal, direction)
        breakdown["oi_flow"] = {
            "score": oi_score,
            "detail": flow_signal.get("detail", "") if flow_signal else "无数据",
        }
        total += oi_score * self.WEIGHTS["oi_flow"]

        # ── 5. 马可夫状态 (10%) ──
        markov_score = self._score_markov(markov_result, direction)
        breakdown["markov"] = {
            "score": markov_score,
            "detail": markov_result.get("detail", "") if markov_result else "无数据",
        }
        total += markov_score * self.WEIGHTS["markov"]

        # ── 6. 策略权重 (9%) ──
        regime_label = regime_info.get("regime", "unknown") if regime_info else "unknown"
        strat_score = self._score_strategy(learner, strategy, direction, regime_label)
        breakdown["strategy"] = {
            "score": strat_score,
            "detail": f"策略={strategy} 方向={direction}",
        }
        total += strat_score * self.WEIGHTS["strategy"]

        # ── 7. 方向开关 (9%) ──
        dir_score = self._score_direction(learner, direction)
        breakdown["direction"] = {
            "score": dir_score,
            "detail": f"方向={direction}",
        }
        total += dir_score * self.WEIGHTS["direction"]

        # ── 8. 组合排名 (6%) v2.0 ──
        port_score = self._score_portfolio(portfolio_rank, portfolio_total)
        breakdown["portfolio"] = {
            "score": port_score,
            "detail": f"排名#{portfolio_rank}/{portfolio_total}",
        }
        total += port_score * self.WEIGHTS["portfolio"]

        # ── 最终分数 ──
        final_score = int(round(total))
        final_score = max(0, min(100, final_score))

        # ── 决策层级 ──
        if final_score > self.AUTO_THRESHOLD:
            tier = "AUTO"
        elif final_score >= self.AI_THRESHOLD:
            tier = "AI"
        else:
            tier = "REJECT"

        # ── 摘要 ──
        parts = [f"{k}={v['score']}" for k, v in breakdown.items()]
        summary = f"总分={final_score} [{tier}] | {' | '.join(parts)}"

        return {
            "score": final_score,
            "tier": tier,
            "breakdown": breakdown,
            "summary": summary,
        }

    # ════════════════════════════════════════════
    # 各维度评分 (每个返回 0-100)
    # ════════════════════════════════════════════

    @staticmethod
    def _score_technical(confidence: int, bonuses: List[str]) -> int:
        """技术面: 直接映射基础置信度 + 加成数量"""
        base = confidence
        # 多个加成 → 更可靠
        bonus_count = len([b for b in bonuses if b != "无加成"])
        if bonus_count >= 4:
            base = min(100, base + 10)
        elif bonus_count >= 2:
            base = min(100, base + 5)
        return base

    @staticmethod
    def _score_regime(regime_info: Optional[Dict], direction: str, strategy: str) -> int:
        """市场状态: 策略是否匹配当前状态"""
        if not regime_info:
            return 50  # 无数据 → 中性
        recommended = regime_info.get("recommended", [])
        regime_conf = regime_info.get("confidence", 50)
        if strategy in recommended:
            return regime_conf  # 推荐策略 → 用状态置信度
        return max(20, regime_conf - 25)  # 不推荐 → 扣分

    @staticmethod
    def _score_sentiment(sentiment: Optional[Dict], direction: str) -> int:
        """情绪量化: F&G 极端值的影响"""
        if not sentiment:
            return 50
        action = sentiment.get("market_action", "neutral")
        conf = sentiment.get("confidence", 50)
        if action == "contrarian_buy":
            return 80 if direction == "LONG" else 30  # 偏多
        if action == "reduce_risk":
            return 30 if direction == "LONG" else 80  # 偏空
        # 情绪趋势影响
        score_val = sentiment.get("score", 50)
        if score_val < 35:  # 恐惧 → 偏多
            return 70 if direction == "LONG" else 35
        if score_val > 65:  # 贪婪 → 偏空
            return 35 if direction == "LONG" else 70
        return conf

    @staticmethod
    def _score_oi_flow(flow: Optional[Dict], direction: str) -> int:
        """OI 订单流: 背离信号"""
        if not flow or flow.get("signal") == "neutral":
            return 50
        signal = flow.get("signal", "neutral")
        conf = flow.get("confidence", 50)
        if signal == "bullish" and direction == "LONG":
            return conf + 10
        if signal == "bearish" and direction == "SHORT":
            return conf + 10
        if signal in ("bullish_divergence", "bearish_divergence"):
            if (signal == "bearish_divergence" and direction == "SHORT") or \
               (signal == "bullish_divergence" and direction == "LONG"):
                return min(100, conf + 15)  # 背离信号更强
            return max(20, conf - 15)  # 背离方向相反 → 扣分
        return conf

    @staticmethod
    def _score_markov(markov: Optional[Dict], direction: str) -> int:
        """马可夫状态: 信号方向一致性"""
        if not markov or markov.get("confidence", 0) < 50:
            return 50
        mr_signal = markov.get("signal", 0)
        if direction == "LONG" and mr_signal > 0.2:
            return 80
        if direction == "SHORT" and mr_signal < -0.2:
            return 80
        if direction == "LONG" and mr_signal < -0.2:
            return 25
        if direction == "SHORT" and mr_signal > 0.2:
            return 25
        return 50

    @staticmethod
    def _score_strategy(learner: Any, strategy: str, direction: str,
                        regime: str = "unknown") -> int:
        """策略权重: 历史表现"""
        if not learner:
            return 50
        try:
            weight = learner.get_strategy_weight(strategy)
            regime_w = learner.tracker.get_regime_strategy_weight(
                regime, strategy, direction)
            combined = weight * regime_w
            if combined >= 1.2:
                return 85
            if combined >= 1.0:
                return 65
            if combined >= 0.7:
                return 45
            return 25
        except Exception:
            return 50

    @staticmethod
    def _score_direction(learner: Any, direction: str) -> int:
        """方向开关: 滚动胜率"""
        if not learner:
            return 50
        try:
            viable, reason = learner.is_direction_viable(direction)
            if not viable:
                return 15  # 严重扣分
            # 获取方向的胜率
            recent = [t for t in learner.tracker.recent_trades[-10:]
                      if t.get("direction") == direction]
            if len(recent) >= 5:
                wins = sum(1 for t in recent if t.get("pnl", 0) > 0)
                wr = wins / len(recent) * 100
                if wr >= 70:
                    return 90
                if wr >= 50:
                    return 65
                if wr >= 30:
                    return 40
                return 20
            return 50  # 数据不足
        except Exception:
            return 50

    @staticmethod
    def _score_portfolio(rank: int, total: int) -> int:
        """v2.0: 组合排名 → 分数。排名越靠前分越高"""
        if total <= 1:
            return 50
        ratio = (rank - 1) / max(total - 1, 1)  # 0.0=最优, 1.0=最差
        if rank == 1:
            return 90
        elif rank == 2:
            return 75
        elif rank == 3:
            return 60
        elif ratio < 0.5:
            return 45
        elif ratio < 0.75:
            return 30
        else:
            return 15
