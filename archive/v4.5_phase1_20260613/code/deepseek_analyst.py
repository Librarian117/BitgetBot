#!/usr/bin/env python3
"""
deepseek_analyst.py — AI 研究层 (Phase 1 提取自 deepseek_quant_bot.py)

调用 DeepSeek API，提供信号解释、持仓复盘、市场异常检测、市场评论等研究功能。
v4.1: AI 降级为研究顾问，不做决策。
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

from config_manager import ConfigManager
from market_context import MarketContextManager

logger = logging.getLogger("QuantBot")


class DeepSeekAnalyst:
    """调用 DeepSeek API，审核量化信号（v2: 含多TF + 市场情绪上下文 + 可选 Function Call）"""

    # DeepSeek Function Call 定义
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "get_crypto_sentiment",
                "description": "获取某个加密货币的当前市场情绪、新闻热度和社交情绪数据",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset": {
                            "type": "string",
                            "description": "资产代码，如 BTC, ETH, SOL, BNB, XRP 等",
                        },
                    },
                    "required": ["asset"],
                },
            },
        }
    ]

    def __init__(self, config: ConfigManager, market_ctx: Optional[MarketContextManager] = None):
        self.config = config
        self.market_ctx = market_ctx
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.deepseek_api_key}",
            "Content-Type": "application/json",
        })
        # ── v2.7: 断路器 (连续失败 N 次后本周期跳过 AI，避免 DeepSeek 宕机时卡死) ──
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False
        self._circuit_threshold: int = 3
        # ── v3.0: 新闻上下文 ──
        self.news_context: str = ""
        # ── v3.1: 交易智慧上下文 ──
        self.wisdom_context: str = ""
        # ── v4.1: 最后一次 AI 原始回复（用于解析失败时诊断） ──
        self.last_raw_info: Dict[str, Any] = {}

    def circuit_breaker_reset(self):
        """每周期初重置断路器"""
        self._consecutive_failures = 0
        self._circuit_open = False

    def circuit_breaker_open(self) -> bool:
        """断路器是否已熔断"""
        return self._circuit_open

    def close(self):
        """关闭 HTTP 会话 (v2.5)"""
        try:
            self.session.close()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

    @staticmethod
    def _merge_content_reasoning(msg: Dict[str, Any]) -> str:
        """v4.1: 智能合并 DeepSeek 推理模型的 content + reasoning_content。

        策略: 优先使用 content（最终答案，通常含 JSON），
        仅在 content 无 JSON 时才合并 reasoning_content。
        避免 reasoning 中的分析文本污染 JSON 解析器。

        返回: (merged_text, source_flag)
          source_flag: "content" | "reasoning" | "merged" | "empty"
        """
        c = (msg.get("content") or "").strip()
        r = (msg.get("reasoning_content") or "").strip()

        # 1. content 中有 JSON → 只用 content（reasoning 是噪音）
        if c and '{' in c:
            return c
        # 2. content 为空但 reasoning 有 JSON → 用 reasoning
        if r and '{' in r:
            return r
        # 3. 都有但 content 无 JSON → 合并（reasoning 可能在文本中含 JSON）
        if c and r:
            return f"{c}\n{r}"
        # 4. 只有其中一个
        return c or r

    @staticmethod
    def _extract_json(raw: str, context: str = "entry_review") -> Dict[str, Any]:
        """从可能包含 markdown 代码块或额外文本中提取 JSON。

        Args:
            raw: AI 原始回复文本
            context: "entry_review" | "position_review" | "market_anomaly" | "genetic_evolve"
                     控制解析失败时的默认决策
        """
        # v3.7: context 决定默认值 — entry_review 默认 REJECT (宁可错过),
        #       position_review 默认 HOLD (保持现状)

        candidates = []

        # 1. 提取 ```json ... ``` 或 ``` ... ``` 代码块
        for m in re.finditer(r'```(?:json)?\s*\n?(.*?)```', raw, re.DOTALL):
            candidates.append(m.group(1).strip())

        # 2. v4.1: 尾部优先 — AI 被告知"JSON 结尾"，从末尾提取最可靠
        #    从最后 2000→1000→500→200 字符逐步收缩窗口查找完整 JSON
        if not candidates:
            for window in (2000, 1000, 500, 200):
                tail = raw[-window:] if len(raw) > window else raw
                # 在尾部找所有 { } 对，从最长的开始尝试
                for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', tail, re.DOTALL):
                    cand = m.group(0)
                    if '"decision"' in cand:
                        candidates.append(cand)
                if candidates:
                    break  # 找到就停，优先用大窗口的结果

        # 3. 如果没有从尾部找到，尝试正则匹配 JSON 对象 (v3.7: 支持嵌套)
        if not candidates:
            for m in re.finditer(r'\{[^{}]*"decision"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL):
                candidates.append(m.group(0))

        # 4. 尝试每个候选（尾部优先的排在前面）
        for cand in candidates:
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue

        # 5. 直接解析全文本
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 5. 找第一个 { 到最后一个 }
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        # 5.5 v4.0: JSON 修复步骤 (从 genetic_evolver v2.0 移植)
        for m in re.finditer(r'\{.*\}', raw, re.DOTALL):
            candidate = m.group(0)
            # 5a. 直接试
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # 5b. 移除尾部逗号
            fixed = re.sub(r',\s*}', '}', candidate)
            fixed = re.sub(r',\s*]', ']', fixed)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
            # 5c. 单引号 → 双引号
            fixed = re.sub(r"'([^']*)'\s*:", r'"\1":', candidate)
            fixed = re.sub(r":\s*'([^']*)'", r': "\1"', fixed)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue

        # 6. v4.0: 智能关键词推断 — 加权评分，尾部优先，防误判
        raw_lower = raw.lower()
        # 取最后 500 字符(结论部分)权重 ×3，全文权重 ×1
        tail = raw_lower[-500:] if len(raw_lower) > 500 else raw_lower

        def _score_keywords(text, weight, keywords):
            """v4.0 fix: 防误判 — 关键词前有"不/未/否/禁止"时不计数"""
            score = 0
            for kw in keywords:
                idx = text.find(kw)
                if idx == -1:
                    continue
                # 检查前几个字符是否有否定词
                before = text[max(0, idx - 4):idx]
                negated = any(n in before for n in ("不", "未", "否", "无", "非"))
                if not negated:
                    score += weight
            return score

        confirm_score = _score_keywords(tail, 3, ["confirm", "确认", "通过", "建议开仓", "建议做多", "建议做空", "可以开仓"]) \
            + _score_keywords(raw_lower, 1, ["confirm", "确认", "通过", "建议开仓", "建议做多", "建议做空"])

        reject_score = _score_keywords(tail, 3, ["reject", "拒绝", "不建议", "观望", "放弃", "风险过大", "不通过", "不开仓"]) \
            + _score_keywords(raw_lower, 1, ["reject", "拒绝", "不建议", "观望", "放弃", "风险过大"])

        close_score = _score_keywords(tail, 3, ["close", "平仓", "止盈", "止损退出", "建议平仓", "立即平仓", "锁仓", "离场", "出场"]) \
            + _score_keywords(raw_lower, 1, ["close", "平仓", "止盈", "止损退出", "建议平仓"])

        hold_score = _score_keywords(tail, 3, ["hold", "持有", "继续持有", "不动", "维持", "等待", "持仓", "暂持"]) \
            + _score_keywords(raw_lower, 1, ["hold", "持有", "继续持有", "持仓", "不动"])

        # 去重: 如果 confirm 出现在 "不建议开仓" 中，不要误判
        if "不建议开仓" in raw_lower or "不confirm" in raw_lower:
            confirm_score = max(0, confirm_score - 3)
        if "不平仓" in raw_lower or "不close" in raw_lower:
            close_score = max(0, close_score - 3)

        max_score = max(confirm_score, reject_score, close_score, hold_score)
        if max_score >= 4:  # 需要足够的置信度
            if confirm_score == max_score:
                logger.warning(f"⚠️  关键词推断 CONFIRM (score={confirm_score})")
                return {"decision": "CONFIRM", "reason": f"文本推断确认(得分{confirm_score})"}
            if reject_score == max_score:
                logger.warning(f"⚠️  关键词推断 REJECT (score={reject_score})")
                return {"decision": "REJECT", "reason": f"文本推断拒绝(得分{reject_score})"}
            if close_score == max_score:
                logger.warning(f"⚠️  关键词推断 CLOSE (score={close_score})")
                return {"decision": "CLOSE", "reason": f"文本推断平仓(得分{close_score})"}
            if hold_score == max_score:
                logger.warning(f"⚠️  关键词推断 HOLD (score={hold_score})")
                return {"decision": "HOLD", "reason": f"文本推断持有(得分{hold_score})"}

        # 6.5 v4.0: 入场信号规则回退 — 关键词不足以判断时，用技术指标文本推断
        if context == "entry_review" and max_score < 4:
            indicators_ok = 0
            # ADX 强趋势 (>25)
            adx_m = re.search(r'(?:ADX|adx).*?(\d+\.?\d*)', raw)
            if adx_m and float(adx_m.group(1)) > 25:
                indicators_ok += 1
            # RSI 在合理区间 (30-70)
            rsi_m = re.search(r'(?:RSI|rsi).*?(\d+\.?\d*)', raw)
            if rsi_m and 30 < float(rsi_m.group(1)) < 70:
                indicators_ok += 1
            # EMA 趋势支持 (价格在EMA之上或提到"EMA"+"之上/上方")
            if re.search(r'(?:EMA|ema).*(?:之上|上方|above)', raw):
                indicators_ok += 1
            # 成交量确认 (提到"放量"、"量比"、"volume")
            if re.search(r'(?:成交量|volume|Vol|放量|量比).*(?:放大|放量|突破|高于|充足)', raw, re.IGNORECASE):
                indicators_ok += 1
            if indicators_ok >= 3:
                logger.warning(f"📋 规则回退 CONFIRM ({indicators_ok}/4指标)")
                return {"decision": "CONFIRM", "reason": f"技术指标推断确认({indicators_ok}/4)"}
            elif indicators_ok <= 1:
                logger.warning(f"📋 规则回退 REJECT ({indicators_ok}/4指标)")
                return {"decision": "REJECT", "reason": f"技术指标推断拒绝({indicators_ok}/4)"}

        # 7. 最后手段：用正则暴力提取 decision 字段
        import re as _re
        m = _re.search(r'(?:decision|决定)[:\s]*["\']?\s*(CONFIRM|REJECT|CLOSE|HOLD|confirm|reject|close|hold)', raw, _re.IGNORECASE)
        if m:
            d = m.group(1).upper()
            logger.warning(f"⚠️  正则推断 decision={d} | raw[:200]={raw[:200]}")
            return {"decision": d, "reason": "正则推断"}

        # v4.0: 记录解析失败详情便于诊断
        logger.warning(f"⚠️  完全无法解析 DeepSeek 回复 (context={context}): raw[:300]={raw[:300]}")

        # v3.7: context 感知的默认值
        # v4.1: 保留旧决策层 context 兼容，新增研究层 context
        defaults = {
            # v4.1 deprecated — 保留兼容
            "entry_review": {"decision": "REJECT", "reason": "解析失败-默认拒绝"},
            "position_review": {"decision": "HOLD", "reason": "解析失败-默认持有"},
            # v4.1 new — 研究层 context，不输出决策
            "signal_explanation": {"signal_explanation": "解析失败", "risk_factors": [], "quality": "unknown"},
            "position_explanation": {"trend_status": "无法分析", "risk_score": 5, "key_observations": []},
            # 其他
            "market_anomaly": {},
            "genetic_evolve": {},
        }
        default = defaults.get(context, {"decision": "REJECT", "reason": "解析失败"})
        logger.warning(f"⚠️  完全无法解析 DeepSeek 回复 (context={context}): raw={raw[:300]}...")
        return default

    # ── v3.0: 异常检测 & 市场评论 ──

    def review_market_anomaly(
        self, symbols_data: List[Dict[str, Any]], market_regime: str
    ) -> Optional[str]:
        """请求 DeepSeek 扫描全部币种数据，主动标记异常"""
        if not symbols_data or self._circuit_open:
            return None

        summary_lines = [
            f"市场状态: {market_regime}",
            "当前币种数据:",
        ]
        for d in symbols_data:
            summary_lines.append(
                f"  {d.get('symbol','?')}: 方向={d.get('direction','?')} "
                f"价格={d.get('price',0):.4f} ADX={d.get('adx',0):.2f} "
                f"RSI={d.get('rsi',0):.2f} VolRatio={d.get('vol_ratio',1):.2f}"
            )

        system_prompt = (
            "【必须使用中文回复，禁止使用英文】\n"
            "你是一名加密市场异常检测器。分析以下币种数据，判断是否存在异常情况。"
            "异常包括：价格与指标严重背离、多个币种同时出现极端信号、"
            "成交量异常放大/缩小、可能的市场操纵迹象等。"
            "返回 JSON: {\"anomaly_detected\": bool, \"description\": \"简要描述\"}"
        )
        user_prompt = "\n".join(summary_lines)

        try:
            resp = self.session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 256,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                body = resp.json()
                content = self._merge_content_reasoning(body["choices"][0]["message"]) or "{}"
                result = self._extract_json(content, context="market_anomaly")
                if result.get("anomaly_detected"):
                    return result.get("description", "异常已检测")
            else:
                self._consecutive_failures += 1
        except Exception:
            self._consecutive_failures += 1
        return None

    def generate_commentary(self, cycle_data: Dict[str, Any]) -> str:
        """请求 DeepSeek 生成一行市场评论"""
        if self._circuit_open:
            return ""

        system_prompt = (
            "【必须使用中文回复，禁止使用英文】\n"
            "你是一名加密货币市场评论员。根据提供的周期摘要，"
            "用一行中文（最多100字）总结当前市场情况。简洁有力。"
        )
        user_prompt = json.dumps(cycle_data, ensure_ascii=False)

        try:
            resp = self.session.post(
                self.config.deepseek_url,
                json={
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.5,
                    "max_tokens": 150,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                body = resp.json()
                msg = body["choices"][0]["message"]
                return self._merge_content_reasoning(msg)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
        return ""
