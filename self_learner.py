#!/usr/bin/env python3
"""
self_learner.py — 量化自我学习引擎 v3.7
========================================
v3.7 核心升级:
  1. 增强去重 — 关键词重叠 + 长度自适应阈值 + 来源频率限制
  2. 智慧检索 — 加权评分 (regime 2x, symbol 1.5x) + 部分匹配回退
  3. 多智能体 — ThreadPoolExecutor 并行 + 总体超时 45s
  4. 策略权重闭环 — 历史表现实时影响信号置信度
  5. 快速诊断 — 统计旁路 (零 API 消耗, >85% 无信号时触发)
  6. WisdomStore LRU — 按质量优先级智能淘汰 (neutral > bad > old good)
  7. 紧急回顾 — 连续亏损 2 笔触发, 冷却 30 分钟

学习回路:
  持仓变化 → 盈亏追踪 → 策略评分 → 参数自动调整 → 反馈到下单逻辑
"""

import json
import logging
import os
import re
import time
from collections import defaultdict
from time_utils import today_str, now_iso, now_str
from typing import Any, Dict, List, Optional, Tuple

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("QuantBot")


def _get_deepseek_content(msg: dict, default: str = "{}") -> str:
    """v3.7: 合并 DeepSeek 推理模型的 content + reasoning_content。
    关键修复: deepseek-v4-pro 回复在 reasoning_content，只用 .get("content")
    会导致 AI 回复被丢弃。
    """
    c = (msg.get("content") or "").strip()
    r = (msg.get("reasoning_content") or "").strip()
    if r and c:
        return f"{r}\n{c}"
    return r or c or default


class StrategyTracker:
    """策略绩效追踪器 — 纯本地，不依赖 DeepSeek"""

    def __init__(self):
        # 按策略: {wins, losses, total_pnl, avg_win, avg_loss, symbols: {}}
        self.strategies: Dict[str, Dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "best_symbol": "", "worst_symbol": "",
            "symbols": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0}),
            "sessions": defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0}),
        })
        # 按方向
        self.directions: Dict[str, Dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
        })
        # 按币种
        self.symbols: Dict[str, Dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0, "total_trades": 0,
        })
        # 按市场环境 — "regime_strategy_direction" → 绩效
        self.regime_performance: Dict[str, Dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
        })
        # v4.0: 因子归因历史
        self._factor_history: List[Dict] = []
        self._max_factor_history = 50

        # 滑动窗口 (最近20笔)
        self.recent_trades: List[Dict] = []
        self._max_recent = 20

    def record_trade(self, symbol: str, direction: str, strategy: str,
                     pnl: float, pnl_pct: float, session: str = "",
                     entry_price: float = 0, exit_price: float = 0,
                     market_regime: str = ""):
        """记录一笔已完成的交易"""
        # v4.0: 去重 — 同一 symbol+direction+pnl 在 2 分钟内不重复记录
        now_ts = now_str("%Y-%m-%d %H:%M")
        for prev in reversed(self.recent_trades[-5:]):
            if (prev.get("symbol") == symbol
                and prev.get("direction") == direction
                and abs(prev.get("pnl", 0) - round(pnl, 2)) < 0.02
                and prev.get("date", "")[:15] == now_ts[:15]):  # 同一分钟
                return  # 重复，跳过
        is_win = pnl > 0
        s = self.strategies[strategy]
        d = self.directions[direction]
        sym = self.symbols[symbol]

        # 策略统计
        if is_win:
            s["wins"] += 1
            s["avg_win"] = (s["avg_win"] * (s["wins"] - 1) + pnl) / s["wins"]
        else:
            s["losses"] += 1
            s["avg_loss"] = (s["avg_loss"] * (s["losses"] - 1) + pnl) / s["losses"]
        s["total_pnl"] += pnl
        s["symbols"][symbol]["wins" if is_win else "losses"] += 1
        s["symbols"][symbol]["pnl"] += pnl
        if session:
            s["sessions"][session]["wins" if is_win else "losses"] += 1
            s["sessions"][session]["pnl"] += pnl

        # 方向统计
        if is_win:
            d["wins"] += 1
        else:
            d["losses"] += 1
        d["total_pnl"] += pnl

        # 币种统计
        sym["wins" if is_win else "losses"] += 1
        sym["total_pnl"] += pnl
        sym["total_trades"] += 1

        # 滑动窗口
        self.recent_trades.append({
            "symbol": symbol, "direction": direction, "strategy": strategy,
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "entry": round(entry_price, 4), "exit": round(exit_price, 4),
            "session": session, "market_regime": market_regime,
            "date": now_str("%Y-%m-%d %H:%M"),
        })
        if len(self.recent_trades) > self._max_recent:
            self.recent_trades = self.recent_trades[-self._max_recent:]

        # 市场环境维度
        if market_regime:
            key = f"{market_regime}_{strategy}_{direction}"
            r = self.regime_performance[key]
            if is_win:
                r["wins"] += 1
                r["avg_win"] = (r["avg_win"] * (r["wins"] - 1) + pnl) / r["wins"]
            else:
                r["losses"] += 1
                r["avg_loss"] = (r["avg_loss"] * (r["losses"] - 1) + pnl) / r["losses"]
            r["total_pnl"] += pnl

    # ════════════════════════════════════════════
    # v4.0: 因子归因
    # ════════════════════════════════════════════

    def record_factors(self, symbol: str, direction: str, strategy: str,
                       pnl: float, factor_scores: Dict[str, float]):
        """存储一笔交易及其开仓时的各因子分数，用于归因分析。"""
        entry = {
            "symbol": symbol, "direction": direction, "strategy": strategy,
            "pnl": round(pnl, 4),
            "factors": factor_scores,
        }
        self._factor_history.append(entry)
        if len(self._factor_history) > self._max_factor_history:
            self._factor_history = self._factor_history[-self._max_factor_history:]

    def get_factor_attribution(self) -> Dict:
        """计算各因子维度与盈利的相关性。
        对每个维度，对比"该维度高分交易"vs"该维度低分交易"的胜率差异。
        """
        if len(self._factor_history) < 5:
            return {"status": "insufficient_data", "n": len(self._factor_history)}

        attribution = {}
        for dim in ["technical", "regime", "sentiment", "oi_flow",
                     "markov", "strategy", "direction", "portfolio"]:
            high_wins = high_total = 0
            low_wins = low_total = 0
            for entry in self._factor_history:
                score = entry["factors"].get(dim, 50)
                won = entry["pnl"] > 0
                if score >= 60:
                    high_total += 1
                    if won:
                        high_wins += 1
                elif score <= 40:
                    low_total += 1
                    if won:
                        low_wins += 1
            high_wr = high_wins / high_total * 100 if high_total >= 2 else None
            low_wr = low_wins / low_total * 100 if low_total >= 2 else None
            spread = round(high_wr - low_wr, 1) if (high_wr is not None and low_wr is not None) else None
            attribution[dim] = {
                "high_wr": round(high_wr, 1) if high_wr else None,
                "low_wr": round(low_wr, 1) if low_wr else None,
                "spread": spread,  # 正数=该因子有效预测盈利
                "high_n": high_total, "low_n": low_total,
            }
        return attribution

    def get_best_strategy(self) -> Optional[str]:
        """返回当前最优策略名"""
        best, best_pnl = None, float("-inf")
        for name, s in self.strategies.items():
            if s["total_pnl"] > best_pnl and (s["wins"] + s["losses"]) >= 2:
                best, best_pnl = name, s["total_pnl"]
        return best

    def get_win_rate(self, strategy: str = None) -> float:
        """获取胜率 (可指定策略，不指定则全局)"""
        if strategy:
            s = self.strategies.get(strategy)
            if not s:
                return 0.0
            total = s["wins"] + s["losses"]
            return s["wins"] / total * 100 if total > 0 else 0.0
        total_w = sum(s["wins"] for s in self.strategies.values())
        total_l = sum(s["losses"] for s in self.strategies.values())
        total = total_w + total_l
        return total_w / total * 100 if total > 0 else 0.0

    def get_regime_strategy_weight(self, market_regime: str, strategy: str,
                                   direction: str) -> float:
        """
        根据特定市场环境+策略+方向组合的历史表现返回权重乘数。

        Returns:
            0.5–1.5: <3笔 → 1.0; 胜率>60%且总盈亏>0 → 1.3; 胜率<40% → 0.6; 其他 → 1.0
        """
        key = f"{market_regime}_{strategy}_{direction}"
        r = self.regime_performance.get(key)
        if not r:
            return 1.0
        total = r["wins"] + r["losses"]
        if total < 3:
            return 1.0
        win_rate = r["wins"] / total
        if win_rate > 0.6 and r["total_pnl"] > 0:
            return 1.3
        if win_rate < 0.4:
            return 0.6
        return 1.0

    def get_regime_summary(self, market_regime: str) -> Dict:
        """
        返回给定市场环境下所有策略+方向组合的绩效统计。

        Returns:
            {"combos": {...}, "regime": "...", "total_trades": N, "total_pnl": float}
        """
        combos = {}
        total_trades = 0
        total_pnl = 0.0
        prefix = f"{market_regime}_"
        for key, r in self.regime_performance.items():
            if key.startswith(prefix):
                combo_key = key[len(prefix):]  # "strategy_direction"
                t = r["wins"] + r["losses"]
                wr = round(r["wins"] / t * 100, 1) if t > 0 else 0.0
                combos[combo_key] = {
                    "wins": r["wins"], "losses": r["losses"],
                    "total_pnl": round(r["total_pnl"], 2),
                    "avg_win": round(r["avg_win"], 2),
                    "avg_loss": round(r["avg_loss"], 2),
                    "win_rate": wr,
                }
                total_trades += t
                total_pnl += r["total_pnl"]
        return {
            "regime": market_regime,
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2),
            "combos": combos,
        }

    def get_summary(self) -> Dict:
        """返回策略绩效摘要"""
        result = {"strategies": {}, "directions": {}, "symbols": {}, "recent": self.recent_trades[-5:]}
        for name, s in self.strategies.items():
            total = s["wins"] + s["losses"]
            if total == 0:
                continue
            # 找出最佳和最差币种
            best_sym = max(s["symbols"].items(), key=lambda x: x[1]["pnl"], default=("?", {}))
            worst_sym = min(s["symbols"].items(), key=lambda x: x[1]["pnl"], default=("?", {}))
            result["strategies"][name] = {
                "trades": total, "wins": s["wins"], "losses": s["losses"],
                "win_rate": round(s["wins"] / total * 100, 1),
                "total_pnl": round(s["total_pnl"], 2),
                "avg_win": round(s["avg_win"], 2), "avg_loss": round(s["avg_loss"], 2),
                "best_symbol": best_sym[0], "worst_symbol": worst_sym[0],
                "best_session": max(s["sessions"].items(), key=lambda x: x[1]["pnl"], default=("?", {}))[0],
            }
        for name, d in self.directions.items():
            total = d["wins"] + d["losses"]
            if total == 0:
                continue
            result["directions"][name] = {
                "trades": total, "wins": d["wins"], "losses": d["losses"],
                "win_rate": round(d["wins"] / total * 100, 1),
                "total_pnl": round(d["total_pnl"], 2),
            }
        for name, s in self.symbols.items():
            if s["total_trades"] == 0:
                continue
            result["symbols"][name] = {
                "trades": s["total_trades"], "wins": s["wins"], "losses": s["losses"],
                "total_pnl": round(s["total_pnl"], 2),
            }
        return result

    def to_dict(self) -> Dict:
        """序列化 (排除嵌套 defaultdict)"""
        summary = self.get_summary()
        # 序列化 regime_performance: defaultdict → plain dict
        regime_data = {}
        for key, r in self.regime_performance.items():
            regime_data[key] = dict(r)
        return {
            "summary": summary,
            "recent_trades": self.recent_trades,
            "regime_performance": regime_data,
        }

    def load_from(self, data: Dict):
        """从保存的数据恢复"""
        if "recent_trades" in data:
            self.recent_trades = data["recent_trades"]
        # 重建统计数据 (从 recent_trades 回放)
        if "recent_trades" in data:
            for t in data["recent_trades"]:
                s = self.strategies[t.get("strategy", "pullback")]
                d = self.directions[t.get("direction", "LONG")]
                sym = self.symbols[t.get("symbol", "?")]
                pnl = t.get("pnl", 0)
                is_win = pnl > 0
                if is_win:
                    s["wins"] += 1
                    s["avg_win"] = (s["avg_win"] * (s["wins"] - 1) + pnl) / s["wins"]
                    d["wins"] += 1
                    sym["wins"] += 1
                else:
                    s["losses"] += 1
                    s["avg_loss"] = (s["avg_loss"] * (s["losses"] - 1) + pnl) / s["losses"]
                    d["losses"] += 1
                    sym["losses"] += 1
                s["total_pnl"] += pnl
                d["total_pnl"] += pnl
                sym["total_pnl"] += pnl
                sym["total_trades"] += 1
                s["symbols"][t.get("symbol", "?")]["wins" if is_win else "losses"] += 1
                s["symbols"][t.get("symbol", "?")]["pnl"] += pnl
                if t.get("session"):
                    s["sessions"][t["session"]]["wins" if is_win else "losses"] += 1
                    s["sessions"][t["session"]]["pnl"] += pnl
                # 重建 regime_performance
                regime = t.get("market_regime", "")
                if regime:
                    regime_key = f"{regime}_{t.get('strategy', 'pullback')}_{t.get('direction', 'LONG')}"
                    r = self.regime_performance[regime_key]
                    if is_win:
                        r["wins"] += 1
                        r["avg_win"] = (r["avg_win"] * (r["wins"] - 1) + pnl) / r["wins"]
                    else:
                        r["losses"] += 1
                        r["avg_loss"] = (r["avg_loss"] * (r["losses"] - 1) + pnl) / r["losses"]
                    r["total_pnl"] += pnl
        # 直接恢复序列化的 regime_performance (完整覆盖，补上回放可能遗漏的)
        if "regime_performance" in data:
            for key, r in data["regime_performance"].items():
                existing = self.regime_performance[key]
                # 取回放值和文件值中 trades 多的那个
                file_total = r.get("wins", 0) + r.get("losses", 0)
                existing_total = existing["wins"] + existing["losses"]
                if file_total > existing_total:
                    existing["wins"] = r.get("wins", 0)
                    existing["losses"] = r.get("losses", 0)
                    existing["total_pnl"] = r.get("total_pnl", 0.0)
                    existing["avg_win"] = r.get("avg_win", 0.0)
                    existing["avg_loss"] = r.get("avg_loss", 0.0)
        # v3.7 fix: 从 summary 恢复策略/方向/币种完整历史统计
        if "summary" in data:
            summary = data["summary"]
            for name, stats in summary.get("strategies", {}).items():
                s = self.strategies[name]
                s["wins"] = stats.get("wins", s["wins"])
                s["losses"] = stats.get("losses", s["losses"])
                s["total_pnl"] = stats.get("total_pnl", s["total_pnl"])
                s["avg_win"] = stats.get("avg_win", s["avg_win"])
                s["avg_loss"] = stats.get("avg_loss", s["avg_loss"])
            for name, stats in summary.get("directions", {}).items():
                d = self.directions[name]
                d["wins"] = stats.get("wins", d["wins"])
                d["losses"] = stats.get("losses", d["losses"])
                d["total_pnl"] = stats.get("total_pnl", d["total_pnl"])
            for name, stats in summary.get("symbols", {}).items():
                sym = self.symbols[name]
                sym["wins"] = stats.get("wins", sym["wins"])
                sym["losses"] = stats.get("losses", sym["losses"])
                sym["total_pnl"] = stats.get("total_pnl", sym["total_pnl"])
                sym["total_trades"] = stats.get("trades", sym["total_trades"])


