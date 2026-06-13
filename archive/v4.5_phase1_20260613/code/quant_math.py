#!/usr/bin/env python3
"""
quant_math.py — 量化交易数学工具箱
====================================
三个纯本地算法，只依赖 numpy + pandas，零 API 调用:

1. KalmanFilterTrend — 卡尔曼滤波趋势判断 (无 EMA 滞后)
2. HurstExponent     — 市场状态识别 (趋势 vs 均值回归)
3. VolatilityCone    — 波动率锥动态止损

用法:
  from quant_math import kalman_trend, hurst_exponent, volatility_cone_sl
"""

import logging
from typing import Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("QuantBot")


# ════════════════════════════════════════════════════════════
# 1. 卡尔曼滤波趋势检测
# ════════════════════════════════════════════════════════════

def kalman_trend(prices: np.ndarray, process_noise: float = 1e-4,
                 measurement_noise: float = 1e-2) -> Tuple[float, str]:
    """
    卡尔曼滤波平滑价格序列，返回趋势方向和强度。

    Args:
        prices: 价格序列 (最近 N 根 K 线的 close)
        process_noise: 过程噪声 (默认 1e-4，越小越平滑)
        measurement_noise: 测量噪声 (默认 1e-2，越小越敏感)

    Returns:
        (trend_score, direction)
        trend_score: -1.0 ~ +1.0 (负=下降趋势, 正=上升趋势, 0=横盘)
        direction: "up" | "down" | "flat"
    """
    if len(prices) < 10:
        return 0.0, "flat"

    # Initialize Kalman filter
    n = len(prices)
    x = prices[0]        # state estimate (true price)
    p = 1.0              # error covariance
    q = process_noise    # process noise
    r = measurement_noise  # measurement noise

    estimates = np.zeros(n)
    estimates[0] = x

    for i in range(1, n):
        # Predict
        p = p + q
        # Update
        k = p / (p + r)  # Kalman gain
        x = x + k * (prices[i] - x)
        p = (1 - k) * p
        estimates[i] = x

    # Calculate trend from last 25% of smoothed data
    window = max(4, n // 4)
    recent = estimates[-window:]
    x_vals = np.arange(window)

    # Linear regression slope
    slope = np.polyfit(x_vals, recent, 1)[0]

    # Normalize to price scale
    price_mean = np.mean(recent)
    if price_mean > 0:
        norm_slope = slope / price_mean * 100  # percentage change per bar
    else:
        norm_slope = 0.0

    # Map to score
    if norm_slope > 0.02:
        score = min(1.0, norm_slope / 0.1)
        direction = "up"
    elif norm_slope < -0.02:
        score = max(-1.0, norm_slope / 0.1)
        direction = "down"
    else:
        score = norm_slope / 0.02 * 0.3  # 弱信号
        direction = "flat"

    return round(score, 3), direction


# ════════════════════════════════════════════════════════════
# 2. Hurst 指数 — 市场状态分类
# ════════════════════════════════════════════════════════════

def hurst_exponent(close_prices: np.ndarray, max_lag: int = 20) -> dict:
    """
    计算 Hurst 指数，判断市场是趋势还是均值回归。

    Hurst 指数 H:
      H > 0.5 → 趋势持续 (trending)
      H < 0.5 → 均值回归 (mean-reverting)
      H ≈ 0.5 → 随机游走 (random walk)

    Args:
        close_prices: 收盘价序列 (至少 50 个)
        max_lag: 最大滞后期数

    Returns:
        {"hurst": float, "regime": "trending"|"mean_reverting"|"random_walk", "confidence": 0-100}
    """
    if len(close_prices) < 50:
        return {"hurst": 0.5, "regime": "random_walk", "confidence": 0}

    n = len(close_prices)
    lags = range(2, min(max_lag, n // 4))
    tau = []
    log_rs = []

    for lag in lags:
        # Split into sub-windows of size 'lag'
        m = n // lag
        if m < 4:
            continue
        rs_values = []
        for i in range(m):
            chunk = close_prices[i * lag:(i + 1) * lag]
            if len(chunk) < lag:
                continue
            mean = np.mean(chunk)
            deviations = chunk - mean
            cumulative = np.cumsum(deviations)
            r_val = np.max(cumulative) - np.min(cumulative)
            s_val = np.std(chunk, ddof=1)
            if s_val > 0:
                rs_values.append(r_val / s_val)
        if not rs_values:
            continue
        avg_rs = np.mean(rs_values)
        tau.append(lag)
        log_rs.append(np.log(avg_rs))

    if len(tau) < 3:
        return {"hurst": 0.5, "regime": "random_walk", "confidence": 0}

    try:
        slope, _ = np.polyfit(np.log(tau), log_rs, 1)
    except Exception:
        return {"hurst": 0.5, "regime": "random_walk", "confidence": 0}

    h = round(slope, 4)

    # Regime classification
    if h > 0.55:
        regime = "trending"
        confidence = min(100, int((h - 0.5) * 200))
    elif h < 0.45:
        regime = "mean_reverting"
        confidence = min(100, int((0.5 - h) * 200))
    else:
        regime = "random_walk"
        confidence = int(50 - abs(h - 0.5) * 100)

    return {"hurst": h, "regime": regime, "confidence": max(0, confidence)}


# ════════════════════════════════════════════════════════════
# 3. 波动率锥 — 动态止损
# ════════════════════════════════════════════════════════════

def volatility_cone_sl(atr_series: np.ndarray, current_atr: float,
                       lookback: int = 100) -> dict:
    """
    基于历史 ATR 分布计算动态止损倍数。

    逻辑:
      - ATR 在历史 90% 分位 → 波动极端, SL 收紧到 0.7x
      - ATR 在历史 50% 分位 → 正常波动, SL 保持 1.0x
      - ATR 在历史 10% 分位 → 低波动, SL 放宽到 1.3x

    返回动态 SL 乘数 + 波动率分位数信息。
    """
    if len(atr_series) < lookback:
        return {"sl_multiplier": 1.0, "atr_percentile": 50, "regime": "normal"}

    recent_atrs = atr_series[-lookback:]
    percentile = (recent_atrs < current_atr).sum() / len(recent_atrs) * 100

    # Dynamic SL multiplier
    if percentile > 90:
        sl_mult = 0.7   # extreme vol → tight SL
        regime = "extreme_vol"
    elif percentile > 75:
        sl_mult = 0.85  # high vol
        regime = "high_vol"
    elif percentile < 15:
        sl_mult = 1.3   # low vol → wide SL
        regime = "low_vol"
    elif percentile < 30:
        sl_mult = 1.15
        regime = "normal_low"
    else:
        sl_mult = 1.0   # normal
        regime = "normal"

    return {
        "sl_multiplier": round(sl_mult, 2),
        "atr_percentile": round(percentile, 1),
        "regime": regime,
    }


# ════════════════════════════════════════════════════════════
# 4. 聚合函数 — 一次性计算所有量化指标
# ════════════════════════════════════════════════════════════

def compute_quant_signals(df: pd.DataFrame) -> dict:
    """
    输入 OHLCV DataFrame (需要 close/high/low 列),
    输出聚合的量化信号字典。

    用于 scan_single_symbol 中作为市场状态增强。
    """
    close = df["close"].values.astype(float)

    result = {}

    # 1. 卡尔曼趋势
    k_score, k_dir = kalman_trend(close[-50:])
    result["kalman_score"] = k_score
    result["kalman_direction"] = k_dir

    # 2. Hurst 指数
    hurst = hurst_exponent(close[-100:])
    result["hurst"] = hurst["hurst"]
    result["hurst_regime"] = hurst["regime"]
    result["hurst_confidence"] = hurst["confidence"]

    # 3. 波动率锥 (需要 ATR 序列)
    atr_raw = df["atr"].values.astype(float) if "atr" in df.columns else None
    if atr_raw is not None and len(atr_raw) >= 20:
        current = atr_raw[-1]
        cone = volatility_cone_sl(atr_raw, current)
        result["vol_cone_sl_mult"] = cone["sl_multiplier"]
        result["vol_cone_percentile"] = cone["atr_percentile"]
        result["vol_cone_regime"] = cone["regime"]

    # 4. CVD (需要交易数据)
    cvd = result.get("_cvd_data")
    if cvd is not None:
        result["cvd_divergence"] = cvd.get("divergence", False)
        result["cvd_trend"] = cvd.get("trend", "flat")

    return result


# ════════════════════════════════════════════════════════════
# 5. CVD — 累积成交量差 (Cumulative Volume Delta)
# ════════════════════════════════════════════════════════════

def compute_cvd(trades: list, close_prices: np.ndarray) -> dict:
    """
    从逐笔成交数据计算 CVD 及背离信号。

    原理:
      CVD = 累加(主动买量 - 主动卖量)
      价格创新高 + CVD未创新高 = 量价背离 → 假突破 → 偏空
      价格创新低 + CVD未创新低 = 量价背离 → 假跌破 → 偏多

    Args:
        trades: ccxt fetch_trades() 返回的成交列表,
                每条包含 {"side": "buy"/"sell", "amount": float, "price": float}
        close_prices: 收盘价序列 (用于判断价格极值)

    Returns:
        {"cvd": float, "divergence": bool, "bias": "bullish"/"bearish"/"neutral"}
    """
    if not trades or len(close_prices) < 10:
        return {"cvd": 0, "divergence": False, "bias": "neutral"}

    # ── 计算 CVD ──
    cvd = 0.0
    buy_vol = 0.0
    sell_vol = 0.0
    for t in trades:
        side = t.get("side", "").lower()
        amount = float(t.get("amount", 0) or 0)
        price = float(t.get("price", 0) or 0)
        vol = amount * price  # notional volume in USDT
        if side == "buy":
            cvd += vol
            buy_vol += vol
        else:
            cvd -= vol
            sell_vol += vol

    total_vol = buy_vol + sell_vol
    if total_vol == 0:
        return {"cvd": 0, "divergence": False, "bias": "neutral"}

    # ── CVD 趋势 ──
    cvd_ratio = cvd / total_vol  # -1.0 ~ +1.0
    if cvd_ratio > 0.15:
        cvd_trend = "bullish"
    elif cvd_ratio < -0.15:
        cvd_trend = "bearish"
    else:
        cvd_trend = "neutral"

    # ── 背离检测 ──
    recent = close_prices[-20:]
    price_high = np.max(recent)
    price_low = np.min(recent)
    current = close_prices[-1]

    divergence = False
    bias = "neutral"
    # 价格接近近期高点 + CVD为负 → 量价背离 (假突破)
    if current >= price_high * 0.98 and cvd_trend == "bearish":
        divergence = True
        bias = "bearish"
    # 价格接近近期低点 + CVD为正 → 量价背离 (假跌破)
    elif current <= price_low * 1.02 and cvd_trend == "bullish":
        divergence = True
        bias = "bullish"

    return {
        "cvd": round(cvd, 2),
        "cvd_ratio": round(cvd_ratio, 3),
        "buy_vol": round(buy_vol, 2),
        "sell_vol": round(sell_vol, 2),
        "cvd_trend": cvd_trend,
        "divergence": divergence,
        "bias": bias,
    }
