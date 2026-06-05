#!/usr/bin/env python3
"""
portfolio_manager.py — 投资组合层面风险管理器 v2.0
====================================================
v2.0 新增: 方向感知动量 / N×N 币种间相关性 / 排名仓位系数 / 细分板块 / 组合 VaR

用法（由 DeepSeekQuantBot 调用）:
  from portfolio_manager import PortfolioManager
  pm = PortfolioManager(config, exchange, tlogger)
  pm.ensure_correlations_fresh(symbols)
  ranked = pm.rank_candidates(candidates, tf_contexts)
  multiplier = pm.get_position_multiplier(rank, total)
  var_warning = pm.check_portfolio_var(positions, equity)
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from time_utils import now_iso

logger = logging.getLogger("QuantBot")


class PortfolioManager:
    """投资组合层面的风险管理和信号排名 v2.0"""

    CACHE_FILE = "correlation_cache.json"

    # v2.0: 细分板块分类
    SECTOR_MAP = {
        # 智能合约平台
        "BTC": "StoreOfValue",
        "ETH": "SmartContract",
        "SOL": "SmartContract",
        "BNB": "SmartContract",
        # 传统 L1
        "ADA": "LegacyL1",
        "DOT": "LegacyL1",
        "LTC": "LegacyL1",
        # DeFi
        "LINK": "Oracle",
        "UNI": "DeFi",
        "AAVE": "DeFi",
        "MKR": "DeFi",
        # 支付
        "XRP": "Payments",
        # Meme
        "DOGE": "Meme",
        # AI
        "FET": "AI",
        "RENDER": "AI",
    }

    # v2.0: 板块风险权重 (用于 VaR 和仓位调整)
    SECTOR_RISK = {
        "StoreOfValue": 0.7,
        "SmartContract": 1.0,
        "LegacyL1": 1.2,
        "DeFi": 1.3,
        "Oracle": 1.1,
        "Payments": 0.9,
        "Meme": 1.5,
        "AI": 1.4,
        "Other": 1.0,
    }

    def __init__(self, config, exchange, tlogger):
        self.config = config
        self.exchange = exchange
        self.tlogger = tlogger

        # v2.0: 从 config.symbol_configs 合并 sector 映射
        self._sector_map = dict(self.SECTOR_MAP)  # 基础映射 (类级别常量)
        if hasattr(config, 'symbol_configs'):
            for base, cfg in config.symbol_configs.items():
                if cfg.get("sector"):
                    self._sector_map[base] = cfg["sector"]

        # BTC 相关性: {base: {"BTC": float}}
        self.correlations: Dict[str, Dict[str, float]] = {}
        # v2.0: 币种间相关性矩阵: {(base1, base2): float}
        self.cross_correlations: Dict[Tuple[str, str], float] = {}
        self._last_correlation_update: float = 0.0

        self._cache_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), self.CACHE_FILE
        )
        self._load_cache()

    # ════════════════════════════════════════════
    # 相关性矩阵 (v2.0: BTC + 币种间)
    # ════════════════════════════════════════════

    def ensure_correlations_fresh(self, symbols: List[str]):
        """刷新 BTC 相关性 + v2.0 币种间相关性"""
        ttl = self.config.correlation_window_hours * 3600
        if (time.time() - self._last_correlation_update) < ttl and self.correlations:
            return

        logger.info("📊 正在刷新相关性矩阵 (BTC + 币种间) ...")
        btc_symbol = "BTC/USDT:USDT"
        # 获取所有币种的收益率序列 (一次 fetch, 复用)
        returns_cache: Dict[str, pd.Series] = {}
        bases = [s.replace("/USDT:USDT", "") for s in symbols]
        updated = 0

        for sym in symbols:
            base = sym.replace("/USDT:USDT", "")
            if base == "BTC":
                self.correlations["BTC"] = {"BTC": 1.0}
                # 获取 BTC 收益率供交叉相关使用
                try:
                    df = self.exchange.fetch_ohlcv_tf(sym, "1h", limit=self.config.correlation_window_hours + 1)
                    returns_cache[base] = df["close"].pct_change().dropna()
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)
                continue

            try:
                corr, rets = self._compute_correlation_with_returns(sym, btc_symbol)
                self.correlations[base] = {"BTC": round(corr, 3)}
                if rets is not None:
                    returns_cache[base] = rets
                updated += 1
            except Exception as e:
                logger.debug(f"📊 {base} 相关性计算失败: {e}")
                old = self.correlations.get(base, {}).get("BTC")
                self.correlations[base] = {"BTC": old if old is not None else 0.7}

        # v2.0: 计算币种间相关性 (BTC 外的配对)
        base_list = [b for b in bases if b in returns_cache]
        for i in range(len(base_list)):
            for j in range(i + 1, len(base_list)):
                b1, b2 = base_list[i], base_list[j]
                try:
                    r1 = returns_cache[b1]
                    r2 = returns_cache[b2]
                    merged = pd.concat([r1, r2], axis=1, join="inner").dropna()
                    if len(merged) >= 10:
                        corr = merged.iloc[:, 0].corr(merged.iloc[:, 1])
                        if not np.isnan(corr):
                            self.cross_correlations[(b1, b2)] = round(corr, 3)
                            self.cross_correlations[(b2, b1)] = round(corr, 3)
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)

        self._last_correlation_update = time.time()
        self._save_cache()
        logger.info(
            f"📊 相关性矩阵已更新: {updated} BTC对, "
            f"{len(self.cross_correlations)//2} 币种间对"
        )

    def _compute_correlation_with_returns(
        self, sym1: str, sym2: str
    ) -> Tuple[float, Optional[pd.Series]]:
        """计算两个币种 1h 收益率的相关性，同时返回收益率序列"""
        lookback = self.config.correlation_window_hours

        df1 = self.exchange.fetch_ohlcv_tf(sym1, "1h", limit=lookback + 1)
        df2 = self.exchange.fetch_ohlcv_tf(sym2, "1h", limit=lookback + 1)

        returns1 = df1["close"].pct_change().dropna()
        returns2 = df2["close"].pct_change().dropna()

        merged = pd.concat([returns1, returns2], axis=1, join="inner").dropna()
        if len(merged) < max(10, lookback // 3):
            return 0.0, returns1 if len(returns1) > 0 else None

        corr = merged.iloc[:, 0].corr(merged.iloc[:, 1])
        return (corr if not np.isnan(corr) else 0.0), returns1

    def get_btc_correlation(self, symbol: str) -> float:
        """返回某币种与 BTC 的相关性"""
        base = symbol.replace("/USDT:USDT", "")
        return self.correlations.get(base, {}).get("BTC", 0.7)

    def get_pair_correlation(self, sym1: str, sym2: str) -> float:
        """v2.0: 返回两个币种之间的相关性"""
        b1 = sym1.replace("/USDT:USDT", "")
        b2 = sym2.replace("/USDT:USDT", "")
        if b1 == b2:
            return 1.0
        return self.cross_correlations.get((b1, b2), 0.5)

    # ════════════════════════════════════════════
    # 信号排名 (v2.0: 方向感知动量)
    # ════════════════════════════════════════════

    def rank_candidates(
        self,
        candidates: List[Dict[str, Any]],
        tf_contexts: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        按综合强度评分对候选信号排序（降序）。
        v2.0: 动量项按信号方向翻转
        评分 = zscore(momentum) * 0.3 + zscore(volume) * 0.3 + zscore(trend) * 0.4
        """
        if len(candidates) <= 1:
            for c in candidates:
                c["_score"] = 0.0
                c["_rank"] = 1
            return candidates

        scores = []
        for cand in candidates:
            tf_ctx = tf_contexts.get(cand["symbol"], {})
            score = self._compute_strength_score(cand, tf_ctx)
            scores.append(score)

        mean_s = np.mean(scores) if scores else 0
        std_s = np.std(scores) if scores and np.std(scores) > 0 else 1.0

        for i, cand in enumerate(candidates):
            cand["_score"] = round((scores[i] - mean_s) / std_s, 3)

        ranked = sorted(candidates, key=lambda c: c.get("_score", 0), reverse=True)

        # v2.0: 标注排名
        for i, cand in enumerate(ranked):
            cand["_rank"] = i + 1

        return ranked

    def _compute_strength_score(
        self, cand: Dict[str, Any], tf_ctx: Dict[str, Any]
    ) -> float:
        """v2.0: 方向感知动量评分"""
        price = cand.get("price", 0)
        ema = cand.get("ema", price) if price > 0 else 1
        direction = cand.get("direction", "LONG")

        # v2.0: 动量项按方向翻转 — SHORT 信号在下跌趋势中加分
        if ema > 0 and price > 0:
            raw_momentum = price / ema - 1.0
            if direction == "SHORT":
                momentum = -raw_momentum  # 下跌→正值→加分
            else:
                momentum = raw_momentum   # 上涨→正值→加分
        else:
            momentum = 0

        # 成交量因子
        vol_factor = cand.get("vol_ratio", 1.0)

        # 趋势一致性因子
        trend_alignment = 0
        for tf_name in self.config.higher_timeframes:
            ctx = tf_ctx.get(tf_name, {})
            trend = ctx.get("trend", "unknown")
            if (direction == "LONG" and trend == "bullish") or (
                direction == "SHORT" and trend == "bearish"
            ):
                trend_alignment += 1
            elif trend == "unknown":
                trend_alignment += 0.5

        # ADX 因子 (趋势越强分越高)
        adx_value = cand.get("adx", 20)
        adx_factor = min(adx_value, 40) / 40.0

        score = (
            momentum * 0.30
            + vol_factor * 0.15
            + trend_alignment * 0.30
            + adx_factor * 0.25
        )
        return score

    # ════════════════════════════════════════════
    # v2.0: 排名 → 仓位系数
    # ════════════════════════════════════════════

    def get_position_multiplier(self, rank: int, total: int) -> float:
        """
        根据排名返回仓位系数。
        #1: 1.25x, #2: 1.10x, #3: 1.00x, #4+: 0.85x, 末尾: 0.65x
        """
        if total <= 1:
            return 1.0
        ratio = (rank - 1) / max(total - 1, 1)  # 0.0 = 最优, 1.0 = 最差

        if rank == 1:
            return 1.25
        elif rank == 2:
            return 1.10
        elif rank == 3:
            return 1.00
        elif ratio < 0.5:
            return 0.85
        elif ratio < 0.75:
            return 0.75
        else:
            return 0.65

    # ════════════════════════════════════════════
    # v2.0: 组合 VaR
    # ════════════════════════════════════════════

    def check_portfolio_var(
        self,
        positions: List[Dict],
        equity: float,
    ) -> Dict[str, Any]:
        """
        计算当前持仓组合的历史 VaR (95% 置信度)。
        使用各币种最近 24h 收益率数据估算。

        Returns:
          - var_pct: 组合 VaR 占权益百分比
          - warning: 是否触发告警 (>8%)
          - detail: 描述
        """
        if not positions or equity <= 0:
            return {"var_pct": 0, "warning": False, "detail": ""}

        # 收集各持仓收益率序列
        pos_returns = []
        weights = []
        total_weight = 0.0

        for p in positions:
            sym = p.get("symbol", "")
            sym_full = f"{sym}/USDT:USDT"
            margin = abs(p.get("margin", 0))
            if margin <= 0:
                continue

            try:
                df = self.exchange.fetch_ohlcv_tf(sym_full, "15m", limit=96)  # 24h
                rets = df["close"].pct_change().dropna().tolist()
                if len(rets) >= 20:
                    pos_returns.append(rets)
                    weights.append(margin)
                    total_weight += margin
            except Exception:
                continue

        if not pos_returns or total_weight <= 0:
            return {"var_pct": 0, "warning": False, "detail": ""}

        # 归一化权重
        weights = [w / total_weight for w in weights]

        # 组合收益率 = 加权平均
        min_len = min(len(r) for r in pos_returns)
        portfolio_rets = []
        for i in range(min_len):
            weighted_ret = sum(
                weights[j] * pos_returns[j][i] for j in range(len(pos_returns))
            )
            portfolio_rets.append(weighted_ret)

        # 历史 VaR (95%)
        var_95 = abs(np.percentile(portfolio_rets, 5))
        var_pct = round(var_95 * 100, 2)

        warning = var_pct > 8.0
        detail = (
            f"组合VaR(95%)={var_pct}%"
            + (" ⚠️高风险" if warning else " ✅正常")
            + f" (仓位{len(positions)}个)"
        )

        return {"var_pct": var_pct, "warning": warning, "detail": detail}

    # ════════════════════════════════════════════
    # 限制检查
    # ════════════════════════════════════════════

    def check_correlation_limit(
        self, new_symbol: str, existing_positions: List[str]
    ) -> Tuple[bool, str]:
        """检查新开仓是否会超过高相关性持仓上限"""
        new_base = new_symbol.replace("/USDT:USDT", "")
        new_corr = self.get_btc_correlation(new_symbol)

        if new_corr < self.config.correlation_threshold:
            return True, ""

        high_corr_count = 0
        for pos_base in existing_positions:
            pos_corr = self.correlations.get(pos_base, {}).get("BTC", 0.7)
            if pos_corr >= self.config.correlation_threshold:
                high_corr_count += 1

        if high_corr_count >= self.config.max_correlated_positions:
            return (
                False,
                f"{new_base} BTC相关性={new_corr:.2f}，"
                f"已有 {high_corr_count} 个高相关持仓（上限 {self.config.max_correlated_positions}）",
            )
        return True, ""

    def check_sector_exposure(
        self, new_symbol: str, existing_positions: List[str]
    ) -> Tuple[bool, str]:
        """v2.0: 板块暴露检查，使用细分板块 + 板块风险权重"""
        new_base = new_symbol.replace("/USDT:USDT", "")
        new_sector = self._sector_map.get(new_base, "Other")

        sector_count = 0
        for pos_base in existing_positions:
            if self._sector_map.get(pos_base, "Other") == new_sector:
                sector_count += 1

        # v2.0: 高风险板块 (Meme, AI) 更严格的限制
        limit = self.config.sector_max_positions
        risk_mult = self.SECTOR_RISK.get(new_sector, 1.0)
        if risk_mult >= 1.4:
            limit = max(1, limit - 1)  # 高风险板块减 1 个仓位上限

        if sector_count >= limit:
            return (
                False,
                f"{new_base} 板块={new_sector} (风险{risk_mult:.1f}x)，"
                f"已有 {sector_count} 个同板块持仓（上限 {limit}）",
            )
        return True, ""

    def get_diversification_score(self, symbols: List[str]) -> float:
        """v2.0: 增强分散化评分，含币种间相关性"""
        if len(symbols) <= 1:
            return 1.0

        sectors = set()
        total_corr = 0.0
        pairs = 0

        for i, s1 in enumerate(symbols):
            sectors.add(self._sector_map.get(s1, "Other"))
            for s2 in symbols[i + 1:]:
                # v2.0: 优先使用币种间相关性，回退 BTC 相关性
                pair_corr = self.cross_correlations.get((s1, s2))
                if pair_corr is not None:
                    total_corr += abs(pair_corr)
                else:
                    total_corr += abs(
                        self.correlations.get(s1, {}).get("BTC", 0.7)
                    )
                pairs += 1

        sector_score = min(1.0, len(sectors) / len(symbols))
        corr_score = 1.0 - (total_corr / max(pairs, 1))
        return round((sector_score * 0.4 + corr_score * 0.6), 2)

    def get_portfolio_summary(self, symbols: List[str]) -> str:
        """v2.0: 生成组合摘要 (供 AI prompt 注入)"""
        diversity = self.get_diversification_score(symbols)
        sectors = set(self._sector_map.get(s, "Other") for s in symbols)
        avg_corr = sum(
            abs(self.correlations.get(s, {}).get("BTC", 0.7)) for s in symbols
        ) / max(len(symbols), 1)

        return (
            f"组合: {len(symbols)}币种 | 板块={','.join(sorted(sectors))} | "
            f"分散度={diversity:.2f} | 平均BTC相关性={avg_corr:.2f}"
        )

    # ════════════════════════════════════════════
    # 持久化 (v2.0: 简化原子写)
    # ════════════════════════════════════════════

    def _save_cache(self):
        """保存相关性矩阵到磁盘"""
        try:
            # v2.0: 转换 tuple key → string key for JSON
            cross = {
                f"{k[0]}|{k[1]}": v
                for k, v in self.cross_correlations.items()
                if k[0] < k[1]  # 只存一半 (去重)
            }
            data = {
                "correlations": self.correlations,
                "cross_correlations": cross,
                "updated_at": now_iso(),
            }
            tmp_path = self._cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._cache_path)
        except Exception as e:
            logger.debug(f"保存相关性缓存失败: {e}")

    def _load_cache(self):
        """从磁盘加载相关性矩阵"""
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.correlations = data.get("correlations", {})
            # v2.0: 恢复币种间相关性
            cross = data.get("cross_correlations", {})
            for key, val in cross.items():
                parts = key.split("|")
                if len(parts) == 2:
                    self.cross_correlations[(parts[0], parts[1])] = val
                    self.cross_correlations[(parts[1], parts[0])] = val
            self._last_correlation_update = (
                time.time() - self.config.correlation_window_hours * 3600
            )
            logger.debug(
                f"📊 已加载相关性缓存 (BTC:{len(self.correlations)}对, "
                f"交叉:{len(cross)}对)"
            )
        except Exception:
            self.correlations = {}
            self.cross_correlations = {}