class WisdomStore:
    """
    WisdomStore — 语义化交易智慧存储与检索 (Layer 2: Memory Retrieval System)

    每条 wisdom 自动生成 embedding_key (market_regime_symbol_strategy_direction)，
    检索时基于关键词重叠度评分，返回最相关的 wisdom 条目注入 AI prompt。
    无需外部 embedding 库，纯 token 重叠匹配，高效且可解释。
    """

    def __init__(self, max_entries: int = 30):
        self.entries: List[Dict[str, Any]] = []
        self.max_entries = max_entries

    # ── embedding key 构造 ──

    @staticmethod
    def _build_embedding_key(entry: dict) -> str:
        """v3.7: 从已知字段构造 embedding_key，跳过 unknown 占位符。"""
        parts = []
        for field in ("market_regime", "regime", "market_context"):
            v = entry.get(field, "").strip().lower()
            if v and v != "unknown":
                parts.append(("regime", v))
                break
        for field in ("symbol",):
            v = entry.get(field, "").strip().lower()
            if v and v != "unknown":
                parts.append(("sym", v))
                break
        for field in ("strategy",):
            v = entry.get(field, "").strip().lower()
            if v and v != "unknown":
                parts.append(("strat", v))
                break
        for field in ("direction",):
            v = entry.get(field, "").strip().lower()
            if v and v != "unknown":
                parts.append(("dir", v))
                break
        if not parts:
            return f"general_{entry.get('date', '')}"
        return "_".join(f"{k}:{v}" for k, v in parts)

    @staticmethod
    def _build_context_key(context: dict) -> str:
        """v3.7: 同上，用于检索时的 context key。"""
        return WisdomStore._build_embedding_key(context)

    # ── 相关性评分 ──

    @staticmethod
    def _token_overlap_score(key1: str, key2: str) -> float:
        """v3.7: 加权评分 — 前缀匹配 (regime:/sym:/strat:/dir:) 给予更高权重。"""
        if not key1 or not key2:
            return 0.0
        # 提取带前缀的 token
        def extract_tokens(k):
            tokens = set()
            for part in k.lower().split("_"):
                tokens.add(part)
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    tokens.add(prefix)  # 只匹配前缀也算部分相关
            return tokens
        t1 = extract_tokens(key1)
        t2 = extract_tokens(key2)
        if not t1 or not t2:
            return 0.0
        intersection = t1 & t2
        # 加权: regime 匹配权重 2x, sym 1.5x, 其他 1x
        weighted = 0.0
        for token in intersection:
            if token.startswith("regime:") or token == "regime":
                weighted += 2.0
            elif token.startswith("sym:") or token == "sym":
                weighted += 1.5
            else:
                weighted += 1.0
        max_possible = sum(2.0 if t.startswith("regime:") or t == "regime"
                          else 1.5 if t.startswith("sym:") or t == "sym"
                          else 1.0 for t in t1)
        return min(1.0, weighted / max_possible) if max_possible > 0 else 0.0

    # ── 检索 ──

    def retrieve_relevant(self, current_context: dict, top_k: int = 3) -> list:
        """v3.7: 增强检索 — 加权评分 + 回退到部分匹配。"""
        if not self.entries:
            return []

        ctx_key = self._build_context_key(current_context)
        scored: List[Tuple[float, Dict]] = []
        for entry in self.entries:
            entry_key = entry.get("embedding_key", "")
            # 如果有完整 entry_key，用加权评分
            if entry_key and entry_key != "general_":
                score = self._token_overlap_score(ctx_key, entry_key)
            else:
                # 回退: 用 insight 文本的 Jaccard 相似度
                score = 0.0
            if score > 0:
                scored.append((score, entry))

        # 如果没有精确匹配，回退到最近 actionable 条目
        if not scored:
            actionable = [e for e in self.entries[-10:]
                         if e.get("quality") in ("good", "bad", "neutral")]
            scored = [(0.1, e) for e in actionable[-top_k:]]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _score, entry in scored[:top_k]]

    # ── 添加 ──

    def add(self, entry: dict):
        """v3.7: 添加 wisdom 条目，自动生成 embedding_key，智能 LRU 淘汰"""
        entry["embedding_key"] = self._build_embedding_key(entry)
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            # 智能淘汰: 优先删 neutral → bad → 最旧的 good
            overflow = len(self.entries) - self.max_entries
            # 按优先级排序: neutral(先删) > bad > good(后删), 同优先级按时间
            quality_order = {"neutral": 0, "bad": 1, "good": 2}
            indexed = [(quality_order.get(e.get("quality", "neutral"), 0),
                        e.get("date", ""), i, e)
                       for i, e in enumerate(self.entries)]
            indexed.sort(key=lambda x: (x[0], x[1]))  # 低质量+旧日期在前
            # 移除 overflow 个最应淘汰的条目
            to_remove = {item[2] for item in indexed[:overflow]}
            self.entries = [e for i, e in enumerate(self.entries) if i not in to_remove]

    # ── 保存 / 加载 ──

    def save(self, filepath: str):
        """持久化到磁盘"""
        try:
            data = {
                "updated_at": now_iso(),
                "total_entries": len(self.entries),
                "wisdom": self.entries,
            }
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, filepath)
        except Exception as e:
            logger.error(f"🧠 保存 wisdom 失败: {e}")

    def load(self, filepath: str, _log_tag: str = ""):
        """从磁盘加载，对旧条目自动补全 embedding_key"""
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("wisdom", [])
            for entry in entries:
                if "embedding_key" not in entry:
                    entry["embedding_key"] = self._build_embedding_key(entry)
            self.entries = entries
            tag = f" {_log_tag}" if _log_tag else ""
            logger.info(f"🧠 已加载 {len(self.entries)} 条交易智慧{tag}")
        except Exception:
            self.entries = []


