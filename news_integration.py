#!/usr/bin/env python3
"""
news_integration.py — 加密货币新闻集成 v3.6
============================================
多源新闻聚合 + 智能缓存 + 币种定向上下文。
为 DeepSeek system prompt 生成交易相关的新闻背景。

数据源:
  1. CryptoPanic API (有 key 时)
  2. CoinGecko trending + price (免费, 限流)
  3. Fear & Greed 指数 (免费, 无认证)

用法:
  from news_integration import CryptoPanicClient
  client = CryptoPanicClient(api_key)
  context = client.get_news_context(["BTC", "ETH", "SOL"])
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("QuantBot")


class CryptoPanicClient:
    """多源加密货币新闻客户端 v3.6"""

    BASE_URL = "https://cryptopanic.com/api/v1/posts/"
    FNG_URL = "https://api.alternative.me/fng/"
    CG_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
    CG_PRICE = "https://api.coingecko.com/api/v3/simple/price"

    # v3.6: CoinGecko 币种 ID 映射 (CoinGecko 用的是 slug, 不是 ticker)
    CG_ID_MAP = {
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
        "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
        "doge": "dogecoin", "dot": "polkadot", "link": "chainlink",
        "ltc": "litecoin", "avax": "avalanche-2", "matic": "matic-network",
        "uni": "uniswap", "atom": "cosmos", "etc": "ethereum-classic",
    }

    # 情感关键词 (v3.6: 扩展词库)
    POSITIVE_KEYWORDS = [
        "surge", "moon", "rally", "bullish", "green", "pump",
        "breakout", "ath", "approval", "partnership", "adoption",
        "upgrade", "launch", "listing", "record", "soar", "新高",
        "上涨", "涨", "突破", "利好", "合作", "上线", "飙升",
    ]
    NEGATIVE_KEYWORDS = [
        "crash", "dump", "bearish", "red", "selloff", "hack",
        "exploit", "ban", "fud", "liquidation", "warning",
        "lawsuit", "sec", "investigation", "decline", "暴跌",
        "下跌", "跌", "利空", "黑客", "调查", "诉讼", "崩盘", "闪崩",
    ]

    def __init__(self, api_key: Optional[str] = None, cache_ttl: int = 3600):
        self.api_key = api_key
        self.cache_ttl = cache_ttl
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_ts: float = 0.0
        self._fng_cache: Optional[Dict] = None
        self._fng_ts: float = 0.0
        self._use_free_source = not api_key
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "DeepSeekQuantBot/3.6"})

    # ════════════════════════════════════════════
    # 公开 API
    # ════════════════════════════════════════════

    def fetch_news(
        self, currencies: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """获取新闻。有 key → CryptoPanic；无 key → 免费源聚合。"""
        if self._cache_fresh():
            return self._cache or []

        if not self._use_free_source:
            results = self._fetch_cryptopanic(currencies)
            if results:
                self._cache = results[:25]
                self._cache_ts = time.time()
                return self._cache

        # 免费源聚合
        results = self._fetch_free_news(currencies)
        self._cache = results[:25]
        self._cache_ts = time.time()
        return self._cache

    def get_news_context(self, currencies: Optional[List[str]] = None) -> str:
        """
        将新闻格式化为 DeepSeek 可读的上下文文本。
        包含：新闻标题 + 情感标签 + Fear & Greed 指数。
        """
        news_list = self.fetch_news(currencies)
        lines = []

        # v3.6: 添加 Fear & Greed 指数
        fng = self._fetch_fng()
        if fng:
            val = fng.get("value", "?")
            classification = fng.get("value_classification", fng.get("classification", "?"))
            emoji = {"Extreme Fear": "🔴", "Fear": "🟠", "Neutral": "⚪",
                     "Greed": "🟢", "Extreme Greed": "🟢"}
            lines.append(f"F&G指数: {val} ({classification}) {emoji.get(classification, '')}")

        if news_list:
            lines.append("近期加密货币新闻:")
            for item in news_list[:8]:
                title = item.get("title", "")
                source = item.get("source", {}).get("title", "?")
                kind = item.get("kind", "news")
                sentiment = self._classify_sentiment(title)
                prefix = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(sentiment, "")
                tag = f" [{kind}]" if kind != "news" else ""
                lines.append(f"- {prefix} [{source}]{tag} {title}")

        return "\n".join(lines) if lines else ""

    def get_fear_greed(self) -> Optional[Dict]:
        """获取 Fear & Greed 指数 (带缓存)"""
        return self._fetch_fng()

    # ════════════════════════════════════════════
    # v3.7: 情绪量化输出
    # ════════════════════════════════════════════

    def get_sentiment_signal(self, currencies: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        v3.7: 量化情绪信号 — 将 F&G 指数 + 新闻情绪 + 价格变动转化为数值信号。

        Returns dict:
          - score:         综合情绪分数 0-100 (<35=恐惧, 35-65=中性, >65=贪婪)
          - signal:        "extreme_fear" | "fear" | "neutral" | "greed" | "extreme_greed"
          - fng_value:     F&G 指数原始值 (0-100)
          - fng_label:     F&G 分类标签
          - news_bias:     新闻情绪偏向 (-1.0 ~ +1.0, 正=利多)
          - market_action: 建议操作 "contrarian_buy" | "neutral" | "reduce_risk"
          - confidence:    信号置信度 0-100
          - detail:        中文摘要
        """
        result = {
            "score": 50, "signal": "neutral",
            "fng_value": 50, "fng_label": "Neutral",
            "news_bias": 0.0, "market_action": "neutral",
            "confidence": 50, "detail": "",
        }

        # ── 1. F&G 指数 (权重 60%) ──
        fng = self._fetch_fng()
        fng_val = 50
        if fng:
            try:
                fng_val = int(fng.get("value", 50))
            except (ValueError, TypeError):
                fng_val = 50
            result["fng_value"] = fng_val
            result["fng_label"] = fng.get("value_classification", str(fng_val))

        # ── 2. 新闻情绪偏向 (权重 25%) ──
        news_list = self.fetch_news(currencies)
        pos_count = neg_count = 0
        if news_list:
            for item in news_list[:15]:
                title = item.get("title", "")
                sentiment = self._classify_sentiment(title)
                if sentiment == "positive":
                    pos_count += 1
                elif sentiment == "negative":
                    neg_count += 1
            total = pos_count + neg_count
            if total > 0:
                result["news_bias"] = round((pos_count - neg_count) / total, 2)
            else:
                result["news_bias"] = 0.0

        # ── 3. 综合分数: F&G×0.6 + 新闻×0.25 + 价格变动×0.15 ──
        # F&G: 直接映射到分数 (0-100)
        # 新闻偏向: -1~+1 映射到 0-100 (50 为中心)
        news_score = 50 + result["news_bias"] * 40

        # 价格变动: 从已有缓存中取 24h 涨跌
        price_bias = 0.0
        try:
            if currencies:
                changes = []
                for item in news_list:
                    chg = item.get("change_pct", 0) or 0
                    if chg != 0:
                        changes.append(chg)
                if changes:
                    # 平均涨跌 → bias (-1~+1)
                    avg_chg = sum(changes) / len(changes)
                    price_bias = max(-1.0, min(1.0, avg_chg / 10.0))
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
        price_score = 50 + price_bias * 40

        score = fng_val * 0.60 + news_score * 0.25 + price_score * 0.15
        result["score"] = round(min(100, max(0, score)))

        # ── 4. 信号判定 ──
        s = result["score"]
        if s <= 15:
            result["signal"] = "extreme_fear"
            result["market_action"] = "contrarian_buy"
            result["confidence"] = 85
            result["detail"] = f"极度恐惧(F&G={fng_val})-反向买入信号"
        elif s <= 35:
            result["signal"] = "fear"
            result["market_action"] = "contrarian_buy"
            result["confidence"] = 65
            result["detail"] = f"恐惧(F&G={fng_val})-逢低布局"
        elif s <= 65:
            result["signal"] = "neutral"
            result["market_action"] = "neutral"
            result["confidence"] = 50
            result["detail"] = f"情绪中性(F&G={fng_val})-正常交易"
        elif s <= 85:
            result["signal"] = "greed"
            result["market_action"] = "reduce_risk"
            result["confidence"] = 65
            result["detail"] = f"贪婪(F&G={fng_val})-注意风控"
        else:
            result["signal"] = "extreme_greed"
            result["market_action"] = "reduce_risk"
            result["confidence"] = 85
            result["detail"] = f"极度贪婪(F&G={fng_val})-减仓信号"

        # ── 5. 追踪情绪趋势 (v3.7) ──
        if not hasattr(self, '_sentiment_history'):
            self._sentiment_history = []
        self._sentiment_history.append(result["score"])
        if len(self._sentiment_history) > 20:
            self._sentiment_history = self._sentiment_history[-20:]

        if len(self._sentiment_history) >= 3:
            recent = self._sentiment_history[-3:]
            if recent[-1] - recent[0] > 15:
                result["detail"] += " | 情绪快速转向贪婪⚠️"
            elif recent[0] - recent[-1] > 15:
                result["detail"] += " | 情绪快速转向恐惧🟢"

        logger.info(f"📊 情绪信号: score={result['score']} {result['signal']} "
                    f"F&G={fng_val} news_bias={result['news_bias']:+.2f}")
        return result

    # ════════════════════════════════════════════
    # 数据源实现
    # ════════════════════════════════════════════

    def _fetch_cryptopanic(
        self, currencies: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """CryptoPanic API"""
        params: Dict[str, Any] = {
            "auth_token": self.api_key,
            "public": "true",
            "kind": "news",
        }
        if currencies:
            params["currencies"] = ",".join(currencies[:5])

        try:
            resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"📰 CryptoPanic: {len(data.get('results', []))} 条新闻")
                return data.get("results", [])
            elif resp.status_code == 429:
                logger.warning("📰 CryptoPanic 限流 (429)，降级免费源")
            else:
                logger.warning(f"📰 CryptoPanic HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"📰 CryptoPanic 失败: {e}")
        return []

    def _fetch_free_news(
        self, currencies: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """v3.6: 免费源聚合 — CoinGecko trending + 价格 24h 变动"""
        results: List[Dict[str, Any]] = []

        # 1. Trending coins (v3.6: 按关注币种过滤)
        self._fetch_trending(results, currencies)

        # 2. 关注币种 24h 涨跌
        if currencies:
            self._fetch_price_changes(currencies, results)

        if results:
            logger.info(f"📰 免费源: {len(results)} 条市场数据")
        return results

    def _fetch_trending(self, results: List[Dict], currencies: Optional[List[str]] = None):
        """CoinGecko trending coins (v3.6: 支持按关注币种过滤)"""
        try:
            # 构建关注币种符号集合
            watch_set = {c.upper() for c in (currencies or [])}
            resp = self._session.get(self.CG_TRENDING, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for coin in data.get("coins", [])[:10]:
                    item = coin.get("item", {})
                    symbol = item.get("symbol", "").upper()
                    # 只显示我们关注的币种 (有 watch_set 时过滤)
                    if watch_set and symbol not in watch_set:
                        # 但 BTC/ETH 始终显示 (市场基准)
                        if symbol not in ("BTC", "ETH"):
                            continue
                    name = item.get("name", "?")
                    score = item.get("score", 0)
                    rank = item.get("market_cap_rank", "?")
                    results.append({
                        "title": f"{name} ({symbol}) 热度#{score} 市值#{rank}",
                        "source": {"title": "CoinGecko"},
                        "kind": "trending",
                    })
            elif resp.status_code == 429:
                logger.warning("📰 CoinGecko trending 限流 (429)")
        except Exception as e:
            logger.warning(f"📰 CoinGecko trending 失败: {e}")

    def _fetch_price_changes(self, currencies: List[str], results: List[Dict]):
        """v3.6: 批量查询币种 24h 价格变动 (一次 API 调用)"""
        cg_ids = []
        for cur in currencies[:5]:
            cg_id = self.CG_ID_MAP.get(cur.lower(), cur.lower())
            cg_ids.append(cg_id)

        if not cg_ids:
            return

        try:
            resp = self._session.get(
                self.CG_PRICE,
                params={
                    "ids": ",".join(cg_ids),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                for cid, info in resp.json().items():
                    chg = info.get("usd_24h_change", 0) or 0
                    direction = "涨" if chg > 0 else "跌"
                    # 反向映射 cg_id → ticker
                    ticker = cid.upper()
                    for t, c in self.CG_ID_MAP.items():
                        if c == cid:
                            ticker = t.upper()
                            break
                    results.append({
                        "title": f"{ticker} 24h{direction}{abs(chg):.1f}%",
                        "source": {"title": "CoinGecko"},
                        "kind": "price",
                        "change_pct": round(chg, 2),
                    })
            elif resp.status_code == 429:
                logger.warning("📰 CoinGecko price 限流 (429)")
        except Exception as e:
            logger.warning(f"📰 CoinGecko price 失败: {e}")

    def _fetch_fng(self) -> Optional[Dict]:
        """Fear & Greed 指数 (5 分钟缓存)"""
        if self._fng_cache and (time.time() - self._fng_ts) < 300:
            return self._fng_cache
        try:
            resp = self._session.get(f"{self.FNG_URL}?limit=1", timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    self._fng_cache = data[0]
                    self._fng_ts = time.time()
                    return self._fng_cache
        except Exception as e:
            logger.debug(f"F&G 获取失败: {e}")
        return self._fng_cache

    # ════════════════════════════════════════════
    # 工具方法
    # ════════════════════════════════════════════

    def _classify_sentiment(self, title: str) -> str:
        """基于关键词的情感分类"""
        title_lower = title.lower()
        pos_count = sum(1 for kw in self.POSITIVE_KEYWORDS if kw.lower() in title_lower)
        neg_count = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw.lower() in title_lower)
        if pos_count > neg_count:
            return "positive"
        elif neg_count > pos_count:
            return "negative"
        return "neutral"

    def _cache_fresh(self) -> bool:
        """检查新闻缓存是否有效"""
        if self._cache is None:
            return False
        return (time.time() - self._cache_ts) < self.cache_ttl

    def close(self):
        """关闭 HTTP 会话"""
        try:
            self._session.close()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
