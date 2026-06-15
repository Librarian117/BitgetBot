#!/usr/bin/env python3
"""
filter_pipeline.py — 市场状态过滤器 (P3: extracted from deepseek_quant_bot.py)
=====================================
regime detection + Kalman resolver. 每个函数是纯逻辑, 行为与原始 100% 一致。
"""

from typing import Any, Dict, List, Optional


def resolve_kalman(
    direction: str, strategy: str,
    kalman_dir: str, kalman_score: float,
    ema_kalman_conflict: bool,
    is_bearish_trend: bool, is_bullish_trend: bool,
) -> Dict[str, Any]:
    """v4.5: Kalman 单一决策。

    原始位置: deepseek_quant_bot.py _resolve_kalman() lines 493-566

    返回:
        action: "allow" | "penalize" | "downgrade"
        new_strategy: None | "counter_trend"
        confidence_penalty: int
        kalman_conflict_for_scorer: bool
    """
    result = {
        "action": "allow",
        "new_strategy": None,
        "confidence_penalty": 0,
        "kalman_conflict_for_scorer": False,
    }

    # Check 1: Kalman direction vs signal direction
    kalman_signal_conflict = (
        kalman_dir != "flat"
        and ((direction == "SHORT" and kalman_dir == "up")
             or (direction == "LONG" and kalman_dir == "down"))
    )

    if kalman_signal_conflict:
        score = abs(kalman_score)
        if score > 0.8:
            result["action"] = "downgrade"
            result["new_strategy"] = "counter_trend"
            result["confidence_penalty"] = int(score * 25)
            result["kalman_conflict_for_scorer"] = True
        elif score > 0.5:
            result["action"] = "downgrade"
            result["new_strategy"] = "counter_trend"
            result["confidence_penalty"] = int(score * 20)
            result["kalman_conflict_for_scorer"] = True
        elif score > 0.2:
            result["action"] = "penalize"
            result["confidence_penalty"] = int(score * 25)
        return result

    # Check 2: EMA-Kalman divergence
    if ema_kalman_conflict:
        goes_against_ema = (
            (direction == "LONG" and is_bearish_trend)
            or (direction == "SHORT" and is_bullish_trend)
        )
        strong_kalman = abs(kalman_score) > 0.5
        if goes_against_ema and strategy == "pullback":
            result["action"] = "downgrade"
            result["new_strategy"] = "counter_trend"
            if strong_kalman:
                result["confidence_penalty"] = min(int(abs(kalman_score) * 20), 20)
                result["kalman_conflict_for_scorer"] = True
        elif goes_against_ema:
            if strong_kalman:
                result["action"] = "penalize"
                result["confidence_penalty"] = min(int(abs(kalman_score) * 20), 20)

    return result


