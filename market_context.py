#!/usr/bin/env python3
"""
market_context.py — 市场情绪与新闻管理器 (Phase 1 提取自 deepseek_quant_bot.py)

数据源:
  - CoinGecko /search/trending (免费, 无需 API Key)
  - alternative.me Fear & Greed Index (免费)

缓存策略: 每 N 小时刷新一次 (默认4小时)，注入 DeepSeek system prompt
"""

import json
import logging
import time

import requests

logger = logging.getLogger("QuantBot")


class MarketContextManager:
    """
    市场情绪与新闻管理器。

    数据源:
      - CoinGecko /search/trending (免费, 无需 API Key)
      - alternative.me Fear & Greed Index (免费)

    缓存策略: 每 N 小时刷新一次 (默认4小时)，注入 DeepSeek system prompt
    """

    def __init__(self, cache_ttl: int = 14400):
        self.trending_cache: list = []
        self._trending_details: list = []
        self.fng_cache: dict = {}
        self.last_refresh: float = 0.0
        self.cache_ttl: int = cache_ttl
        self._initialized = False

    def _should_refresh(self) -> bool:
        return (time.time() - self.last_refresh) > self.cache_ttl

    def ensure_fresh(self):
        """确保缓存是最新的（如果需要则刷新）"""
        if self._should_refresh() or not self._initialized:
            self.refresh_all()

    def refresh_all(self):
        """刷新所有缓存数据"""
        self._fetch_trending()
        self._fetch_fng()
        self.last_refresh = time.time()
        self._initialized = True
        logger.info(
            f"市场情绪数据已刷新 "
            f"(热门币种={len(self.trending_cache)}个, "
            f"F&G={self.fng_cache.get('value', 'N/A')})"
        )

    def _fetch_trending(self):
        """获取 CoinGecko 热门币种"""
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/search/trending",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                coins = data.get("coins", [])
                self.trending_cache = [
                    c.get("item", {}).get("name", "")
                    for c in coins[:15]
                    if c.get("item", {}).get("name")
                ]
                self._trending_details = [
                    {
                        "name": c.get("item", {}).get("name", ""),
                        "symbol": c.get("item", {}).get("symbol", ""),
                        "market_cap_rank": c.get("item", {}).get("market_cap_rank", ""),
                        "score": c.get("item", {}).get("score", ""),
                    }
                    for c in coins[:10]
                ]
            else:
                logger.warning(f"CoinGecko trending 返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"获取 trending 失败: {e}")

    def _fetch_fng(self):
        """获取恐惧与贪婪指数"""
        try:
            resp = requests.get(
                "https://api.alternative.me/fng/",
                params={"limit": 1},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data"):
                    entry = data["data"][0]
                    self.fng_cache = {
                        "value": int(entry.get("value", 50)),
                        "classification": entry.get("value_classification", "Neutral"),
                    }
        except Exception as e:
            logger.warning(f"获取F&G失败: {e}")

    def get_system_context(self) -> str:
        """生成市场上下文文本，用于注入 DeepSeek system prompt"""
        self.ensure_fresh()
        lines = []

        # Fear & Greed
        if self.fng_cache:
            fg = self.fng_cache
            fg_val = fg.get("value", 50)
            if fg_val <= 25:
                fg_advice = "极度恐惧 -> 可能接近底部，考虑逆势做多机会"
            elif fg_val >= 75:
                fg_advice = "极度贪婪 -> 可能接近顶部，注意回调风险"
            elif fg_val >= 55:
                fg_advice = "偏贪婪 -> 顺势操作但控制仓位"
            else:
                fg_advice = "偏恐惧 -> 市场谨慎，等待明确信号"
            lines.append(
                f"恐惧与贪婪指数: {fg_val}/100 ({fg.get('classification')}) -- {fg_advice}"
            )

        # Trending coins (market heat indicator)
        if self.trending_cache:
            lines.append(
                f"CoinGecko 当前热门币种: {', '.join(self.trending_cache[:8])}"
            )

        return "\n".join(lines) if lines else ""

    def get_sentiment_for_symbol(self, symbol: str) -> str:
        """获取特定币种的市场数据 (供 DeepSeek Function Call 使用)"""
        base = symbol.split("/")[0].lower()
        result = {"asset": base.upper()}

        # 1. CoinGecko 市场数据
        try:
            resp = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{base}",
                params={"localization": "false", "tickers": "false",
                        "community_data": "false", "developer_data": "false"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                market = data.get("market_data", {})
                result["current_price_usd"] = market.get("current_price", {}).get("usd")
                result["market_cap_rank"] = data.get("market_cap_rank")
                result["price_change_24h_pct"] = market.get("price_change_percentage_24h")
                result["price_change_7d_pct"] = market.get("price_change_percentage_7d")
                result["total_volume_24h"] = market.get("total_volume", {}).get("usd")
                # 情绪相关
                sentiment = data.get("sentiment_votes_up_percentage")
                if sentiment:
                    result["community_sentiment_up_pct"] = round(sentiment, 1)
            else:
                # Fallback to search endpoint
                resp2 = requests.get(
                    "https://api.coingecko.com/api/v3/search",
                    params={"query": base}, timeout=10,
                )
                if resp2.status_code == 200:
                    coins = resp2.json().get("coins", [])
                    if coins:
                        top = coins[0]
                        result["name"] = top.get("name")
                        result["market_cap_rank"] = top.get("market_cap_rank")
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

        # 2. 是否在热门榜单中
        if base.upper() in [t.upper() for t in self.trending_cache]:
            result["is_trending"] = True

        return json.dumps(result, ensure_ascii=False)
