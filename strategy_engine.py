#!/usr/bin/env python3
"""
strategy_engine.py — 策略信号生成器 (P3: extracted from deepseek_quant_bot.py)
=====================================
每个 evaluator 是纯函数: 输入 market data + config → 返回 Optional[signal dict]
行为必须与 scan_single_symbol() 原始内联逻辑 100% 一致。
"""

from typing import Any, Dict, Optional

import pandas as pd


def evaluate_pullback(
    close: float, ema: float, rsi: float,
    effective_oversold: float, effective_overbought: float,
) -> Optional[Dict[str, Any]]:
    """回调策略: 趋势中等待回调。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1368-1373

    LONG:  price > EMA and RSI below oversold → 上升趋势中的回调买入
    SHORT: price < EMA and RSI above overbought → 下降趋势中的反弹卖出
    """
    if close > ema and rsi < effective_oversold:
        return {"direction": "LONG", "strategy": "pullback"}
    if close < ema and rsi > effective_overbought:
        return {"direction": "SHORT", "strategy": "pullback"}
    return None


def evaluate_momentum(
    close: float, ema: float, rsi: float, adx: float,
    df: pd.DataFrame, cidx: int,
    momentum_enabled: bool,
    momentum_rsi_min: float, momentum_rsi_max: float,
    momentum_adx_threshold: float,
    momentum_ema_period: int,
    momentum_breakout_bars: int,
) -> Optional[Dict[str, Any]]:
    """动量突破策略: 顺势追涨/追跌。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1376-1392

    LONG:  price > EMA + ADX强势 + RSI在动量区 + 突破近期高点
    SHORT: price < EMA + ADX强势 + RSI在弱势动量区 + 跌破近期低点
    """
    if not momentum_enabled:
        return None

    # LONG momentum
    if (close > ema
            and rsi > momentum_rsi_min
            and rsi < momentum_rsi_max
            and adx > momentum_adx_threshold):
        momentum_ema = float(df["close"].ewm(span=momentum_ema_period, adjust=False).mean().iloc[cidx])
        if close > momentum_ema:
            recent_high = float(df["high"].tail(momentum_breakout_bars).max())
            if close >= recent_high * 0.998:
                return {"direction": "LONG", "strategy": "momentum"}

    # SHORT momentum (v3.2)
    if (close < ema
            and rsi < (100 - momentum_rsi_min)
            and rsi > (100 - momentum_rsi_max)
            and adx > momentum_adx_threshold):
        momentum_ema = float(df["close"].ewm(span=momentum_ema_period, adjust=False).mean().iloc[cidx])
        if close < momentum_ema:
            recent_low = float(df["low"].tail(momentum_breakout_bars).min())
            if close <= recent_low * 1.002:
                return {"direction": "SHORT", "strategy": "momentum"}

    return None


def evaluate_ema_cross(
    df: pd.DataFrame, cidx: int,
    close: float, ema200: float, rsi: float,
    momentum_enabled: bool,
) -> Optional[Dict[str, Any]]:
    """EMA 交叉策略: 快慢线金叉死叉。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1390-1406
    """
    if not momentum_enabled:
        return None
    ema_fast = float(df["close"].ewm(span=9, adjust=False).mean().iloc[cidx])
    ema_slow = float(df["close"].ewm(span=21, adjust=False).mean().iloc[cidx])
    ema_fast_prev = float(df["close"].ewm(span=9, adjust=False).mean().iloc[cidx - 1])
    ema_slow_prev = float(df["close"].ewm(span=21, adjust=False).mean().iloc[cidx - 1])
    if ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow:
        if close > ema200 or rsi < 60:
            return {"direction": "LONG", "strategy": "ema_cross"}
    elif ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow:
        if close < ema200 or rsi > 40:
            return {"direction": "SHORT", "strategy": "ema_cross"}
    return None


def evaluate_bollinger(
    df: pd.DataFrame, cidx: int,
    close: float, rsi: float,
) -> Optional[Dict[str, Any]]:
    """布林带均值回归: 震荡市触碰轨道反弹。

    原始位置: deepseek_quant_bot.py scan_single_symbol() lines 1408-1424
    """
    bb_std = float(df["close"].rolling(20).std().iloc[cidx])
    bb_mid = float(df["close"].rolling(20).mean().iloc[cidx])
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
    if bb_width <= 0.02:
        return None
    if close <= bb_lower and rsi < 45 and rsi > 20:
        return {"direction": "LONG", "strategy": "bollinger"}
    if close >= bb_upper and rsi > 55 and rsi < 80:
        return {"direction": "SHORT", "strategy": "bollinger"}
    return None