class SelfLearner:
    """
    量化自我学习引擎 v2.0。

    新增能力:
    1. 策略绩效追踪 (本地计算，不费 API)
    2. 持仓盈亏实时监控
    3. 动态 SL/TP/仓位 参数优化
    4. 智能 wisdom 去重 (语义级)
    5. 学习成果直接反馈到交易决策
    """

    WISDOM_FILE = "trading_wisdom.json"
    TRACKER_FILE = "strategy_performance.json"
    MAX_WISDOM_ENTRIES = 30
    REVIEW_INTERVAL_HOURS = 4  # 每 4 小时做一次宏观回顾
    EMERGENCY_REVIEW_LOSSES = 2  # v3.7: 连续亏损 N 笔 → 紧急回顾
    EMERGENCY_COOLDOWN_MINUTES = 30  # v3.7: 紧急回顾冷却时间

    # v2.0: 可自调参数范围
    TUNABLE_PARAMS = {
        "sl_atr_mult":       {"min": 0.8, "max": 2.5, "step": 0.1, "current": None},
        "tp_atr_mult_1":     {"min": 1.0, "max": 4.0, "step": 0.2, "current": None},
        "adx_margin_min":    {"min": 0.02, "max": 0.10, "step": 0.005, "current": None},
        "adx_margin_max":    {"min": 0.05, "max": 0.15, "step": 0.005, "current": None},
        "momentum_margin":   {"min": 0.01, "max": 0.08, "step": 0.005, "current": None},
        "rsi_oversold":      {"min": 20, "max": 40, "step": 2, "current": None},
        "rsi_overbought":    {"min": 60, "max": 80, "step": 2, "current": None},
    }

    def __init__(self, config, tlogger, log_dir: str = "logs"):
        self.config = config
        self.tlogger = tlogger
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._log_dir = os.path.join(script_dir, log_dir)
        # v3.6: 支持按日分割的日志文件
        self.trades_path = self._resolve_log_path()
        self.wisdom_path = os.path.join(script_dir, self.WISDOM_FILE)
        self.tracker_path = os.path.join(script_dir, self.TRACKER_FILE)

        self._wisdom_store = WisdomStore()
        self._last_review: float = 0.0
        self._last_emergency_review: float = 0.0  # v3.7: 紧急回顾冷却
        self._load_wisdom()

        # v2.0: 策略追踪器
        self.tracker = StrategyTracker()
        self._load_tracker()

        # ── 从 wisdom 恢复 auto_tune 状态 ──
        self._init_tune_state()

        # 当前调参索引
        self._param_tune_index: int = 0
        self._consecutive_loss_streak: int = 0

        # ── Layer 4: Ablation Testing ──
        self._trade_count_since_ablation: int = 0

        # ── 沙箱模式 ──
        self._is_sandbox = getattr(config, 'is_sandbox', False)

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.deepseek_api_key}",
            "Content-Type": "application/json",
        })

        # v2.0: 初始化可调参数的当前值
        self._init_tunable_params()

    def _resolve_log_path(self) -> str:
        """v3.6: 找到今天或最近的日志文件"""
        today = today_str()
        today_path = os.path.join(self._log_dir, f"trades_{today}.jsonl")
        if os.path.exists(today_path):
            return today_path
        legacy = os.path.join(self._log_dir, "trades.jsonl")
        if os.path.exists(legacy):
            return legacy
        return today_path

    # ════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════

    def _init_tunable_params(self):
        """从 config 读取当前参数值"""
        cfg = self.config
        params = self.TUNABLE_PARAMS
        params["sl_atr_mult"]["current"] = getattr(cfg, 'sl_atr_mult', 1.5)
        params["tp_atr_mult_1"]["current"] = getattr(cfg, 'tp_atr_mults', [2.0])[0] if getattr(cfg, 'tp_atr_mults', None) else 2.0
        params["adx_margin_min"]["current"] = getattr(cfg, 'adx_margin_min', 0.05)
        params["adx_margin_max"]["current"] = getattr(cfg, 'adx_margin_max', 0.10)
        params["momentum_margin"]["current"] = getattr(cfg, 'momentum_margin_ratio', 0.03)
        params["rsi_oversold"]["current"] = getattr(cfg, 'rsi_oversold', 30)
        params["rsi_overbought"]["current"] = getattr(cfg, 'rsi_overbought', 70)

    def _init_tune_state(self):
        """从 wisdom 推断调参状态"""
        strict_entries = sum(1 for w in self._wisdom_store.entries if "filter_analysis" in w.get("source", ""))
        tune_levels_all = [
            {"adx_threshold": 16, "vol_ratio_threshold": 1.2},
            {"adx_threshold": 14, "vol_ratio_threshold": 1.0},
            {"adx_threshold": 12, "vol_ratio_threshold": 0.7},
            {"adx_threshold": 10, "vol_ratio_threshold": 0.5},
        ]
        self._tune_count: int = min(strict_entries // 3, len(tune_levels_all))
        if self._tune_count > 0:
            lvl = tune_levels_all[min(max(0, self._tune_count - 1), len(tune_levels_all) - 1)]
            if self.config.adx_threshold > lvl["adx_threshold"]:
                self.config.adx_threshold = lvl["adx_threshold"]
                self.config.vol_ratio_threshold = lvl["vol_ratio_threshold"]
                logger.info(f"🧠 从历史恢复调参: ADX={lvl['adx_threshold']}, 量比={lvl['vol_ratio_threshold']}")
        self._consecutive_strict_diag: int = strict_entries % 3

    # ════════════════════════════════════════════
    # v2.0: 策略绩效追踪 (核心新增)
    # ════════════════════════════════════════════

    def record_closed_trade(self, symbol: str, direction: str, strategy: str,
                            entry_price: float, exit_price: float,
                            pnl: float, pnl_pct: float, session: str = "",
                            market_regime: str = ""):
        """记录一笔已平仓的交易 → 更新策略追踪器"""
        self.tracker.record_trade(
            symbol=symbol, direction=direction, strategy=strategy,
            pnl=pnl, pnl_pct=pnl_pct, session=session,
            entry_price=entry_price, exit_price=exit_price,
            market_regime=market_regime,
        )
        self._save_tracker()
        # Layer 4: increment ablation counter
        self._trade_count_since_ablation += 1
        # 检查是否需要自动调参
        self._check_auto_param_tune()

    def get_strategy_context(self) -> str:
        """
        返回策略绩效上下文，注入到 AI 审核 prompt。
        让 DeepSeek 知道哪些策略/方向/币种当前表现更好。
        """
        summary = self.tracker.get_summary()
        if not summary.get("strategies") and not summary.get("directions"):
            return ""

        lines = ["\n## 策略绩效追踪 (基于已平仓交易)"]

        # 策略表现
        for name, s in summary.get("strategies", {}).items():
            lines.append(
                f"- {name}: {s['trades']}笔 胜率{s['win_rate']}% "
                f"盈亏{s['total_pnl']:+.2f} 均盈{s['avg_win']:+.2f} 均亏{s['avg_loss']:+.2f} "
                f"最佳币种:{s['best_symbol']} 最佳时段:{s['best_session']}"
            )

        # 方向表现
        for name, d in summary.get("directions", {}).items():
            lines.append(
                f"- {name}方向: {d['trades']}笔 胜率{d['win_rate']}% 盈亏{d['total_pnl']:+.2f}"
            )

        # 币种表现
        sym_lines = []
        for name, s in sorted(summary.get("symbols", {}).items(),
                              key=lambda x: x[1]["total_pnl"], reverse=True):
            sym_lines.append(
                f"  {name}: {s['trades']}笔 {s['wins']}赢{s['losses']}输 "
                f"盈亏{s['total_pnl']:+.2f}"
            )
        if sym_lines:
            lines.append("- 币种盈亏排名:")
            lines.extend(sym_lines[:10])

        # 最近交易
        if self.tracker.recent_trades:
            lines.append("- 最近平仓:")
            for t in self.tracker.recent_trades[-3:]:
                lines.append(
                    f"  {t['date']} {t['symbol']} {t['direction']} {t['strategy']} "
                    f"盈亏{t['pnl']:+.2f}"
                )

        return "\n".join(lines)

    def get_strategy_weight(self, strategy: str) -> float:
        """
        根据策略历史表现返回权重 (用于仓位大小调整)。
        胜率 > 50% 且总盈亏 > 0 → 1.0+
        胜率 < 40% → 0.5
        无数据 → 1.0 (默认)
        """
        s = self.tracker.strategies.get(strategy)
        if not s:
            return 1.0
        total = s["wins"] + s["losses"]
        if total < 3:
            return 1.0  # 数据不足，默认权重

        win_rate = s["wins"] / total
        if win_rate >= 0.6 and s["total_pnl"] > 0:
            return 1.3  # 优秀策略加仓
        elif win_rate >= 0.5:
            return 1.0
        elif win_rate >= 0.4:
            return 0.7
        else:
            return 0.5  # 差策略减仓

    def is_direction_viable(self, direction: str, min_trades: int = 5,
                            win_rate_threshold: float = 0.30,
                            force_allow: bool = False,
                            symbol: str = "") -> tuple:
        """v3.7: 方向可行性判断。
        v4.0: 币种级方向开关 — 优先按币种+方向判断, 数据不足时回退全局。
              每币种只需 3 笔即可独立判断, 至少需要 1 笔盈利 (33% wr)。

        Returns: (viable: bool, reason: str)
        """
        if force_allow:
            return True, ""

        recent_10 = self.tracker.recent_trades[-10:]

        # ── v4.0: 币种级检查 (优先) ──
        if symbol:
            sym_recent = [t for t in recent_10
                          if t.get("symbol") == symbol and t.get("direction") == direction]
            if len(sym_recent) >= 3:
                wins = sum(1 for t in sym_recent if t.get("pnl", 0) > 0)
                wr = wins / len(sym_recent)
                if wr == 0:  # 该币种该方向全输 → 封杀
                    return False, (
                        f"{symbol} {direction} 币种{len(sym_recent)}笔胜率0%，暂停"
                    )
                # 否则放行（有盈利就不封）
                return True, ""

        # ── 全局回退: 按方向统计 ──
        recent = [t for t in recent_10 if t.get("direction") == direction]
        if len(recent) < min_trades:
            return True, ""
        wins = sum(1 for t in recent if t.get("pnl", 0) > 0)
        wr = wins / len(recent)
        if wr < win_rate_threshold:
            return False, (
                f"{direction}方向滚动{len(recent)}笔胜率{wr*100:.0f}%"
                f"<{win_rate_threshold*100:.0f}%，自动暂停"
            )
        return True, ""

    # ════════════════════════════════════════════
    # v2.0: 持仓实时分析 (不耗 API)
    # ════════════════════════════════════════════

    def analyze_open_positions(self, positions: List[Dict],
                                session_label: str = "") -> Optional[str]:
        """
        分析当前持仓，提取本地洞察（不调用 DeepSeek）。
        检测:
        - 是否有币种持续亏损需要关注
        - 盈利币种是否可以移动止损
        - 做多/做空比例是否失衡
        """
        if not positions:
            return None

        insights = []
        longs = [p for p in positions if p.get("side") == "LONG"]
        [p for p in positions if p.get("side") == "SHORT"]

        # 1. 方向比例检查
        total = len(positions)
        long_pct = len(longs) / total * 100 if total > 0 else 0
        if long_pct > 80:
            insights.append(f"⚠️ 做多占比{ long_pct:.0f}%，单向风险过高，建议等待做空信号")
        elif long_pct < 20 and total >= 3:
            insights.append(f"⚠️ 做空占比{100-long_pct:.0f}%，单向风险过高，建议等待做多信号")

        # 2. 浮亏最大的币种
        losers = sorted(positions, key=lambda x: x.get("unrealized_pnl", 0))
        if losers and losers[0].get("unrealized_pnl", 0) < -30:
            worst = losers[0]
            insights.append(
                f"🔴 最大浮亏: {worst['symbol']} {worst.get('side','')} "
                f"浮亏{worst['unrealized_pnl']:.1f} 入场{worst.get('entry_price',0)}"
            )

        # 3. 盈利王
        winners = sorted(positions, key=lambda x: x.get("unrealized_pnl", 0), reverse=True)
        if winners and winners[0].get("unrealized_pnl", 0) > 50:
            best = winners[0]
            insights.append(
                f"🟢 最大浮盈: {best['symbol']} {best.get('side','')} "
                f"浮盈{best['unrealized_pnl']:.1f} 建议移动止损锁定利润"
            )

        # 4. 总浮盈/浮亏
        total_upl = sum(p.get("unrealized_pnl", 0) for p in positions)
        total_margin = sum(p.get("margin", 0) for p in positions)
        if total_margin > 0:
            roi = total_upl / total_margin * 100
            insights.append(f"📊 总浮盈{total_upl:+.1f} USDT (保证金回报率{roi:+.1f}%)")

        return "\n".join(insights) if insights else None

    # ════════════════════════════════════════════
    # v2.1: Multi-Agent Reflection Loop
    # ════════════════════════════════════════════

    def multi_agent_reflection(self, trade_data: dict) -> List[Dict]:
        """
        Multi-Agent Reflection Loop — 3 位 AI 专家并行审核同一笔交易。

        Agent A (策略分析师): 诊断入场时机、方向判断、策略选择
        Agent B (风控审计师): 审查止损位、仓位大小、风险敞口
        Agent C (执行审查员): 审查滑点、手续费影响、出场时机

        三路并行调用 DeepSeek，使用 ThreadPoolExecutor 并发，
        任一 Agent 失败不影响其他。
        """
        if not self.config.deepseek_api_key:
            return []

        symbol = trade_data.get("symbol", "?")
        direction = trade_data.get("direction", "?")
        strategy = trade_data.get("strategy", "?")
        entry_price = trade_data.get("entry_price", 0)
        exit_price = trade_data.get("exit_price", 0)
        pnl = trade_data.get("pnl", 0)
        pnl_pct = trade_data.get("pnl_pct", 0)
        holding_hours = trade_data.get("holding_hours", 0)
        ai_decision = trade_data.get("ai_decision", "?")

        trade_summary = (
            f"交易详情:\n"
            f"- 币种: {symbol}  方向: {direction}  策略: {strategy}\n"
            f"- 入场: {entry_price:.4f}  出场: {exit_price:.4f}\n"
            f"- 盈亏: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)  持仓: {holding_hours:.1f}小时\n"
            f"- AI审核: {ai_decision}"
        )

        # 三路 Agent 定义：(名称, system_prompt, user_prompt)
        agents = [
            (
                "strategy_analyst",
                "你是策略分析师。诊断这笔交易的入场时机、方向判断、策略选择是否正确。用中文给出50字以内的批判性诊断。返回JSON: {\"diagnosis\": \"...\", \"was_entry_good\": true/false, \"was_direction_good\": true/false, \"confidence\": 0-1}",
                trade_summary + "\n\n请诊断这笔交易的入场时机、方向判断、策略选择是否正确。用中文给出50字以内的批判性诊断。",
            ),
            (
                "risk_auditor",
                "你是风控审计师。审查这笔交易的止损位、仓位大小、风险敞口是否合理。返回JSON: {\"diagnosis\": \"...\", \"sl_was_appropriate\": true/false, \"position_size_ok\": true/false, \"confidence\": 0-1}",
                trade_summary + "\n\n审查这笔交易的止损位、仓位大小、风险敞口是否合理。",
            ),
            (
                "execution_reviewer",
                "你是执行审查员。审查这笔交易的滑点、手续费影响、出场时机。返回JSON: {\"diagnosis\": \"...\", \"timing_score\": 0-10, \"fee_efficiency\": 0-1, \"confidence\": 0-1}",
                trade_summary + "\n\n审查这笔交易的滑点、手续费影响、出场时机。",
            ),
        ]

        results: List[Dict] = []

        def _call_agent(agent_name: str, system_prompt: str, user_prompt: str) -> Dict:
            """单次 Agent 调用 — 每个线程独立 Session 保证线程安全"""
            session = requests.Session()
            session.headers.update({
                "Authorization": f"Bearer {self.config.deepseek_api_key}",
                "Content-Type": "application/json",
            })
            try:
                resp = session.post(
                    self.config.deepseek_url,
                    json={
                        "model": self.config.deepseek_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 250,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = _get_deepseek_content(msg, "{}")
                    parsed = self._parse_json(content)
                    parsed["_agent"] = agent_name
                    parsed["_raw"] = content
                    parsed["_trade"] = trade_data
                    return parsed
                else:
                    logger.warning(f"🧠 Agent {agent_name} HTTP {resp.status_code}")
                    return {"_agent": agent_name, "_error": f"HTTP {resp.status_code}", "_trade": trade_data}
            except Exception as e:
                logger.warning(f"🧠 Agent {agent_name} 失败: {e}")
                return {"_agent": agent_name, "_error": str(e), "_trade": trade_data}
            finally:
                session.close()

        # 并行执行 3 个 Agent (v3.7: 加总体超时保护)
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_call_agent, name, sys_prompt, user_prompt): name
                for name, sys_prompt, user_prompt in agents
            }
            for future in as_completed(futures, timeout=45):  # 总体超时 45s
                try:
                    result = future.result(timeout=10)  # 单个结果超时 10s
                    results.append(result)
                except Exception as e:
                    agent_name = futures[future]
                    logger.warning(f"🧠 多智能体并行异常 [{agent_name}]: {e}")

        return results

    def synthesize_reflections(self, results: List[Dict]) -> dict:
        """
        综合 3 位 Agent 的诊断输出，生成统一洞察和结构化 IF-THEN 规则。

        Returns:
            {
                "diagnosis": 综合诊断文本,
                "actionable_rule": "IF ... AND ... THEN ...",
                "entry_good": bool, "direction_good": bool,
                "sl_appropriate": bool, "position_size_ok": bool,
                "avg_timing_score": float, "avg_fee_efficiency": float,
                "avg_confidence": float, "agent_count": int,
            }
        """
        if not results:
            return {}

        # ── 收集各 Agent 诊断文本 ──
        diagnoses: List[str] = []
        for r in results:
            agent = r.get("_agent", "?")
            diag = r.get("diagnosis", "")
            if diag and "_error" not in r:
                diagnoses.append(f"[{agent}] {diag}")
        combined_diagnosis = " | ".join(diagnoses) if diagnoses else "无有效诊断"

        # ── 提取结构化字段 ──
        entry_good = any(r.get("was_entry_good", False) for r in results)
        direction_good = any(r.get("was_direction_good", False) for r in results)
        sl_appropriate = any(r.get("sl_was_appropriate", False) for r in results)
        position_size_ok = any(r.get("position_size_ok", False) for r in results)

        timing_scores = [r.get("timing_score", 5) for r in results if "timing_score" in r]
        avg_timing = sum(timing_scores) / len(timing_scores) if timing_scores else 5.0

        fee_effs = [r.get("fee_efficiency", 0.5) for r in results if "fee_efficiency" in r]
        avg_fee_eff = sum(fee_effs) / len(fee_effs) if fee_effs else 0.5

        confidences = [r.get("confidence", 0) for r in results if "confidence" in r]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        # ── 提取交易上下文 (存在第一个有效结果中) ──
        trade_data = None
        for r in results:
            if "_trade" in r:
                trade_data = r["_trade"]
                break

        # ── 调用 DeepSeek 生成结构化 IF-THEN 规则 ──
        actionable_rule = self._generate_actionable_rule(diagnoses, trade_data)

        return {
            "diagnosis": combined_diagnosis,
            "actionable_rule": actionable_rule,
            "entry_good": entry_good,
            "direction_good": direction_good,
            "sl_appropriate": sl_appropriate,
            "position_size_ok": position_size_ok,
            "avg_timing_score": round(avg_timing, 1),
            "avg_fee_efficiency": round(avg_fee_eff, 2),
            "avg_confidence": round(avg_confidence, 2),
            "agent_count": sum(1 for r in results if "_error" not in r),
        }

    def _generate_actionable_rule(self, diagnoses: List[str],
                                   trade_data: Optional[Dict]) -> str:
        """
        基于多智能体诊断，调用 DeepSeek 生成结构化 IF-THEN 规则。

        规则模式:
          "IF market_regime=<regime> AND strategy=<strategy>
           AND direction=<direction> AND <condition> THEN <action>"

        动作类型:
          confidence_penalty=-N  降低信号置信度
          confidence_boost=+N   提升信号置信度
          skip_signal=true      跳过该信号
          reduce_position=true  减小仓位
          increase_sl=true      收紧止损
          tighten_tp=true       收紧止盈
        """
        if not trade_data or not self.config.deepseek_api_key:
            return ""

        symbol = trade_data.get("symbol", "?")
        direction = trade_data.get("direction", "?")
        strategy = trade_data.get("strategy", "?")
        pnl = trade_data.get("pnl", 0)
        pnl_pct = trade_data.get("pnl_pct", 0)

        diag_text = "\n".join(diagnoses) if diagnoses else "无诊断"

        prompt = f"""基于以下多智能体对一笔交易的联合诊断，提炼一条结构化 IF-THEN 规则。

交易: {symbol} {direction} {strategy} 盈亏{pnl:+.2f} USDT ({pnl_pct:+.2f}%)

多智能体诊断:
{diag_text}

请生成一条 IF-THEN 规则，格式严格遵循:
"IF market_regime=<regime> AND strategy=<strategy> AND direction=<direction> AND <condition> THEN <action>"

参考示例:
- IF market_regime=strong_trend AND strategy=pullback AND direction=SHORT AND adx>25 THEN confidence_penalty=-10
- IF market_regime=ranging AND strategy=momentum AND direction=LONG AND rsi<30 THEN confidence_boost=+15
- IF market_regime=weak_trend AND strategy=breakout AND direction=LONG AND volume_ratio<1.2 THEN skip_signal=true

动作类型: confidence_penalty, confidence_boost, skip_signal, reduce_position, increase_sl, tighten_tp

只返回原始规则文本一行，不要 JSON 包裹，不要额外解释。"""

        try:
            resp = self._session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system",
                         "content": "你是量化交易规则提炼专家。只输出 IF-THEN 格式的规则，不要额外解释，不要 JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 200,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                msg = resp.json()["choices"][0]["message"]
                content = _get_deepseek_content(msg, "")
                rule = content.strip().strip('"').strip("'")
                # 验证格式: 必须以 IF 开头且包含 THEN
                if rule.startswith("IF ") and " THEN " in rule:
                    logger.info(f"🧠 提炼规则: {rule}")
                    return rule
                # 尝试从多行或 JSON 中提取
                for line in content.split("\n"):
                    line = line.strip().strip('"').strip("'")
                    if line.startswith("IF ") and " THEN " in line:
                        logger.info(f"🧠 提炼规则: {line}")
                        return line
                logger.warning(f"🧠 规则格式不符 (期望 IF...THEN): {content[:120]}")
                # 尝试修复常见格式问题
                if "IF " in content and "THEN " in content:
                    start = content.index("IF ")
                    end = content.index("THEN ") + len("THEN ")
                    # 找到 THEN 后的动作结束位置 (换行或标点)
                    rest = content[end:]
                    action_end = len(rest)
                    for delim in ["\n", ".", "。", ","]:
                        idx = rest.find(delim)
                        if idx >= 0:
                            action_end = idx
                            break
                    rule = content[start:end + action_end].strip()
                    if rule.startswith("IF "):
                        logger.info(f"🧠 修复后规则: {rule}")
                        return rule
                return ""
            else:
                logger.warning(f"🧠 规则生成 HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"🧠 规则生成失败: {e}")
        return ""

    # ════════════════════════════════════════════
    # 交易后反思 (增强版)
    # ════════════════════════════════════════════

    def learn_from_closed_trade(
        self, symbol: str, direction: str, entry_price: float,
        exit_price: float, pnl: float, pnl_pct: float,
        strategy: str, ai_decision: str, reason: str = "",
        holding_hours: float = 0, market_regime: str = "",
    ) -> Optional[str]:
        """
        一笔交易平仓后，让 DeepSeek 反思。
        v2.0: 附带策略绩效上下文，让 AI 做更有洞察的分析。
        """
        if not self.config.deepseek_api_key:
            return None

        # 先记录到本地追踪器
        self.record_closed_trade(
            symbol=symbol, direction=direction, strategy=strategy,
            entry_price=entry_price, exit_price=exit_price,
            pnl=pnl, pnl_pct=pnl_pct,
            market_regime=market_regime,
        )

        # ── v2.1: Multi-Agent Reflection Loop ──
        trade_data = {
            "symbol": symbol, "direction": direction, "strategy": strategy,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": pnl_pct,
            "ai_decision": ai_decision, "holding_hours": holding_hours,
            "market_regime": market_regime,
        }

        # Primary: 3-agent parallel reflection
        agent_results = self.multi_agent_reflection(trade_data)
        if agent_results:
            synthesis = self.synthesize_reflections(agent_results)
            if synthesis.get("diagnosis"):
                entry = {
                    "date": now_str("%Y-%m-%d %H:%M"),
                    "source": f"multi_agent:{symbol} {direction} {strategy}",
                    "insight": synthesis.get("diagnosis", ""),
                    "quality": "good" if pnl > 0 else "bad",
                    "action": synthesis.get("actionable_rule", ""),
                    "actionable_rule": synthesis.get("actionable_rule", ""),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "agent_count": synthesis.get("agent_count", 0),
                    "avg_confidence": synthesis.get("avg_confidence", 0),
                    "avg_timing_score": synthesis.get("avg_timing_score", 0),
                    "symbol": symbol,
                    "direction": direction,
                    "strategy": strategy,
                    "market_regime": market_regime,
                }
                self._add_wisdom(entry)
                insight = synthesis.get("diagnosis", "")
                rule = synthesis.get("actionable_rule", "")
                logger.info(f"🧠 多智能体反思: {insight[:60]}")
                if rule:
                    logger.info(f"🧠 可操作规则: {rule}")
                return insight

        # Fallback: original single-call approach
        perf_ctx = self.get_strategy_context()

        prompt = f"""你是量化交易教练。分析以下已完成的交易，提炼一条可操作的教训。

交易详情:
- 币种: {symbol}  方向: {direction}  策略: {strategy}
- 入场: {entry_price:.4f}  出场: {exit_price:.4f}
- 盈亏: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)  持仓: {holding_hours:.1f}小时
- AI审核: {ai_decision}

{perf_ctx}

请提炼一条 60 字以内的中文教训。格式为 JSON:
{{"insight": "教训", "quality": "good|bad|neutral", "action": "具体的参数或策略调整建议"}}

例如:
- 盈利: {{"insight": "动量策略在BTC上表现突出，ADX>25时做空胜率高", "quality": "good", "action": "动量做空仓位可加大20%"}}
- 亏损: {{"insight": "BNB回调做多时RSI未触及深度超卖即反弹失败，需等待RSI<25", "quality": "bad", "action": "提高RSI超卖阈值至25"}}
"""

        try:
            resp = self._session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": "你是量化交易教练。基于真实交易数据提炼可操作教训。简洁、具体、能落地。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 300,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                msg = resp.json()["choices"][0]["message"]
                content = _get_deepseek_content(msg, "{}")
                result = self._parse_json(content)
                insight = result.get("insight", "")
                if insight:
                    entry = {
                        "date": now_str("%Y-%m-%d %H:%M"),
                        "source": f"trade:{symbol} {direction} {strategy}",
                        "insight": insight,
                        "quality": result.get("quality", "neutral"),
                        "action": result.get("action", ""),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    }
                    self._add_wisdom(entry)
                    logger.info(f"🧠 学会: {insight[:60]}")
                    return insight
            else:
                logger.warning(f"🧠 反思 HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"🧠 反思失败: {e}")
        return None

    # ════════════════════════════════════════════
    # 周期性回顾 (增强版)
    # ════════════════════════════════════════════

    def should_review(self) -> bool:
        return (time.time() - self._last_review) > self.REVIEW_INTERVAL_HOURS * 3600

    def should_emergency_review(self) -> bool:
        """v3.7: 连续亏损触发紧急回顾 — 冷却 30 分钟防抖"""
        if self._consecutive_loss_streak < self.EMERGENCY_REVIEW_LOSSES:
            return False
        return (time.time() - self._last_emergency_review) > self.EMERGENCY_COOLDOWN_MINUTES * 60

    def mark_emergency_review_done(self):
        """v3.7: 标记紧急回顾已执行，重置冷却计时"""
        self._last_emergency_review = time.time()

    def periodic_review(self, trades_data: List[Dict]) -> Optional[List[Dict]]:
        """
        周期性宏观回顾。
        v2.0: 附带策略追踪数据，让 DeepSeek 做精准诊断。
        """
        if not trades_data or not self.config.deepseek_api_key:
            return None

        self._last_review = time.time()

        # 策略绩效摘要
        perf_summary = self.get_strategy_context()

        # 最近交易
        summary_lines = [f"近期 {len(trades_data)} 笔交易:"]
        for t in trades_data[-30:]:
            t_time = t.get("timestamp", "?")[:16]
            summary_lines.append(
                f"  {t_time} {t.get('symbol','?')} {t.get('direction','?')} "
                f"盈亏{t.get('pnl',0):+.2f}"
            )

        wins = [t for t in trades_data if t.get("pnl", 0) > 0]
        losses = [t for t in trades_data if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in trades_data)
        win_rate = len(wins) / len(trades_data) * 100 if trades_data else 0
        stats = (
            f"\n统计: 胜率{win_rate:.0f}% ({len(wins)}赢/{len(losses)}输) "
            f"总盈亏{total_pnl:+.2f} USDT"
        )

        prompt = f"""你是量化策略分析师。基于以下交易数据，提出 2-3 条可操作改进。

{chr(10).join(summary_lines)}
{stats}

{perf_summary}

关注:
1. 哪些策略/方向/币种该加大投入？哪些该暂停？
2. 当前参数 (SL/TP/仓位) 是否有问题？
3. 有没有明显可优化的点？

返回 JSON 数组:
[{{"insight": "建议", "category": "filter|strategy|parameter|risk|allocation", "confidence": 0.0-1.0, "action": "具体操作"}}]
"""

        try:
            resp = self._session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": "你是量化策略分析师。基于实际数据提出可操作、可量化的改进建议。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 600,
                },
                timeout=45,
            )
            if resp.status_code == 200:
                msg = resp.json()["choices"][0]["message"]
                content = _get_deepseek_content(msg, "[]")
                suggestions = self._parse_json(content)
                if isinstance(suggestions, dict):
                    suggestions = [suggestions]
                if isinstance(suggestions, list) and suggestions:
                    logger.info(f"🧠 周期性回顾: {len(suggestions)} 条建议")
                    for s in suggestions:
                        if s.get("confidence", 0) >= 0.5:
                            self._add_wisdom({
                                "date": now_str("%Y-%m-%d %H:%M"),
                                "source": "periodic_review",
                                "insight": s.get("insight", ""),
                                "quality": "neutral",
                                "category": s.get("category", "strategy"),
                                "confidence": s.get("confidence", 0.5),
                                "action": s.get("action", ""),
                                "symbol": s.get("symbol", "global"),
                                "direction": s.get("direction", "global"),
                                "strategy": s.get("strategy", "global"),
                                "market_regime": s.get("market_regime", "global"),
                            })
                    return suggestions
        except Exception as e:
            logger.warning(f"🧠 周期回顾失败: {e}")
        return None

    # ════════════════════════════════════════════
    # v2.0: 动态参数自调
    # ════════════════════════════════════════════

    def _check_auto_param_tune(self):
        """检查是否需要自动调整参数"""
        recent = self.tracker.recent_trades
        if len(recent) < 5:
            return

        # 最近 5 笔交易
        last5 = recent[-5:]
        [t for t in last5 if t["pnl"] <= 0]
        wins = [t for t in last5 if t["pnl"] > 0]

        # 连续亏损 ≥ 3 → 收紧 SL (更快止损)
        if len(last5) >= 3 and all(t["pnl"] <= 0 for t in last5[-3:]):
            self._consecutive_loss_streak += 1
            if self._consecutive_loss_streak >= 3:
                self._tighten_stop_loss()
        else:
            self._consecutive_loss_streak = max(0, self._consecutive_loss_streak - 1)

        # 连续盈利 ≥ 4 → 放宽 TP (让利润跑)
        if len(wins) >= 4:
            self._widen_take_profit()

    def _tighten_stop_loss(self):
        """收紧止损：连续亏损时更快止损"""
        current = self.config.sl_atr_mult
        new_sl = max(0.8, current - 0.2)
        if new_sl < current:
            self.config.sl_atr_mult = new_sl
            logger.info(f"🧠 连续亏损 → SL收紧: {current}xATR → {new_sl}xATR")
            self._add_wisdom({
                "date": now_str("%Y-%m-%d %H:%M"),
                "source": "auto_tune:sl",
                "insight": f"连续亏损触发SL收紧: {current:.1f}→{new_sl:.1f}xATR",
                "quality": "neutral",
                "action": f"sl_atr_mult={new_sl}",
            })

    def _widen_take_profit(self):
        """放宽止盈：连续盈利时让利润跑更远"""
        if self.config.tp_atr_mults:
            current = self.config.tp_atr_mults[0]
            new_tp = min(4.0, current + 0.3)
            if new_tp > current:
                self.config.tp_atr_mults[0] = new_tp
                logger.info(f"🧠 连续盈利 → TP放宽: {current:.1f}xATR → {new_tp:.1f}xATR")
                self._add_wisdom({
                    "date": now_str("%Y-%m-%d %H:%M"),
                    "source": "auto_tune:tp",
                    "insight": f"连续盈利触发TP放宽: {current:.1f}→{new_tp:.1f}xATR",
                    "quality": "neutral",
                    "action": f"tp_atr_mult={new_tp}",
                })

    # ════════════════════════════════════════════
    # 过滤器分析 (保留但优化)
    # ════════════════════════════════════════════

    def analyze_filter_effectiveness(self, recent_cycles: int = 100) -> Optional[str]:
        """
        分析过滤条件是否过严。
        v2.0: 只分析最近 N 个周期的数据 (不碰旧日志)。
        """
        if not self.config.deepseek_api_key:
            return None

        cycles = []
        filter_stats = {"ADX_SKIP": 0, "VOL_SKIP": 0, "BTC_FILTER": 0, "TF_SKIP": 0, "CONCURRENT_SKIP": 0}
        try:
            with open(self.trades_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            for line in all_lines[-500:]:  # 只看最近500行
                try:
                    evt = json.loads(line)
                    if evt.get("event") == "CYCLE":
                        cycles.append(evt)
                    elif evt.get("event") in filter_stats:
                        filter_stats[evt["event"]] += 1
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)
        except Exception:
            return None

        cycles = cycles[-recent_cycles:]
        if not cycles or len(cycles) < 10:
            return None

        total = len(cycles)
        zero_signal = sum(1 for c in cycles if c.get("candidates", 0) == 0)
        zero_pct = zero_signal / total * 100

        # 只在 0 信号率 > 60% 且周期足够时触发 (v3.7: 放宽阈值)
        if zero_pct < 60 or total < 10:
            return None

        # 检查是否最近已经对同一问题诊断过（去重）
        recent_diags = [w for w in self._wisdom_store.entries[-5:]
                        if w.get("source") == "filter_analysis"]
        if len(recent_diags) >= 3:
            return None

        # ── v3.7: 统计快速诊断 (不耗 API) ──
        # 当无信号率极高 (>85%) 时，直接给出统计建议，跳过 DeepSeek
        if zero_pct > 85 and total >= 30:
            current_adx = self.config.adx_threshold
            suggested_adx = max(10, current_adx - 4)
            diagnosis = (
                f"ADX阈值{current_adx}与量比{self.config.vol_ratio_threshold}组合过严，"
                f"导致近{total}周期{zero_pct:.0f}%时间无信号触发。"
            )
            logger.info(f"🧠 [快速诊断] {diagnosis}")
            return diagnosis
        if zero_pct > 70 and total >= 20:
            # 中度干旱 → 统计建议
            current_adx = self.config.adx_threshold
            suggested_adx = max(10, current_adx - 2)
            diagnosis = (
                f"ADX与量比过滤过严，{zero_pct:.0f}%无信号，"
                f"建议降低阈值至{suggested_adx}和{max(0.5, self.config.vol_ratio_threshold - 0.3)}"
            )
            logger.info(f"🧠 [快速诊断] {diagnosis}")
            return diagnosis

        # ── AI 深度诊断 (仅在中等程度干旱时调用) ──
        if not self.config.deepseek_api_key:
            return None

        prompt = f"""量化策略参数诊断。最近 {total} 周期中 {zero_pct:.0f}% 无信号。

过滤统计: ADX={filter_stats['ADX_SKIP']}次 VOL={filter_stats['VOL_SKIP']}次
当前ADX阈值={self.config.adx_threshold} 量比={self.config.vol_ratio_threshold}

如果确实过滤过严，用一句中文建议（40字内）。如果数据不够判断，返回 {{"diagnosis": ""}}。
{{"diagnosis": "诊断", "suggested_action": "建议"}}
"""

        try:
            resp = self._session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": "量化参数优化专家。只在实际需要调整时才建议。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 200,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                msg = resp.json()["choices"][0]["message"]
                content = _get_deepseek_content(msg, "{}")
                result = self._parse_json(content)
                diagnosis = result.get("diagnosis", "")
                if diagnosis and len(diagnosis) > 5:
                    self._add_wisdom({
                        "date": now_str("%Y-%m-%d %H:%M"),
                        "source": "filter_analysis",
                        "insight": diagnosis,
                        "quality": "neutral",
                        "suggested_action": result.get("suggested_action", ""),
                    })
                    logger.info(f"🧠 过滤诊断: {diagnosis}")
                    self._consecutive_strict_diag += 1
                    return diagnosis
        except Exception as e:
            logger.warning(f"🧠 过滤分析失败: {e}")
        return None

    # ════════════════════════════════════════════
    # 公开 API
    # ════════════════════════════════════════════

    def get_wisdom_context(self, context: dict = None) -> str:
        """
        返回与当前上下文最相关的交易智慧，注入 AI system prompt。

        v2.1 (Layer 2): 基于 embedding_key 检索最相关的 wisdom 条目，
        只注入相关智慧，不注入全部。无匹配时退回最近的可操作条目。

        Args:
            context: dict with market_regime, symbol, strategy, direction keys
        """
        if not self._wisdom_store.entries:
            return ""

        if context:
            # ── Layer 2: 检索相关 wisdom ──
            relevant = self._wisdom_store.retrieve_relevant(context, top_k=3)
            if relevant:
                lines = ["\n## 历史交易智慧（与当前信号相关）"]
                for i, w in enumerate(relevant):
                    action_str = f" → {w.get('action','')}" if w.get('action') else ""
                    score_info = ""
                    if w.get("embedding_key"):
                        score_info = f" [key:{w['embedding_key']}]"
                    lines.append(
                        f"{i+1}. [{w.get('date','?')}] {w.get('insight','')}"
                        f"{action_str}{score_info}"
                    )
                return "\n".join(lines)

        # ── 降级: 无 context 或无匹配时，返回最近 actionable 条目 ──
        actionable = [w for w in self._wisdom_store.entries if w.get("quality") in ("good", "bad")]
        neutral = [w for w in self._wisdom_store.entries if w.get("quality") == "neutral"]
        selected = (actionable + neutral)[-5:]  # 最近5条

        if not selected:
            return ""

        lines = ["\n## 历史交易智慧（最近积累）"]
        for i, w in enumerate(selected):
            action_str = f" → {w.get('action','')}" if w.get('action') else ""
            key_hint = f" [key:{w.get('embedding_key','')}]" if w.get("embedding_key") else ""
            lines.append(
                f"{i+1}. [{w.get('date','?')}] {w.get('insight','')}"
                f"{action_str}{key_hint}"
            )
        return "\n".join(lines)

    def get_full_context(self, context: dict = None) -> str:
        """
        返回完整学习上下文：相关智慧 + 策略绩效。

        Args:
            context: dict with market_regime, symbol, strategy, direction keys
                     passed through to get_wisdom_context for relevance retrieval
        """
        wisdom = self.get_wisdom_context(context)
        strategy = self.get_strategy_context()
        parts = [p for p in [wisdom, strategy] if p]
        return "\n".join(parts)

    def get_top_actions(self, n: int = 3) -> List[str]:
        """返回最 actionable 的建议"""
        actions = []
        for w in reversed(self._wisdom_store.entries):
            if w.get("action") and w.get("action") not in actions:
                actions.append(w["action"])
            if len(actions) >= n:
                break
        return actions

    # ════════════════════════════════════════════
    # 智慧管理 (v2.0: 增强去重)
    # ════════════════════════════════════════════

    def _add_wisdom(self, entry: Dict[str, Any]):
        """添加智慧，v3.7增强去重：关键词重叠 + 长度自适应阈值 + 来源频率限制"""
        new_text = entry.get("insight", "")
        if not new_text:
            return

        source = entry.get("source", "unknown")

        # 去重检查：三层过滤
        for w in self._wisdom_store.entries:
            existing = w.get("insight", "")
            if not existing:
                continue
            # 方法1: 完全相同
            if existing == new_text:
                return
            # 方法2: 前30字符相同 (防止微调重发)
            if len(existing) > 30 and existing[:30] == new_text[:30]:
                return
            # 方法3: 关键词重叠度 + 长度自适应阈值
            # 短文本(≤30字) → 阈值0.8; 中等(30-80字) → 0.65; 长文本(>80字) → 0.5
            text_len = min(len(new_text), len(existing))
            if text_len <= 30:
                threshold = 0.80
            elif text_len <= 80:
                threshold = 0.65
            else:
                threshold = 0.50
            overlap = self._keyword_overlap(new_text, existing)
            if overlap > threshold:
                logger.debug(f"🧠 去重拦截: overlap={overlap:.2f} > {threshold}")
                return

        # 来源频率限制: 同一来源连续3条 → 合并而非新增
        same_source = [w for w in self._wisdom_store.entries[-5:]
                       if w.get("source") == source]
        if len(same_source) >= 3:
            # 用最新一条替换最旧一条同源条目
            for i, w in enumerate(self._wisdom_store.entries):
                if w.get("source") == source:
                    self._wisdom_store.entries[i] = entry
                    self._save_wisdom()
                    return

        # v2.1: 通过 WisdomStore.add() 自动生成 embedding_key
        self._wisdom_store.add(entry)
        self._save_wisdom()

    @staticmethod
    def _keyword_overlap(a: str, b: str) -> float:
        """计算两段文本的关键词重叠度 (v3.7: 基于中文分词特征)。
        用 2-gram 字符 + 4-gram 字符混合计算，兼顾短词和长词。
        """
        def ngrams(s, n):
            return {s[i:i+n] for i in range(len(s)-n+1)}
        # 2-gram (短词) + 4-gram (长词/短语)
        a_set = ngrams(a, 2) | ngrams(a, 4)
        b_set = ngrams(b, 2) | ngrams(b, 4)
        if not a_set or not b_set:
            return 0.0
        return len(a_set & b_set) / min(len(a_set), len(b_set))

    def purge_repetitive(self):
        """清理重复/低质量智慧条目"""
        before = len(self._wisdom_store.entries)
        # 移除 filter_analysis 来源超过 5 条的旧条目
        fa_entries = [i for i, w in enumerate(self._wisdom_store.entries)
                      if w.get("source") == "filter_analysis"]
        if len(fa_entries) > 5:
            to_remove = sorted(fa_entries)[:len(fa_entries) - 5]
            for i in reversed(to_remove):
                self._wisdom_store.entries.pop(i)
        after = len(self._wisdom_store.entries)
        if before != after:
            logger.info(f"🧠 清理 {before-after} 条重复智慧")
            self._save_wisdom()

    # ════════════════════════════════════════════
    # 持久化
    # ════════════════════════════════════════════

    def _save_wisdom(self):
        """持久化 wisdom 到磁盘（委托给 WisdomStore）"""
        self._wisdom_store.save(self.wisdom_path)

    def _load_wisdom(self):
        """从磁盘加载 wisdom（委托给 WisdomStore，自动补全 embedding_key）"""
        self._wisdom_store.load(self.wisdom_path)

    def _save_tracker(self):
        try:
            tmp = self.tracker_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.tracker.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.tracker_path)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

    def _load_tracker(self):
        if not os.path.exists(self.tracker_path):
            return
        try:
            with open(self.tracker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tracker.load_from(data)
            logger.info(f"🧠 已加载策略绩效: {len(self.tracker.recent_trades)} 笔历史交易")
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

    @staticmethod
    def _parse_json(raw: str) -> Any:
        try:
            return json.loads(raw)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
        for m in re.finditer(r'```(?:json)?\s*\n?(.*?)```', raw, re.DOTALL):
            try:
                return json.loads(m.group(1).strip())
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)
        for m in re.finditer(r'(\[.*\]|\{.*\})', raw, re.DOTALL):
            try:
                return json.loads(m.group(0))
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)
        return {}

    # ════════════════════════════════════════════
    # v3.6: ① 信号率自适应调参
    # ════════════════════════════════════════════

    def adaptive_signal_rate_tune(self, cycle_history: list) -> Optional[str]:
        """v3.7: 非对称自适应调参 — ADX + 量比。

        下调快、上调慢，避免过度收紧：
        - 信号率 < 5%: ADX -3 + vol_ratio -0.2 (急放)
        - 信号率 < 10%: ADX -2 (快放)
        - 信号率 < 15%: ADX -1 (微放)
        - 信号率 > 30% 且胜率 < 40%: ADX +1 (慢收)
        - 信号率 > 40% 且胜率 < 35%: ADX +2 + vol_ratio +0.1 (收紧)
        所有参数有 floor/ceiling 保护。
        """
        if len(cycle_history) < 20:
            return None

        recent = cycle_history[-50:]
        total = len(recent)
        with_signal = sum(1 for c in recent if c.get("candidates", 0) > 0)
        signal_rate = with_signal / total if total > 0 else 0

        # 计算近期胜率
        recent_trades = [t for t in self.tracker.recent_trades[-30:]]
        if recent_trades:
            wins = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
            win_rate = wins / len(recent_trades)
        else:
            win_rate = 0.5

        messages = []
        current_adx = self.config.adx_threshold

        # ── 下调路径 (信号太少 → 放宽) ──
        if signal_rate < 0.05 and total >= 20:
            # 严重干旱: 急放 ADX + 量比 (v4.0: 降低触发门槛)
            new_adx = max(8, current_adx - 3)
            if new_adx != current_adx:
                messages.append(f"ADX {current_adx}→{new_adx}")
                self.config.adx_threshold = new_adx
            old_vol = self.config.vol_ratio_threshold
            new_vol = round(max(0.5, old_vol - 0.2), 1)
            if new_vol != old_vol:
                messages.append(f"量比 {old_vol}→{new_vol}")
                self.config.vol_ratio_threshold = new_vol
        elif signal_rate < 0.10 and total >= 15:
            # v4.0: 加速下调 — 信号持续偏低时更快响应
            new_adx = max(8, current_adx - 2)
            if new_adx != current_adx:
                messages.append(f"ADX {current_adx}→{new_adx}")
                self.config.adx_threshold = new_adx
        elif signal_rate < 0.15 and total >= 15:
            new_adx = max(8, current_adx - 2)  # v4.0: -2 instead of -1
            if new_adx != current_adx:
                messages.append(f"ADX {current_adx}→{new_adx}")
                self.config.adx_threshold = new_adx

        # ── 上调路径 (信号太多但质量差 → 收紧) ──
        if signal_rate > 0.40 and win_rate < 0.35:
            new_adx = min(25, current_adx + 2)
            if new_adx != current_adx:
                messages.append(f"ADX {current_adx}→{new_adx}")
                self.config.adx_threshold = new_adx
            old_vol = self.config.vol_ratio_threshold
            new_vol = round(min(2.0, old_vol + 0.1), 1)
            if new_vol != old_vol:
                messages.append(f"量比 {old_vol}→{new_vol}")
                self.config.vol_ratio_threshold = new_vol
        elif signal_rate > 0.30 and win_rate < 0.40:
            new_adx = min(25, current_adx + 1)
            if new_adx != current_adx:
                messages.append(f"ADX {current_adx}→{new_adx}")
                self.config.adx_threshold = new_adx

        if messages:
            msg = f"信号率{signal_rate*100:.0f}% 胜率{win_rate*100:.0f}% → {', '.join(messages)}"
            logger.info(f"🧠 [自适应] {msg}")
            return msg
        return None

    # ════════════════════════════════════════════
    # v3.6: ② 退出质量分析 (反事实回测)
    # ════════════════════════════════════════════

    def analyze_exit_quality(self, trade: dict) -> dict:
        """
        对一笔已完成的交易做反事实分析。
        返回: {tp_optimal: float, sl_optimal: float, gain_if_better_tp: float, ...}
        """
        entry = trade.get("entry_price", 0)
        exit_p = trade.get("exit_price", 0)
        direction = trade.get("direction", "SHORT")
        pnl = trade.get("pnl", 0)

        if entry <= 0 or exit_p <= 0:
            return {}

        move_pct = abs(exit_p - entry) / entry
        is_win = pnl > 0

        result = {
            "is_win": is_win,
            "move_pct": round(move_pct * 100, 2),
            "direction": direction,
        }

        # 盈利交易: 是否止盈太早？
        if is_win:
            # 估算: 如果方向对，move 越大说明可能可以拿更多
            # SHORT 盈利 = entry > exit，move = (entry-exit)/entry
            profit_move = (entry - exit_p) / entry if direction == "SHORT" else (exit_p - entry) / entry
            result["profit_move_pct"] = round(profit_move * 100, 2)
            result["assessment"] = (
                "止盈偏早" if profit_move < 0.02 else
                "止盈适中" if profit_move < 0.05 else
                "止盈良好"
            )
        else:
            # 亏损交易: SL 是否太宽？
            loss_move = (exit_p - entry) / entry if direction == "SHORT" else (entry - exit_p) / entry
            result["loss_move_pct"] = round(loss_move * 100, 2)
            result["assessment"] = (
                "止损过宽" if loss_move > 0.05 else
                "止损偏宽" if loss_move > 0.03 else
                "止损合理"
            )

        return result

    def periodic_exit_optimization(self):
        """
        周期性汇总退出质量，微调 SL/TP 参数。
        每 REVIEW_INTERVAL_HOURS 调用一次。
        """
        trades = self.tracker.recent_trades[-30:]
        if len(trades) < 10:
            return None

        profits = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]

        adjustments = []

        # TP 调整: 如果盈利交易 moves 都很小 (<2%)，说明 TP 可能偏早
        if profits:
            avg_profit_move = sum(
                abs(t.get("exit_price", 0) - t.get("entry_price", 0)) / t.get("entry_price", 1)
                for t in profits
            ) / len(profits)
            if avg_profit_move < 0.02:
                # 扩大 TP
                old = self.config.tp_atr_mults
                new_mults = [min(5.0, m * 1.2) for m in old]
                self.config.tp_atr_mults = new_mults
                adjustments.append(f"TP {old}→{new_mults} (盈利move={avg_profit_move*100:.1f}%)")

        # SL 调整: 如果亏损交易 moves 都很大 (>5%)，且胜率低
        if losses:
            avg_loss_move = sum(
                abs(t.get("exit_price", 0) - t.get("entry_price", 0)) / t.get("entry_price", 1)
                for t in losses
            ) / len(losses)
            loss_rate = len(losses) / (len(profits) + len(losses))
            if avg_loss_move > 0.05 and loss_rate > 0.4:
                old_sl = self.config.sl_atr_mult
                self.config.sl_atr_mult = max(0.8, old_sl * 0.85)  # 收紧 SL
                adjustments.append(f"SL {old_sl}→{self.config.sl_atr_mult:.1f} (亏损move={avg_loss_move*100:.1f}%)")

        if adjustments:
            msg = "; ".join(adjustments)
            logger.info(f"🧠 [退出优化] {msg}")
            return msg
        return None

    # ════════════════════════════════════════════
    # v3.6: ③ Kelly 仓位分配
    # ════════════════════════════════════════════

    def get_kelly_multiplier(self, strategy: str = "pullback", direction: str = "SHORT") -> float:
        """
        基于策略绩效计算 Kelly 仓位系数。
        f = (bp - q) / b, 其中 b = avg_win/abs(avg_loss), p = win_rate, q = 1-p
        返回 fractional Kelly (0.25x)，限制在 [0.3, 1.5]。
        无数据时返回 1.0 (不调整)。
        """
        s = self.tracker.strategies.get(strategy, {})
        self.tracker.directions.get(direction, {})

        # 合并策略+方向数据
        total_wins = s.get("wins", 0)
        total_losses = s.get("losses", 0)
        total = total_wins + total_losses

        if total < 5:
            return 1.0  # 数据不足，默认仓位

        win_rate = total_wins / total
        avg_win = s.get("avg_win", 0)
        avg_loss = abs(s.get("avg_loss", 1))
        if avg_loss <= 0:
            return 1.0

        b_ratio = avg_win / avg_loss  # 盈亏比

        # Kelly 公式
        kelly = (b_ratio * win_rate - (1 - win_rate)) / b_ratio if b_ratio > 0 else 0

        # Fractional Kelly: 0.25x
        fractional = kelly * 0.25

        # 限制范围
        clamped = max(0.3, min(1.5, fractional)) if kelly > 0 else 0.5

        if total >= 10:
            logger.debug(
                f"🧠 [Kelly] {strategy}/{direction}: "
                f"胜率={win_rate:.0%} 盈亏比={b_ratio:.1f} kelly={kelly:.2f} final={clamped:.2f}"
            )

        return clamped

    def close(self):
        try:
            self._session.close()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