def detect_market_regime(
    tf_context: Dict[str, Any],
    higher_timeframes: List[str],
    atr: float = 0, close: float = 0,
    adx: float = 0, vol_ratio: float = 1.0,
    markov_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """v4.0: 6 状态市场分类。

    原始位置: deepseek_quant_bot.py _detect_market_regime() lines 940-1135

    Returns:
        regime, direction, volatility, trend_strength, atr_pct, consensus,
        recommended, allowed_directions, confidence, detail, markov_bias
    """
    # 1. Multi-TF direction consensus
    bull_tfs = 0
    bear_tfs = 0
    total_tfs = 0
    for tf in higher_timeframes:
        ctx = tf_context.get(tf, {})
        trend = ctx.get("trend", "unknown")
        if trend == "bullish":
            bull_tfs += 1
            total_tfs += 1
        elif trend == "bearish":
            bear_tfs += 1
            total_tfs += 1

    # 2. Volatility (ATR%)
    atr_pct = (atr / close * 100) if close > 0 else 0
    if atr_pct >= 2.5:
        volatility = "extreme"
    elif atr_pct >= 1.0:
        volatility = "high"
    elif atr_pct >= 0.3:
        volatility = "normal"
    else:
        volatility = "low"

    # 3. ADX tier
    if adx >= 25:
        adx_tier = "strong"
    elif adx >= 20:
        adx_tier = "moderate"
    else:
        adx_tier = "weak"

    # 4. Direction
    if bull_tfs > bear_tfs:
        direction = "bullish"
        consensus = bull_tfs / max(total_tfs, 1)
    elif bear_tfs > bull_tfs:
        direction = "bearish"
        consensus = bear_tfs / max(total_tfs, 1)
    else:
        direction = "neutral"
        consensus = 0.0

    # 5. Markov bias
    markov_bias = 0.0
    if markov_result and markov_result.get("confidence", 0) >= 40:
        markov_bias = markov_result.get("signal", 0)

    # 6. Six-state classification
    regime = "range"
    recommended = []
    detail_parts = []

    if volatility == "extreme":
        regime = "panic"
        recommended = []
        detail_parts.append("extreme volatility - watch only")
    elif direction == "bullish" and consensus >= 1.0 and adx_tier == "strong":
        regime = "strong_bull"
        recommended = ["momentum", "pullback", "ema_cross"]
        detail_parts.append("strong bull - trend follow long")
    elif direction == "bearish" and consensus >= 1.0 and adx_tier == "strong":
        regime = "strong_bear"
        recommended = ["momentum", "pullback", "ema_cross"]
        detail_parts.append("strong bear - trend follow short")
    elif direction == "bullish" and consensus >= 0.5:
        regime = "bull"
        recommended = ["pullback", "ema_cross"]
        if adx_tier == "strong":
            recommended.insert(0, "momentum")
        detail_parts.append("bull - pullback long + EMA cross")
    elif direction == "bearish" and consensus >= 0.5:
        regime = "bear"
        recommended = ["pullback", "ema_cross"]
        if adx_tier == "strong":
            recommended.insert(0, "momentum")
        detail_parts.append("bear - pullback short + EMA cross")
    else:
        regime = "range"
        if volatility == "low":
            recommended = ["grid", "bollinger", "pullback", "ema_cross"]
            detail_parts.append("low vol range - grid/bollinger/pullback/ema_cross")
        else:
            recommended = ["pullback", "bollinger", "ema_cross"]
            if adx_tier == "strong":
                recommended.append("momentum")
            detail_parts.append("range - pullback/bollinger/ema_cross")

    # 7. Markov adjustment
    if markov_bias > 0.3 and regime in ("range", "bear"):
        detail_parts.append(f"Markov bullish({markov_bias:.2f}) upgrade")
        if regime == "bear":
            regime = "range"
            recommended = ["pullback", "bollinger", "ema_cross"]
        elif regime == "range" and direction != "bearish":
            regime = "bull"
            recommended = ["pullback", "ema_cross"]
    elif markov_bias < -0.3 and regime in ("range", "bull"):
        detail_parts.append(f"Markov bearish({markov_bias:.2f}) downgrade")
        if regime == "bull":
            regime = "range"
            recommended = ["pullback", "bollinger", "ema_cross"]
        elif regime == "range" and direction != "bullish":
            regime = "bear"
            recommended = ["pullback", "counter_trend"]

    # 8. Allowed directions
    allowed_long = True
    allowed_short = True
    for tf in higher_timeframes:
        ctx = tf_context.get(tf, {})
        trend_tf = ctx.get("trend", "unknown")
        regime_tf = ctx.get("regime", "unknown")
        if regime_tf == "ranging" or trend_tf == "unknown":
            continue
        if trend_tf == "bearish":
            allowed_long = False
        elif trend_tf == "bullish":
            allowed_short = False
    allowed_directions: List[str] = []
    if allowed_long:
        allowed_directions.append("LONG")
    if allowed_short:
        allowed_directions.append("SHORT")
    if not allowed_directions:
        detail_parts.append("both directions blocked - close only")

    # 9. Volume adjustment
    if vol_ratio > 2.0:
        detail_parts.append(f"high volume {vol_ratio:.1f}x")
    elif vol_ratio < 0.5 and vol_ratio > 0:
        detail_parts.append("low volume - cautious")

    # 10. Confidence
    confidence = 70 if consensus >= 1.0 else (55 if consensus >= 0.5 else 40)
    if volatility == "extreme":
        confidence = 85

    return {
        "regime": regime,
        "direction": direction,
        "volatility": volatility,
        "trend_strength": adx_tier,
        "atr_pct": round(atr_pct, 3),
        "consensus": round(consensus, 2),
        "recommended": recommended,
        "allowed_directions": allowed_directions,
        "confidence": min(100, confidence),
        "detail": " | ".join(detail_parts),
        "markov_bias": round(markov_bias, 3),
    }


def filter_volume(
    df, cidx: int, session: dict, is_sandbox: bool,
) -> dict:
    """成交量确认过滤。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1026-1044

    Returns {"passed": bool, "vol_ratio": float}
    sandbox 模式下始终 passed=True, vol_ratio=1.0。
    """
    vol_ratio = 1.0
    if not is_sandbox:
        try:
            cur_vol = float(df["volume"].iloc[cidx])
            avg_vol = float(df["volume"].tail(20).mean())
            vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
        except Exception:
            vol_ratio = 1.0
        if vol_ratio < session.get("vol_ratio", 1.0):
            return {"passed": False, "vol_ratio": vol_ratio}
    return {"passed": True, "vol_ratio": vol_ratio}


def adjust_direction_bias(
    is_bearish_trend: bool, is_bullish_trend: bool,
    kalman_ema_disagree: bool,
    effective_oversold: float, effective_overbought: float,
) -> dict:
    """趋势方向偏向 + RSI 阈值调整。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1093-1107

    Returns {
        "block_long": bool, "block_short": bool,
        "effective_oversold": float, "effective_overbought": float,
    }
    """
    block_long = is_bearish_trend and not kalman_ema_disagree
    block_short = is_bullish_trend and not kalman_ema_disagree

    if is_bearish_trend:
        effective_overbought = max(45, effective_overbought - 10)
        effective_oversold = max(10, effective_oversold - 5)
    elif is_bullish_trend:
        effective_oversold = min(55, effective_oversold + 10)
        effective_overbought = min(85, effective_overbought + 5)

    return {
        "block_long": block_long,
        "block_short": block_short,
        "effective_oversold": effective_oversold,
        "effective_overbought": effective_overbought,
    }


def filter_btc_linkage(
    candidates: list, btc_change: float,
    btc_drop_block_long: float, btc_pump_block_short: float,
) -> dict:
    """BTC 联动过滤: BTC 暴跌时阻止做多, BTC 暴涨时阻止做空。

    原始位置: deepseek_quant_bot.py _phase_scan_and_filter() lines 2466-2501

    Returns {"passed": list, "blocked": list[dict]}
    blocked items have {symbol, direction, reason} for tlogger/stats consumption.
    BTC symbol itself always passes.
    """
    if btc_change is None:
        return {"passed": candidates, "blocked": []}

    passed = []
    blocked = []
    for sig in candidates:
        symbol = sig["symbol"]
        direction = sig["direction"]

        if "BTC" in symbol:
            passed.append(sig)
            continue

        if direction == "LONG" and btc_change < btc_drop_block_long:
            blocked.append({"symbol": symbol, "direction": direction,
                            "reason": "btc_drop_block_long"})
            continue
        if direction == "SHORT" and btc_change > btc_pump_block_short:
            blocked.append({"symbol": symbol, "direction": direction,
                            "reason": "btc_pump_block_short"})
            continue

        passed.append(sig)

    return {"passed": passed, "blocked": blocked}
