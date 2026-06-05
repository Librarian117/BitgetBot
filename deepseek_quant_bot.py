#!/usr/bin/env python3
"""
deepseek_quant_bot.py — AI + 量化混合交易策略 v2.7
====================================================
架构：高度模块化，专业风控，DeepSeek V4 AI 决策确认

v2.0 新特性:
  - 附带 SL/TP 下单（消除裸仓空窗期）
  - 手续费计算 & 追踪
  - ADX 动态仓位管理
  - 最大并发持仓限制 + 滑点缓冲
  - 多时间周期趋势分析（1h/4h 大局观）
  - 市场情绪 & 新闻分析（cryptocurrency.cv + F&G Index）
  - 部分止盈（50%@2×ATR + 50%@3×ATR）
  - DeepSeek Function Call 按需查询市场情绪

模块清单：
  1. ConfigManager        — 环境变量 & 策略参数
  2. ExchangeInterface    — Bitget 沙箱封装 (U本位合约)
  3. IndicatorCalculator  — 本地量化计算 (EMA200 / RSI14 / ATR14 / ADX14)
  4. DeepSeekAnalyst      — AI 信号审核 (含多TF + 市场情绪上下文)
  5. MarketContextManager — 市场情绪 & 新闻管理
  6. TradeLogger          — 日志持久化 (JSONL)
  7. RiskMonitor          — 日内风控 (亏损锁 + 日重置 + 手续费追踪)
  8. TradeExecutor        — 风控 & 下单执行 (动态仓位 + 部分止盈)
  9. DeepSeekQuantBot     — 主循环编排器 (每 5 分钟)
"""

import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import time
from datetime import timedelta
from time_utils import now, today_str, now_iso, now_str, datetime_from_iso
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ============================================================================
# ◎ SessionManager — 交易时段感知 (v2.3)
# ============================================================================
class SessionManager:
    """
    根据当前时间自动调整策略参数。
    加密货币波动性因时段/星期而异：
      - 工作日欧美重叠盘(20-24 CST): 波动最大，放宽条件加大仓位
      - 工作日亚洲盘(8-16): 正常波动
      - 工作日低活跃期(0-8): 波动小，收紧条件
      - 周末: 低量震荡，大幅放宽ADX但要缩仓
    """

    @staticmethod
    def get_session(base_adx: int = 20, base_vol: float = 1.5) -> Dict[str, Any]:
        """v3.7: 加密货币真实波动时段 — 7 时段模型 (UTC+8 / CST)。

        加密市场波动规律 (按活跃度排序):
          1. 美股重叠盘 20:00-00:00 — 美+欧重叠，峰值波动，爆仓高发
          2. 美股后半段 00:00-04:00 — 美股独导，高波动，方向性强
          3. 欧洲盘     16:00-20:00 — 欧盘主力，活跃度上升
          4. 亚洲盘     08:00-16:00 — 正常亚洲资金，波动中等
          5. 周末活跃段 14:00-02:00 — 周末也有行情，不宜全压
          6. 亚洲凌晨   04:00-08:00 — 全球真空期，最低波动
          7. 周末凌晨   02:00-14:00 — 周末最低波动

        参数含义:
          adx_threshold: 信号入场门槛 (越低越容易触发)
          margin_mult:   仓位乘数 (<1 减仓, >1 加仓)
          sl_mult:       止损宽度 (波动大→窄止损防止意外, 波动小→宽止损防止噪音)
          max_positions: 最大并发持仓数
        """
        _now = now()
        weekday = _now.weekday()
        hour = _now.hour
        is_weekend = weekday >= 5

        # ── 周末分支 ──
        if is_weekend:
            if 14 <= hour or hour < 2:
                # 周末活跃段 14:00-02:00 — 欧美散户在线
                return {"name": "weekend_active", "label": "周末活跃",
                        "adx_threshold": max(10, base_adx - 6),
                        "margin_mult": 0.7, "vol_ratio": max(0.3, base_vol * 0.2),
                        "sl_mult": 1.8, "max_positions": 4,
                        "advice": "周末活跃段-适度参与"}
            else:
                # 周末凌晨 02:00-14:00 — 全球休息
                return {"name": "weekend_dead", "label": "周末凌晨",
                        "adx_threshold": min(25, base_adx + 2),
                        "margin_mult": 0.4, "vol_ratio": max(0.5, base_vol * 0.3),
                        "sl_mult": 2.2, "max_positions": 2,
                        "advice": "周末凌晨-最小仓位"}

        # ── 工作日分支 ──
        if hour >= 20:
            # 美股重叠盘 20:00-00:00 — 波动峰值
            return {"name": "us_overlap", "label": "美股重叠",
                    "adx_threshold": max(10, base_adx - 6),
                    "margin_mult": 1.2, "vol_ratio": max(0.5, base_vol * 0.6),
                    "sl_mult": 1.2, "max_positions": 8,
                    "advice": "波动峰值-降门槛+窄止损"}
        if 0 <= hour < 4:
            # 美股后半段 00:00-04:00 — 美股主导，仍活跃
            return {"name": "us_late", "label": "美股后半",
                    "adx_threshold": max(10, base_adx - 4),
                    "margin_mult": 1.0, "vol_ratio": max(0.4, base_vol * 0.5),
                    "sl_mult": 1.3, "max_positions": 6,
                    "advice": "美股主导-方向性强"}
        if 16 <= hour < 20:
            # 欧洲盘 16:00-20:00
            return {"name": "europe", "label": "欧洲盘",
                    "adx_threshold": max(12, base_adx - 4),
                    "margin_mult": 1.1, "vol_ratio": max(0.5, base_vol * 0.6),
                    "sl_mult": 1.3, "max_positions": 6,
                    "advice": "欧洲活跃-适度放宽"}
        if 8 <= hour < 16:
            # 亚洲盘 08:00-16:00
            return {"name": "asia", "label": "亚洲盘",
                    "adx_threshold": base_adx,
                    "margin_mult": 1.0, "vol_ratio": base_vol,
                    "sl_mult": 1.5, "max_positions": 6,
                    "advice": "亚洲基准-正常参数"}
        # 亚洲凌晨 04:00-08:00 — 全球真空
        return {"name": "asia_dead", "label": "亚洲凌晨",
                "adx_threshold": min(25, base_adx + 4),
                "margin_mult": 0.5, "vol_ratio": max(0.5, base_vol * 0.4),
                "sl_mult": 1.8, "max_positions": 3,
                "advice": "全球真空-收紧+宽止损防噪音"}

    @staticmethod
    def effective_params(config) -> Dict[str, Any]:
        session = SessionManager.get_session(config.adx_threshold, config.vol_ratio_threshold)
        return {
            **session,
            "adx_threshold": session["adx_threshold"],
            "margin_min": config.adx_margin_min * session["margin_mult"],
            "margin_max": config.adx_margin_max * session["margin_mult"],
            "vol_ratio": session["vol_ratio"],
            "sl_atr_mult": config.sl_atr_mult * session["sl_mult"],
        }


# ============================================================================
# 日志
# ============================================================================
LOG_FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s"
LOG_DATE = "%Y-%m-%d %H:%M:%S"
# v3.6: 强制 UTF-8 输出，解决 Windows GBK 终端中文乱码
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE)

# ============================================================================
# 1. ConfigManager — 配置管理
# ============================================================================
logger = logging.getLogger("QuantBot")
logging.getLogger("ccxt").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ============================================================================
# 1. ConfigManager — 配置管理
# ============================================================================
class ConfigManager:
    """从 .env 加载配置，集中管理所有策略参数"""

    # ── 默认监控币种（无 .env 配置时使用）──
    DEFAULT_SYMBOLS = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
        "DOGE/USDT:USDT", "DOT/USDT:USDT", "LINK/USDT:USDT",
        "LTC/USDT:USDT",
    ]

    def __init__(self, env_path: str = ".env"):
        # 如果 .env 不存在，尝试项目根目录
        script_dir = os.path.dirname(os.path.abspath(__file__))
        full_env_path = os.path.join(script_dir, env_path) if not os.path.isabs(env_path) else env_path
        if os.path.isfile(full_env_path):
            load_dotenv(full_env_path)
        elif os.path.isfile(env_path):
            load_dotenv(env_path)
        else:
            logger.warning(f".env 文件未找到: {full_env_path}，使用现有环境变量")

        # ── API 密钥 ──
        self.bitget_api_key    = os.getenv("BITGET_API_KEY", "")
        self.bitget_secret     = os.getenv("BITGET_SECRET", "")
        self.bitget_passphrase = os.getenv("BITGET_PASSPHRASE", "")
        self.deepseek_api_key  = os.getenv("DEEPSEEK_API_KEY", "")

        # ── 策略参数 ──
        self.leverage      = 20      # 杠杆倍数
        self.margin_ratio  = 0.05    # 单笔保证金 = 可用余额的 5% (ADX动态时为基础值)

        # ── K线 & 指标 ──
        self.timeframe     = "15m"   # 15 分钟 K 线
        self.kline_limit   = 100     # 拉取最近 100 根
        self.ema_period    = 200     # EMA200
        self.rsi_period    = 14      # RSI14
        self.atr_period    = 14      # ATR14

        # ── 信号阈值 ──
        self.rsi_oversold   = 40     # RSI < 40 → 回调超卖
        self.rsi_overbought = 60     # RSI > 60 → 反弹超买
        self.adx_threshold  = 16     # ADX < 16 → 无趋势震荡，跳过信号 (v3.6: 20→16, 自学习反馈过滤过严)
        self.adx_period     = 14     # ADX 计算周期

        # ── 成交量确认 (v2.1) ──
        self.vol_ratio_threshold = float(os.getenv("VOL_RATIO_THRESHOLD", "1.5"))
        self.vol_lookback = int(os.getenv("VOL_LOOKBACK", "20"))

        # ── BTC 联动过滤 (v2.1) ──
        self.btc_drop_block_long = float(os.getenv("BTC_DROP_BLOCK_LONG", "-0.02"))
        self.btc_pump_block_short = float(os.getenv("BTC_PUMP_BLOCK_SHORT", "0.03"))
        self.btc_filter_timeframe = os.getenv("BTC_FILTER_TF", "1h")

        # ── 动量突破策略 (v2.4) ──
        self.momentum_enabled = os.getenv("MOMENTUM_ENABLED", "true").lower() == "true"
        self.momentum_adx_threshold = float(os.getenv("MOMENTUM_ADX", "25"))
        self.momentum_breakout_bars = int(os.getenv("MOMENTUM_BARS", "20"))
        self.momentum_rsi_min = float(os.getenv("MOMENTUM_RSI_MIN", "55"))
        self.momentum_rsi_max = float(os.getenv("MOMENTUM_RSI_MAX", "78"))
        self.momentum_ema_period = int(os.getenv("MOMENTUM_EMA", "50"))
        self.momentum_sl_mult = float(os.getenv("MOMENTUM_SL_MULT", "2.5"))
        self.momentum_margin_ratio = float(os.getenv("MOMENTUM_MARGIN", "0.03"))
        self.momentum_max_slots = int(os.getenv("MOMENTUM_MAX_SLOTS", "2"))

        # ── 资金费率 (v2.1) ──
        self.funding_warn_long = float(os.getenv("FUNDING_WARN_LONG", "0.0005"))
        self.funding_warn_short = float(os.getenv("FUNDING_WARN_SHORT", "-0.0005"))

        # ── 止盈止损 (v2: 部分止盈) ──
        self.sl_atr_mult    = 1.5    # 止损距离 = 1.5 × ATR
        self.tp_atr_mult    = 3.0    # 止盈距离 = 3.0 × ATR (保留兼容)
        self.tp_split_ratios = self._parse_float_list(
            os.getenv("TP_SPLIT_RATIOS", "0.5,0.5"), [0.5, 0.5]
        )
        self.tp_atr_mults = self._parse_float_list(
            os.getenv("TP_ATR_MULTS", "2.0,3.0"), [2.0, 3.0]
        )

        # ── 仓位管理 (v2: ADX动态 + 并发限制 + 滑点) ──
        self.max_concurrent_positions = int(os.getenv("MAX_CONCURRENT_POSITIONS", "4"))
        self.adx_margin_min = float(os.getenv("ADX_MARGIN_MIN", "0.03"))
        self.adx_margin_max = float(os.getenv("ADX_MARGIN_MAX", "0.08"))
        self.slippage_buffer = float(os.getenv("SLIPPAGE_BUFFER", "0.0005"))

        # ── 多时间周期 (v2) ──
        self.higher_timeframes = ["1h", "4h"]
        self.higher_tf_limit = 60
        self.higher_tf_ema_period = 50
        self.higher_tf_adx_threshold = 25

        # ── 市场情绪 (v2) ──
        self.market_context_ttl = int(os.getenv("MARKET_CONTEXT_TTL", "14400"))  # 4小时

        # ── 日内风控 ──
        self.daily_loss_limit = 0.05  # 日内累计亏损 > 5% → 停止交易

        # ── v3.1: 初始资金 (用于计算总盈亏) ──
        self.initial_equity = float(os.getenv("INITIAL_EQUITY", "0"))

        # ── DeepSeek API ──
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        self.deepseek_url   = "https://api.deepseek.com/v1/chat/completions"
        self.deepseek_temp  = 0.3
        self.deepseek_timeout = 45
        self.deepseek_enable_tools = os.getenv("DEEPSEEK_ENABLE_TOOLS", "true").lower() == "true"

        # ── 模型废弃提醒 ──
        if self.deepseek_model == "deepseek-chat":
            logger.warning(
                "⚠️  deepseek-chat 将于 2026-07-24 废弃，已自动使用 deepseek-v4-pro。"
                "建议更新 .env: DEEPSEEK_MODEL=deepseek-v4-pro"
            )

        # ── v3.0: 网格/震荡策略 ──
        self.grid_enabled = os.getenv("GRID_ENABLED", "false").lower() == "true"
        self.grid_levels = int(os.getenv("GRID_LEVELS", "4"))
        self.grid_tp_atr = float(os.getenv("GRID_TP_ATR", "1.5"))
        self.grid_sl_atr = float(os.getenv("GRID_SL_ATR", "1.0"))
        self.grid_position_ratio = float(os.getenv("GRID_POSITION_RATIO", "0.02"))
        self.grid_min_spacing_pct = float(os.getenv("GRID_MIN_SPACING_PCT", "0.005"))

        # ── v3.0: 实盘安全 ──
        self.min_position_value = float(os.getenv("MIN_POSITION_VALUE", "50"))
        self.profit_fee_multiplier = float(os.getenv("PROFIT_FEE_MULTIPLIER", "3"))
        self.limit_fallback_enabled = os.getenv("LIMIT_FALLBACK_ENABLED", "true").lower() == "true"
        self.limit_fallback_offset = float(os.getenv("LIMIT_FALLBACK_OFFSET", "0.001"))
        self.order_confirmation_enabled = os.getenv("ORDER_CONFIRMATION_ENABLED", "true").lower() == "true"
        self.order_confirmation_timeout = int(os.getenv("ORDER_CONFIRMATION_TIMEOUT", "15"))
        self.emergency_stop_enabled = os.getenv("EMERGENCY_STOP_ENABLED", "true").lower() == "true"
        self.emergency_drawdown = float(os.getenv("EMERGENCY_DRAWDOWN", "0.03"))
        self.emergency_window = int(os.getenv("EMERGENCY_WINDOW", "300"))

        # ── v3.0: AI 深度集成 ──
        self.cryptopanic_api_key = os.getenv("CRYPTOPANIC_API_KEY", "")
        self.news_sentiment_enabled = os.getenv("NEWS_SENTIMENT_ENABLED", "false").lower() == "true"
        self.deepseek_anomaly_detection = os.getenv("DEEPSEEK_ANOMALY_DETECTION", "false").lower() == "true"
        self.deepseek_market_commentary = os.getenv("DEEPSEEK_MARKET_COMMENTARY", "false").lower() == "true"
        # ── v3.6: AI 持仓审核 (定期审查已持仓是否需要提前退出) ──
        self.ai_position_review_enabled = os.getenv("AI_POSITION_REVIEW_ENABLED", "true").lower() == "true"
        self.ai_position_review_interval = int(os.getenv("AI_POSITION_REVIEW_INTERVAL", "3"))
        self.ai_position_review_min_roi = float(os.getenv("AI_POSITION_REVIEW_MIN_ROI", "0.05"))
        # ── v3.6: 浮亏自动止损 ──
        self.auto_sl_enabled = os.getenv("AUTO_SL_ENABLED", "true").lower() == "true"
        self.auto_sl_roi_threshold = float(os.getenv("AUTO_SL_ROI_THRESHOLD", "-0.30"))
        # ── v3.6: Kelly 仓位系数 (由自学习引擎动态更新) ──
        self.kelly_multiplier = 1.0
        self.auto_sl_min_hours = float(os.getenv("AUTO_SL_MIN_HOURS", "1.0"))

        # ── v3.7: 僵尸仓 & 波动熔断 ──
        self.stale_exit_hours = float(os.getenv("STALE_EXIT_HOURS", "4.0"))
        self.stale_roi_limit = float(os.getenv("STALE_ROI_LIMIT", "0.02"))

        # ── v3.1: 自我学习引擎 ──
        self.self_learner_enabled = os.getenv("SELF_LEARNER_ENABLED", "true").lower() == "true"

        # ── v3.2: 移动止损 ──
        self.trailing_sl_enabled = os.getenv("TRAILING_SL_ENABLED", "true").lower() == "true"
        self.trailing_sl_activation_atr = float(os.getenv("TRAILING_SL_ACTIVATION_ATR", "1.0"))
        self.trailing_sl_distance_atr = float(os.getenv("TRAILING_SL_DISTANCE_ATR", "1.5"))

        # ── 沙箱标记（供 auto_tune 调整策略） ──
        self.is_sandbox = os.getenv("BITGET_SANDBOX", "true").lower() == "true"

        # ── v3.0: 投资组合管理 ──
        self.portfolio_manager_enabled = os.getenv("PORTFOLIO_MANAGER_ENABLED", "false").lower() == "true"
        self.correlation_window_hours = int(os.getenv("CORRELATION_WINDOW_HOURS", "24"))
        self.correlation_threshold = float(os.getenv("CORRELATION_THRESHOLD", "0.8"))
        self.max_correlated_positions = int(os.getenv("MAX_CORRELATED_POSITIONS", "2"))
        self.sector_max_positions = int(os.getenv("SECTOR_MAX_POSITIONS", "2"))
        self.strength_ranking_enabled = os.getenv("STRENGTH_RANKING_ENABLED", "true").lower() == "true"

        # ── v2.0: 灵活币种配置 ──
        self._load_symbols()

        # ── v4.0: 运行时参数持久化 ──
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._runtime_params_path = os.path.join(script_dir, "runtime_params.json")
        self._load_runtime_params()

        self._validate()

    # ════════════════════════════════════════════
    # v4.0: 运行时参数持久化
    # ════════════════════════════════════════════

    RUNTIME_PARAMS = [
        "adx_threshold", "vol_ratio_threshold",
        "sl_atr_mult", "tp_atr_mult_1", "tp_atr_mult_2",
        "adx_margin_min", "adx_margin_max",
        "momentum_margin_ratio", "momentum_margin_max",
        "rsi_oversold", "rsi_overbought",
        "kelly_multiplier", "tp_atr_mults",
    ]

    def save_runtime_params(self):
        """v4.0: 持久化运行时被调整的参数到 JSON"""
        try:
            data = {}
            for attr in self.RUNTIME_PARAMS:
                val = getattr(self, attr, None)
                if val is not None:
                    data[attr] = val
            # 处理 tp_atr_mults (list)
            if hasattr(self, 'tp_atr_mults') and isinstance(self.tp_atr_mults, list):
                data["tp_atr_mults"] = self.tp_atr_mults
            data["_saved_at"] = now_iso()
            tmp = self._runtime_params_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._runtime_params_path)
        except Exception as e:
            logger.debug(f"💾 运行时参数保存失败: {e}")

    def _load_runtime_params(self):
        """v4.0: 从 JSON 恢复运行时参数 (覆盖 .env 默认值)"""
        if not os.path.exists(self._runtime_params_path):
            return
        try:
            with open(self._runtime_params_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            restored = 0
            for attr in self.RUNTIME_PARAMS:
                if attr in data:
                    setattr(self, attr, data[attr])
                    restored += 1
            if restored > 0:
                logger.info(
                    f"💾 已恢复 {restored} 个运行时参数 "
                    f"(保存于 {data.get('_saved_at', '?')})"
                )
        except Exception as e:
            logger.warning(f"💾 运行时参数加载失败: {e}")

    def _load_symbols(self):
        """v2.0: 从 symbols.json + .env 加载监控币种。
        优先级: .env MONITOR_SYMBOLS > symbols.json > 默认列表
        支持: ALL, TOP_20, TOP_50 等动态模式
        """
        self.symbol_configs: Dict[str, Dict] = {}  # {base: {tier, enabled, sector, ...}}
        self.symbols_mode = "fixed"
        self.symbols_top_n = 0

        # 1. 加载 symbols.json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        symbols_json_path = os.path.join(script_dir, "symbols.json")
        if os.path.exists(symbols_json_path):
            try:
                with open(symbols_json_path, "r", encoding="utf-8") as f:
                    sj = json.load(f)
                for base, cfg in sj.get("symbols", {}).items():
                    self.symbol_configs[base] = {
                        "tier": cfg.get("tier", "primary"),
                        "enabled": cfg.get("enabled", True),
                        "sector": cfg.get("sector", "Other"),
                    }
                self.symbols_default_tier = sj.get("default_tier", "primary")
                self.symbols_default_enabled = sj.get("default_enabled", True)
                logger.debug(f"📋 已加载 symbols.json ({len(self.symbol_configs)} 个币种配置)")
            except Exception as e:
                logger.warning(f"⚠️  symbols.json 加载失败: {e}")

        # 2. 从 .env 读取 (覆盖)
        env_symbols = os.getenv("MONITOR_SYMBOLS", "").strip()
        if env_symbols:
            env_upper = env_symbols.upper()
            if env_upper == "ALL":
                self.symbols_mode = "all"
            elif env_upper.startswith("TOP_"):
                self.symbols_mode = "top_n"
                try:
                    self.symbols_top_n = int(env_upper.split("_")[1])
                except ValueError:
                    self.symbols_top_n = 20
            else:
                # 手动逗号分隔列表
                bases = [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
                self.SYMBOLS = [f"{b}/USDT:USDT" for b in bases]
                for b in bases:
                    if b not in self.symbol_configs:
                        self.symbol_configs[b] = {"tier": "primary", "enabled": True, "sector": "Other"}
                return  # 手动模式直接返回

        # 3. 根据 mode 构建 SYMBOLS
        if self.symbols_mode == "fixed":
            tier_filter = os.getenv("SYMBOLS_TIER", "primary").strip()
            bases = [
                b for b, cfg in self.symbol_configs.items()
                if cfg.get("enabled", True) and (
                    tier_filter == "all" or cfg.get("tier", "primary") == tier_filter
                )
            ]
            if not bases:
                bases = [s.replace("/USDT:USDT", "") for s in self.DEFAULT_SYMBOLS]
            self.SYMBOLS = [f"{b}/USDT:USDT" for b in bases]
        elif self.symbols_mode in ("all", "top_n"):
            # 动态模式: 先填默认列表, 等 exchange 初始化后 resolve
            self.SYMBOLS = list(self.DEFAULT_SYMBOLS)

    def resolve_dynamic_symbols(self, exchange):
        """v2.0: 在 exchange 就绪后解析 ALL/TOP_N 模式"""
        if self.symbols_mode not in ("all", "top_n"):
            return

        try:
            markets = exchange.fetch_markets()
            usdt_perps = [
                m for m in markets
                if m.get("type") == "swap"
                and m.get("quote") == "USDT"
                and m.get("active", False)
            ]
            # 按 24h 成交量排序
            usdt_perps.sort(key=lambda m: m.get("info", {}).get("baseVolume", 0) or 0, reverse=True)

            if self.symbols_mode == "top_n":
                usdt_perps = usdt_perps[:self.symbols_top_n]

            bases = []
            for m in usdt_perps:
                base = m.get("base", "")
                if base:
                    bases.append(base)
                    # 自动分配 tier 和 sector
                    if base not in self.symbol_configs:
                        self.symbol_configs[base] = {
                            "tier": "auto", "enabled": True,
                            "sector": self._guess_sector(base),
                        }

            self.SYMBOLS = [f"{b}/USDT:USDT" for b in bases]
            logger.info(
                f"📋 动态币种: {len(bases)} 个 "
                f"({'全部' if self.symbols_mode == 'all' else f'TOP_{self.symbols_top_n}'})"
            )
        except Exception as e:
            logger.warning(f"⚠️  动态币种解析失败: {e}, 使用默认列表")
            self.SYMBOLS = list(self.DEFAULT_SYMBOLS)

    @staticmethod
    def _guess_sector(base: str) -> str:
        """根据币种名称猜测板块"""
        base_upper = base.upper()
        if base_upper == "BTC":
            return "StoreOfValue"
        if base_upper in ("ETH", "SOL", "BNB", "AVAX", "NEAR", "APT", "ARB", "OP", "SUI", "TIA", "SEI", "STRK"):
            return "SmartContract"
        if base_upper in ("ADA", "DOT", "LTC", "ETC", "ATOM", "FIL"):
            return "LegacyL1"
        if base_upper in ("XRP",):
            return "Payments"
        if base_upper in ("DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"):
            return "Meme"
        if base_upper in ("LINK", "PYTH"):
            return "Oracle"
        if base_upper in ("UNI", "AAVE", "MKR", "INJ", "RUNE", "JUP", "ENA"):
            return "DeFi"
        if base_upper in ("FET", "RENDER", "TAO", "WLD", "AKT"):
            return "AI"
        return "Other"

    @staticmethod
    def _parse_float_list(raw: str, default: List[float]) -> List[float]:
        try:
            return [float(x.strip()) for x in raw.split(",") if x.strip()]
        except Exception:
            return default

    def _validate(self):
        """全面校验配置 (v2.5)"""
        errors = []

        # ── API 密钥 ──
        for key, val in [
            ("BITGET_API_KEY", self.bitget_api_key),
            ("BITGET_SECRET", self.bitget_secret),
            ("BITGET_PASSPHRASE", self.bitget_passphrase),
            ("DEEPSEEK_API_KEY", self.deepseek_api_key),
        ]:
            if not val:
                errors.append(f".env 缺失 {key}")

        # ── 数值范围 ──
        if not (1 <= self.leverage <= 125):
            errors.append(f"LEVERAGE={self.leverage} 超出范围 [1, 125]")
        if not (0.01 <= self.margin_ratio <= 0.5):
            errors.append(f"MARGIN_RATIO={self.margin_ratio} 超出范围 [0.01, 0.5]")
        if not (0.01 <= self.daily_loss_limit <= 0.5):
            errors.append(f"DAILY_LOSS_LIMIT={self.daily_loss_limit} 超出范围 [0.01, 0.5]")
        if self.adx_margin_min > self.adx_margin_max:
            errors.append(
                f"ADX_MARGIN_MIN({self.adx_margin_min}) > ADX_MARGIN_MAX({self.adx_margin_max})"
            )
        if not (1 <= self.adx_threshold <= 60):
            errors.append(f"ADX_THRESHOLD={self.adx_threshold} 超出范围 [1, 60]")

        # ── TP 比例必须成对且总和 ≈ 1.0 ──
        if len(self.tp_split_ratios) != len(self.tp_atr_mults):
            errors.append(
                f"TP_SPLIT_RATIOS={len(self.tp_split_ratios)} 项 != "
                f"TP_ATR_MULTS={len(self.tp_atr_mults)} 项"
            )
        else:
            ratio_sum = sum(self.tp_split_ratios)
            if abs(ratio_sum - 1.0) > 0.05:
                errors.append(f"TP_SPLIT_RATIOS 总和={ratio_sum:.2f}，应为 ~1.0")

        # ── K 线周期有效性 (ccxt 只支持有限值) ──
        valid_tf = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w", "1M"}
        if self.timeframe not in valid_tf:
            errors.append(f"TIMEFRAME='{self.timeframe}' 无效，应为 {valid_tf}")
        for tf in self.higher_timeframes:
            if tf not in valid_tf:
                errors.append(f"HIGHER_TIMEFRAMES 含 '{tf}' 无效")

        # ── 并发持仓数 ──
        if not (1 <= self.max_concurrent_positions <= 20):
            errors.append(
                f"MAX_CONCURRENT_POSITIONS={self.max_concurrent_positions} 超出 [1, 20]"
            )

        if errors:
            raise ValueError(
                f"❌ 配置校验失败 ({len(errors)} 项):\n  " + "\n  ".join(errors)
            )

    def __repr__(self) -> str:
        return (
            f"Config(leverage={self.leverage}x, margin={self.margin_ratio*100:.0f}%, "
            f"SL={self.sl_atr_mult}xATR, TP={self.tp_atr_mults}xATR, "
            f"symbols={len(self.SYMBOLS)}, tf={self.timeframe}, "
            f"max_pos={self.max_concurrent_positions}, "
            f"margin_range=[{self.adx_margin_min*100:.0f}%-{self.adx_margin_max*100:.0f}%], "
            f"vol>x{self.vol_ratio_threshold}, "
            f"model={self.deepseek_model})"
        )


# ============================================================================
# 2. ExchangeInterface — Bitget 交易所封装
# ============================================================================
class ExchangeInterface:
    """Bitget 沙箱对接层：只暴露策略需要的方法"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.exchange: ccxt.Exchange = self._build_exchange()
        self._trading_fees: Dict[str, float] = {}  # unified symbol -> taker fee
        self._load_markets()
        self._load_trading_fees()

    # ── 内部初始化 ──────────────────────────────────────────────
    def _build_exchange(self) -> ccxt.Exchange:
        ex = ccxt.bitget({
            "apiKey":    self.config.bitget_api_key,
            "secret":    self.config.bitget_secret,
            "password":  self.config.bitget_passphrase,
            "options": {
                "defaultType": "swap",          # U本位合约
            },
            "enableRateLimit": True,
        })
        # ★ 关键：开启沙箱模式
        ex.set_sandbox_mode(True)
        return ex

    def _load_markets(self):
        logger.info("⏳ 正在加载 Bitget 沙箱市场信息 …")
        for attempt in range(5):
            try:
                self.exchange.load_markets()
                swap_count = sum(1 for m in self.exchange.markets.values() if m.get("swap"))
                logger.info(f"✅ Bitget 沙箱就绪 | {swap_count} 个永续合约可用")
                return
            except Exception as e:
                if attempt < 4:
                    wait = (attempt + 1) * 5
                    logger.warning(f"⚠️  加载市场失败 (尝试{attempt+1}/5): {e}，{wait}秒后重试...")
                    time.sleep(wait)
                else:
                    logger.error(f"❌ 加载市场失败 (已重试5次): {e}")
                    raise

    def _load_trading_fees(self):
        """加载 USDT 合约交易费率"""
        try:
            raw_fees = self.exchange.fetch_trading_fees({'productType': 'USDT-FUTURES'})
            for market_id, fee_info in raw_fees.items():
                if market_id in self.exchange.markets_by_id:
                    unified = self.exchange.markets_by_id[market_id]['symbol']
                    self._trading_fees[unified] = float(fee_info.get('taker', 0.0006))
            logger.info(f"💸 已加载 {len(self._trading_fees)} 个币种交易费率")
        except Exception as e:
            logger.warning(f"⚠️  加载费率失败: {e}，使用默认 0.06% taker")

    # ── 缓存 (v2.5: 避免同一周期内重复 API 调用) ──
    def _cache_fresh(self, key: str, ttl: float = 30.0) -> bool:
        """检查缓存是否有效"""
        if not hasattr(self, '_cache'):
            self._cache = {}
            self._cache_ts = {}
        if key not in self._cache or key not in self._cache_ts:
            return False
        return (time.time() - self._cache_ts.get(key, 0)) < ttl

    def _cache_get(self, key: str):
        return self._cache.get(key) if hasattr(self, '_cache') else None

    def _cache_set(self, key: str, value):
        if not hasattr(self, '_cache'):
            self._cache = {}
            self._cache_ts = {}
        self._cache[key] = value
        self._cache_ts[key] = time.time()

    # ── 公开方法 ────────────────────────────────────────────────
    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        """拉取默认周期 K 线 → DataFrame"""
        return self.fetch_ohlcv_tf(symbol, self.config.timeframe, self.config.kline_limit)

    def fetch_ohlcv_tf(self, symbol: str, timeframe: str, limit: int = 50) -> pd.DataFrame:
        """拉取指定时间周期的 K 线 → DataFrame"""
        raw = self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        )
        if not raw:
            raise RuntimeError(f"{symbol} {timeframe} 未返回任何 K 线数据")

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        return df

    def _fetch_balance_dict(self) -> Dict[str, float]:
        """拉取 USDT 余额字典 (带缓存，30s TTL)"""
        cache_key = "balance"
        if self._cache_fresh(cache_key, 30):
            return self._cache_get(cache_key)
        try:
            bal = self.exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            result = {
                "free": float(usdt.get("free", 0) or 0),
                "total": float(usdt.get("total", 0) or 0),
                "used": float(usdt.get("used", 0) or 0),
            }
            self._cache_set(cache_key, result)
            return result
        except Exception:
            return {"free": 10000.0, "total": 10000.0, "used": 0.0}

    def fetch_usdt_balance(self) -> float:
        """查询 USDT 可用余额（带缓存，沙箱降级：失败时返回模拟余额）"""
        try:
            bal = self._fetch_balance_dict()
            free = bal["free"]
            logger.info(f"💰 USDT 可用余额: {free:.2f}")
            return free
        except Exception as e:
            err_msg = str(e)[:120]
            logger.warning(f"⚠️  余额查询不可用 ({err_msg})，使用模拟余额 10000 USDT")
            return 10000.0

    def fetch_total_balance(self) -> float:
        """查询 USDT 总余额 (带缓存，free + used)"""
        try:
            return self._fetch_balance_dict()["total"]
        except Exception:
            return self.fetch_usdt_balance()

    def get_account_summary(self) -> Dict[str, Any]:
        """
        返回账户全景 (v2.5): 余额 + 持仓权益 + 未实现盈亏。
        解决 status.json 只显示余额的问题——有持仓时看起来像亏了，
        实际上大部分是保证金 + 未实现盈亏。
        """
        total_balance = self.fetch_total_balance()
        free = self.fetch_usdt_balance()
        positions = self.get_open_positions()

        total_upnl = 0.0
        total_margin = 0.0
        pos_list = []

        for sym, pos in positions.items():
            contracts = float(pos.get("contracts", 0) or 0)
            upnl = float(pos.get("unrealizedPnl", 0) or 0)
            margin = float(pos.get("initialMargin", 0) or 0)
            entry = float(pos.get("entryPrice", 0) or 0)
            mark = float(pos.get("markPrice", 0) or 0)
            pos_side_raw = pos.get("side") or pos.get("info", {}).get("holdSide", "")
            side = "LONG" if str(pos_side_raw).lower() in ("long", "buy") else "SHORT"

            total_upnl += upnl
            total_margin += margin
            pos_list.append({
                "symbol": sym.replace("/USDT:USDT", ""),
                "side": side,
                "contracts": abs(contracts),
                "entry_price": round(entry, 4),
                "mark_price": round(mark, 4),
                "unrealized_pnl": round(upnl, 4),
                "margin": round(margin, 4),
            })

        # ccxt total 已包含未实现盈亏 (等于 Bitget accountEquity)，不要重复加
        equity = total_balance

        return {
            "total_balance": round(total_balance, 2),   # = Bitget accountEquity (含浮盈)
            "free": round(free, 2),                      # 可用余额
            "used_margin": round(total_margin, 2),       # 已用保证金
            "equity": round(equity, 2),                  # 总权益 = ccxt total (已含浮盈)
            "unrealized_pnl": round(total_upnl, 4),      # 未实现盈亏 (仅供参考)
            "positions_detail": pos_list,
        }

    def get_open_positions(self) -> Dict[str, Any]:
        """返回当前持仓字典 (v2.7: 批量查询，1 次 API 替代 10 次)"""
        cache_key = "positions"
        if self._cache_fresh(cache_key, 30):
            cached = self._cache_get(cache_key)
            if isinstance(cached, dict):
                return cached

        positions: Dict[str, Any] = {}
        # ── v2.7: 优先批量查询所有币种 (1 次 API)，失败则逐币种降级 ──
        try:
            all_positions = self.exchange.fetch_positions(self.config.SYMBOLS)
            if all_positions:
                for p in all_positions:
                    sym = p.get("symbol", "")
                    if sym and float(p.get("contracts", 0) or 0) != 0:
                        positions[sym] = p
        except Exception:
            # 降级：逐币种查询
            for sym in self.config.SYMBOLS:
                try:
                    pos = self.exchange.fetch_position(sym)
                    if pos and float(pos.get("contracts", 0) or 0) != 0:
                        positions[sym] = pos
                except Exception:
                    pass  # 沙箱环境可能不支持

        if positions:
            logger.info(f"📊 当前持仓: {list(positions.keys())}")
        self._cache_set(cache_key, positions)
        return positions

    def count_open_positions(self) -> int:
        """快速查询持仓数量"""
        return len(self.get_open_positions())

    def set_leverage(self, symbol: str, leverage: int):
        """设置逐仓杠杆"""
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info(f"⚙️  {symbol} 杠杆 → {leverage}x")
        except Exception as e:
            err = str(e)
            if "same as current" in err.lower() or "already" in err.lower():
                logger.info(f"⚙️  {symbol} 杠杆已是 {leverage}x")
            else:
                logger.warning(f"⚠️  设置杠杆失败 {symbol}: {err}")

    def get_taker_fee(self, symbol: str) -> float:
        """获取 taker 费率 (小数形式, 如 0.0006 = 0.06%)"""
        if symbol in self._trading_fees:
            return self._trading_fees[symbol]
        # 尝试通过 market id 查找
        try:
            market = self.exchange.market(symbol)
            market_id = market.get('id', '')
            if market_id in self._trading_fees:
                return self._trading_fees[market_id]
        except Exception:
            pass
        return 0.0006  # 默认 0.06%

    def create_market_order_with_partial_tp(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl_price: float,
        tp_parts: List[Tuple[float, float]],
    ) -> Optional[Dict]:
        """
        下单市价单 + 止损 + 部分止盈 (v2)

        tp_parts: [(tp_price, amount_ratio), ...]
          - 第一批 TP 附带在主单上（原子化）
          - 后续 TP 作为独立 reduceOnly 限价单
        """
        if not tp_parts:
            logger.error(f"❌ tp_parts 为空 {symbol}")
            return None

        try:
            logger.info(
                f"🔔 下单(分步SL/TP): {symbol} {side.upper()} | "
                f"数量={amount}张 | SL={sl_price:.4f}"
            )

            # ── v3.6 重构: 市价开仓 + place-pos-tpsl 一次设 SL/TP ──

            # 1. 下市价单（纯开仓）
            order = self.exchange.create_order(
                symbol=symbol, type="market", side=side, amount=amount,
                params={'tradeSide': 'open', 'marginMode': 'crossed'},
            )
            logger.info(f"✅ 主单成交: {symbol} | ID={order.get('id', 'N/A')}")

            # 2. 用 place-pos-tpsl API 一次设 SL+TP (自动替换旧单，不累积)
            # 从成交单获取实际入场价
            fill_price = float(order.get("price") or order.get("average") or sl_price)
            if fill_price <= 0:
                fill_price = sl_price
            # v4.0: 使用 tp_parts 中的第一个 TP 价格 (而非硬编码 2%)
            if tp_parts and len(tp_parts) > 0:
                tp_price = tp_parts[0][0]
                # 验证 TP 方向 (从成交价重新校验)
                if side == "SHORT" and tp_price >= fill_price:
                    tp_price = fill_price * 0.98  # 回退
                elif side == "buy" and tp_price <= fill_price:
                    tp_price = fill_price * 1.02  # 回退
            else:
                # 无 tp_parts 时的回退
                tp_price = fill_price * 0.98 if side in ("sell", "SHORT") else fill_price * 1.02
            self.set_position_sl_tp(symbol, side, sl_price, tp_price)

            # v4.0: 多级 TP (第2级+) 用独立计划单补充
            if tp_parts and len(tp_parts) > 1:
                raw_symbol = symbol.split(":")[0].replace("/", "")
                hold_side = "short" if side in ("sell", "SHORT") else "long"
                for tp_p, ratio in tp_parts[1:]:
                    try:
                        # v4.0 fix: 使用 price_to_precision 适配不同价格精度
                        tp_str = self.exchange.price_to_precision(symbol, tp_p)
                        tp_params = {
                            "symbol": raw_symbol,
                            "productType": "usdt-futures",
                            "marginCoin": "USDT",
                            "holdSide": hold_side,
                            "planType": "pos_profit",
                            "triggerPrice": tp_str,
                            "triggerType": "mark_price",
                            "executePrice": tp_str,  # 必须 > 0，与 triggerPrice 一致
                        }
                        tp_r = self.exchange.private_mix_post_v2_mix_order_place_tpsl_order(tp_params)
                        if tp_r.get("code") == "00000":
                            logger.info(f"  📏 附加TP @ {tp_str} ({ratio*100:.0f}%)")
                        else:
                            logger.warning(f"  ⚠️ 附加TP失败: {tp_r.get('msg','?')}")
                    except Exception as e:
                        logger.warning(f"  ⚠️ 附加TP异常: {e}")

            return order

        except Exception as e:
            logger.error(f"❌ 部分TP下单失败 {symbol} {side}: {e}")
            return None

    def get_contract_size(self, symbol: str) -> float:
        """获取合约面值 (1 张合约 = ? 个标的资产)"""
        try:
            market = self.exchange.market(symbol)
            return float(market.get("contractSize", 1.0))
        except Exception:
            return 1.0

    def get_min_amount(self, symbol: str) -> float:
        """获取最小下单数量"""
        try:
            market = self.exchange.market(symbol)
            return float(market["limits"]["amount"]["min"])
        except Exception:
            return 1.0

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """获取当前资金费率 (v2.1 新增)"""
        try:
            rate = self.exchange.fetch_funding_rate(symbol)
            if isinstance(rate, dict):
                return float(rate.get("fundingRate", 0) or 0)
            return float(rate)
        except Exception as e:
            logger.debug(f"获取 {symbol} 资金费率失败: {e}")
            return None

    def fetch_closed_position_pnl(self, symbol: str,
                                   since: float = None) -> Optional[Dict]:
        """
        v4.0: 从 Bitget V2 持仓历史 API 获取最近已平仓的真实 PnL。
        参考: https://www.bitget.com/api-doc/contract/position/Get-History-Position

        symbol: "BTC/USDT:USDT"
        since:   Unix 时间戳 (秒), 查询此后平仓的仓位
        Returns: {"pnl": float, "exit_price": float, "fee": float, "open_fee": float}
        """
        try:
            params = {
                "symbol": symbol.replace("/USDT:USDT", "USDT"),
                "productType": "usdt-futures",
                "limit": "3",
            }
            if since:
                params["startTime"] = str(int(since * 1000))
            # V2 API: GET /api/v2/mix/position/history-position
            resp = self.exchange.privateMixGetV2MixPositionHistoryPosition(params)
            if isinstance(resp, dict) and resp.get("code") == "00000":
                records = resp.get("data", {}).get("list", [])
                if not records:
                    return None
                latest = records[0]
                return {
                    "pnl": float(latest.get("pnl", 0) or 0),
                    "exit_price": float(latest.get("closeAvgPrice", 0) or 0),
                    "fee": float(latest.get("closeFee", 0) or 0),
                    "open_fee": float(latest.get("openFee", 0) or 0),
                    "holding_ms": int(latest.get("holdTime", 0) or 0),
                }
            # V2 返回非 00000 (非错误情况: 无记录等)
            logger.debug(f"📊 history-position V2: code={resp.get('code')} msg={resp.get('msg','')}")
            return None
        except Exception as e:
            logger.debug(f"📊 获取 {symbol} 平仓历史失败: {e}")
            return None

    def fetch_btc_change(self, timeframe: str = "1h") -> Optional[float]:
        """获取 BTC 在指定周期的涨跌幅 (v2.1 新增)"""
        try:
            btc_symbol = "BTC/USDT:USDT"
            df = self.fetch_ohlcv_tf(btc_symbol, timeframe, limit=2)
            if len(df) >= 2:
                prev_close = float(df["close"].iloc[-2])
                curr_close = float(df["close"].iloc[-1])
                return (curr_close - prev_close) / prev_close
        except Exception as e:
            logger.debug(f"获取 BTC 涨跌幅失败: {e}")
        return None

    # ── v3.0: 网格/安全层新增方法 ──

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """获取所有活跃挂单"""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception:
            return []

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """取消单个订单"""
        try:
            self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.warning(f"取消订单失败 {order_id}: {e}")
            return False

    @staticmethod
    @staticmethod
    def _is_reduce_only(order: Dict) -> bool:
        """判断是否为减仓/止损止盈单 (兼容 ccxt + Bitget info 字段)"""
        if order.get("reduceOnly"):
            return True
        info = order.get("info", {})
        if str(info.get("tradeSide", "")).lower() == "close":
            return True
        if str(info.get("planStatus", "")).lower() == "live":
            return True
        return False

    @staticmethod
    def _has_position_tpsl(self, symbol: str, entry_price: float, side: str) -> Tuple[bool, bool]:
        """
        检测持仓是否有 SL/TP 保护。

        方法:
          1. (主力) fetch_positions → info.stopLoss/takeProfit — 支持 pos-tpsl
          2. (兜底) fetch_open_orders(stop=True) — 独立计划单

        side: "buy"/"LONG" 或 "sell"/"SHORT"
        返回 (has_sl, has_tp)
        """
        has_sl, has_tp = False, False

        # 方法1: fetch_positions — 最可靠，沙箱实测返回 stopLoss/takeProfit
        try:
            pos_list = self.exchange.fetch_positions([symbol])
            for p in pos_list:
                if abs(float(p.get("contracts", 0) or 0)) <= 0:
                    continue
                info = p.get("info", {})
                sl_val = info.get("stopLoss", info.get("stopLossPrice", ""))
                tp_val = info.get("takeProfit", info.get("takeProfitPrice", ""))
                if sl_val and str(sl_val) not in ("0", "", "None"):
                    has_sl = True
                if tp_val and str(tp_val) not in ("0", "", "None"):
                    has_tp = True
        except Exception:
            pass

        # 方法2: 独立计划单 (兜底，用于非 pos-tpsl 方式设定的 SL/TP)
        if not has_sl or not has_tp:
            try:
                stop_orders = self.exchange.fetch_open_orders(symbol, params={"stop": True}) or []
                side_is_long = str(side).upper() in ("BUY", "LONG")
                for o in stop_orders:
                    trigger = float(o.get("info", {}).get("triggerPrice", 0) or 0)
                    if trigger <= 0 or entry_price <= 0:
                        continue
                    if side_is_long:
                        if trigger < entry_price:
                            has_sl = True
                        if trigger > entry_price:
                            has_tp = True
                    else:
                        if trigger > entry_price:
                            has_sl = True
                        if trigger < entry_price:
                            has_tp = True
            except Exception:
                pass

        return (has_sl, has_tp)

    def cancel_all_orders(self, symbol: str) -> int:
        """取消某币种所有挂单，返回取消数量"""
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            count = 0
            for o in orders:
                try:
                    self.exchange.cancel_order(o.get("id", ""), symbol)
                    count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

    def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        params: dict = None
    ) -> Optional[Dict]:
        """下达限价单"""
        try:
            return self.exchange.create_order(
                symbol=symbol, type="limit", side=side,
                amount=amount, price=price, params=params or {},
            )
        except Exception as e:
            logger.error(f"限价单失败 {symbol} {side}: {e}")
            return None

    def fetch_order(self, order_id: str, symbol: str) -> Optional[Dict]:
        """获取单个订单状态"""
        try:
            return self.exchange.fetch_order(order_id, symbol)
        except Exception:
            return None

    def create_market_order_close(
        self, symbol: str, amount: float, side: str, pos_side: str = ""
    ) -> Optional[Dict]:
        """
        市价平仓单（紧急停止 / AI平仓 / 自动止损用）。
        v3.6 fix: 根据 Bitget v2 API 文档，双向持仓模式平仓规则：
          - 平多: side=buy, tradeSide=close (不是 sell!)
          - 平空: side=sell, tradeSide=close (不是 buy!)
          - 双向模式禁止传 reduceOnly (会导致 40774)
          - holdSide 不是请求参数，仅出现在持仓返回中
        pos_side: "long" or "short"
        """
        try:
            # 优先使用 ccxt 的 close_position (内部处理了参数转换)
            if pos_side and hasattr(self.exchange, 'close_position'):
                return self.exchange.close_position(symbol, pos_side)

            # 回退: v2 API 双向模式参数
            # pos_side="long" → close long → side=buy, tradeSide=close
            # pos_side="short" → close short → side=sell, tradeSide=close
            close_side_map = {"long": "sell", "short": "buy"}  # 平多=卖出, 平空=买回
            close_side = close_side_map.get(pos_side, side)
            return self.exchange.create_order(
                symbol=symbol, type="market", side=close_side,
                amount=amount, params={"tradeSide": "close", "marginMode": "crossed"},
            )
        except Exception as e:
            logger.error(f"市价平仓失败 {symbol}: {e}")
            return None

    def is_sandbox(self) -> bool:
        """是否在沙箱环境中"""
        return getattr(self.exchange, "sandbox_mode", True)

    def set_position_sl_tp(
        self, symbol: str, side: str, sl_price: float, tp_price: float
    ) -> bool:
        """
        v3.6 重构: 使用 Bitget v2 place-pos-tpsl API，一次调用设好 SL+TP，自动替换旧的。
        文档: POST /api/v2/mix/order/place-pos-tpsl
        彻底解决重复计划单累积问题。
        """
        try:
            # 兼容 "buy"/"sell" 和 "LONG"/"SHORT" 两种传参
            side_upper = side.upper() if isinstance(side, str) else ""
            if side_upper in ("BUY", "LONG"):
                hold_side = "long"
            elif side_upper in ("SELL", "SHORT"):
                hold_side = "short"
            else:
                hold_side = "long" if side.lower() == "buy" else "short"

            # 获取价格精度 (使用 ccxt 的价格格式化)
            try:
                sl_str = self.exchange.price_to_precision(symbol, sl_price)
                tp_str = self.exchange.price_to_precision(symbol, tp_price)
                sl_val = float(sl_str)
            except Exception:
                sl_str = str(round(sl_price, 2))
                tp_str = str(round(tp_price, 2))
                sl_val = float(sl_str)

            # 验证 SL/TP 方向并修正 (v4.0: 加 TP 校验)
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                mark = float(ticker.get("mark", ticker.get("last", 0)))
                if mark > 0:
                    # SL 校验
                    if hold_side == "long" and sl_val >= mark:
                        sl_str = self.exchange.price_to_precision(symbol, mark * 0.995)
                    elif hold_side == "short" and sl_val <= mark:
                        sl_str = self.exchange.price_to_precision(symbol, mark * 1.005)
                    # TP 校验 (v4.0: 新增)
                    tp_val = float(tp_str)
                    if hold_side == "long" and tp_val <= mark:
                        tp_str = self.exchange.price_to_precision(symbol, mark * 1.02)
                        logger.warning(f"⚠️  TP方向修正: LONG TP必须>mark({mark}), 设为{mark*1.02:.4f}")
                    elif hold_side == "short" and tp_val >= mark:
                        tp_str = self.exchange.price_to_precision(symbol, mark * 0.98)
                        logger.warning(f"⚠️  TP方向修正: SHORT TP必须<mark({mark}), 设为{mark*0.98:.4f}")
            except Exception:
                pass

            raw_symbol = symbol.split(":")[0].replace("/", "")
            params = {
                "symbol": raw_symbol,
                "productType": "usdt-futures",
                "marginCoin": "USDT",
                "holdSide": hold_side,
                "stopLossTriggerPrice": sl_str,
                "stopLossTriggerType": "mark_price",
                "stopLossExecutePrice": sl_str,       # 必须 > 0，与 triggerPrice 一致
                "stopSurplusTriggerPrice": tp_str,
                "stopSurplusTriggerType": "mark_price",
                "stopSurplusExecutePrice": tp_str,     # 必须 > 0
            }
            result = self.exchange.private_mix_post_v2_mix_order_place_pos_tpsl(params)
            code = result.get("code", "")
            if code == "00000":
                logger.info(f"🛡️🎯 {symbol} SL={sl_str} TP={tp_str} → pos-tpsl OK")
                # v4.0 fix: 沙箱 fetch_position 不返回 stopLoss/takeProfit,
                # code=00000 就是设成功了，直接信任。不再用 fetch_position 验证。
                return True

            # v4.0: place-pos-tpsl 失败 → 降级用独立计划单
            logger.warning(f"⚠️  pos-tpsl失败(code={code}), 降级为独立计划单...")
            return self._set_sl_tp_via_plan_orders(symbol, hold_side, sl_str, tp_str, raw_symbol)
        except Exception as e:
            logger.error(f"❌ {symbol} SL/TP 异常: {e}")
            return False

    def _set_sl_tp_via_plan_orders(self, symbol: str, hold_side: str,
                                    sl_str: str, tp_str: str,
                                    raw_symbol: str) -> bool:
        """v4.0: 用独立 plan order 设 SL/TP (place-pos-tpsl 的降级方案)"""
        try:
            ok = True
            # 撤旧止损单
            try:
                old = self.exchange.fetch_open_orders(symbol, params={"stop": True}) or []
                for o in old:
                    if self._is_reduce_only(o):
                        self.exchange.cancel_order(str(o.get('id', '')), symbol)
            except Exception:
                pass

            # 下止损单 (pos_loss)
            sl_params = {
                "symbol": raw_symbol,
                "productType": "usdt-futures",
                "marginCoin": "USDT",
                "holdSide": hold_side,
                "planType": "pos_loss",
                "triggerPrice": sl_str,
                "triggerType": "mark_price",
                "executePrice": sl_str,  # v4.0 fix: 必须 > 0，用 triggerPrice 同值
            }
            sl_r = self.exchange.private_mix_post_v2_mix_order_place_tpsl_order(sl_params)
            if sl_r.get("code") != "00000":
                logger.error(f"❌ SL计划单失败: {sl_r.get('msg','?')}")
                ok = False
            else:
                logger.info(f"🛡️ SL计划单: {sl_str}")

            # 下止盈单 (pos_profit)
            tp_params = {
                "symbol": raw_symbol,
                "productType": "usdt-futures",
                "marginCoin": "USDT",
                "holdSide": hold_side,
                "planType": "pos_profit",
                "triggerPrice": tp_str,
                "triggerType": "mark_price",
                "executePrice": tp_str,  # v4.0 fix: 必须 > 0
            }
            tp_r = self.exchange.private_mix_post_v2_mix_order_place_tpsl_order(tp_params)
            if tp_r.get("code") != "00000":
                logger.error(f"❌ TP计划单失败: {tp_r.get('msg','?')}")
                ok = False
            else:
                logger.info(f"🛡️ TP计划单: {tp_str}")

            return ok
        except Exception as e:
            logger.error(f"❌ 独立计划单异常: {e}")
            return False


# ============================================================================
# 3. IndicatorCalculator — 本地量化计算
# ============================================================================
class IndicatorCalculator:
    """
    纯 Python + pandas 本地计算；
    不依赖交易所 API 的指标数据。

    指标：
      - EMA(N)     指数移动均线
      - RSI(14)    相对强弱指标 (Wilder smoothing)
      - ATR(14)    平均真实波幅 (Wilder smoothing)
      - ADX(14)    平均趋向指数 (Wilder smoothing)
    """

    def __init__(self, config: ConfigManager):
        self.ema_period = config.ema_period
        self.rsi_period = config.rsi_period
        self.atr_period = config.atr_period
        self.adx_period = config.adx_period

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """输入 OHLCV DataFrame，追加 ema, rsi, atr, adx, macd, bb 列"""
        df = df.copy()
        close = df["close"]

        df["ema"] = close.ewm(span=self.ema_period, adjust=False).mean()
        df["rsi"] = self._wilder_rsi(close, self.rsi_period)
        df["atr"] = self._wilder_atr(df, self.atr_period)
        df["adx"] = self._wilder_adx(df, self.adx_period)

        # ── v3.2: MACD (12, 26, 9) ──
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        # ── v3.2: Bollinger Bands (20, 2) ──
        bb_mean = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        df["bb_upper"] = bb_mean + 2 * bb_std
        df["bb_lower"] = bb_mean - 2 * bb_std
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / bb_mean  # 带宽

        return df

    def compute_tf_indicators(self, df: pd.DataFrame, ema_period: int = 50) -> pd.DataFrame:
        """为多时间周期计算简化指标 (EMA50 + ATR + ADX)"""
        df = df.copy()
        close = df["close"]

        df["ema"] = close.ewm(span=ema_period, adjust=False).mean()
        df["atr"] = self._wilder_atr(df, self.atr_period)
        df["adx"] = self._wilder_adx(df, self.adx_period)

        return df

    @staticmethod
    def _wilder_adx(df: pd.DataFrame, period: int) -> pd.Series:
        """Wilder's ADX — 平均趋向指数"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)

        mask_plus = (up_move > down_move) & (up_move > 0)
        mask_minus = (down_move > up_move) & (down_move > 0)

        plus_dm[mask_plus] = up_move[mask_plus]
        minus_dm[mask_minus] = down_move[mask_minus]

        smooth_plus_dm = plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        smooth_minus_dm = minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

        plus_di = 100.0 * smooth_plus_dm / atr.replace(0, np.nan)
        minus_di = 100.0 * smooth_minus_dm / atr.replace(0, np.nan)

        di_sum = plus_di + minus_di
        dx = (plus_di - minus_di).abs() / di_sum.replace(0, np.nan) * 100.0

        adx = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        return adx

    @staticmethod
    def _wilder_rsi(series: pd.Series, period: int) -> pd.Series:
        """Wilder's RSI"""
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[avg_loss == 0] = 100.0
        return rsi

    @staticmethod
    def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
        """Wilder's ATR"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        return atr


# ============================================================================
# 4. MarketContextManager — 市场情绪 & 新闻管理 (v2 新增)
# ============================================================================
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
            pass

        # 2. 是否在热门榜单中
        if base.upper() in [t.upper() for t in self.trending_cache]:
            result["is_trending"] = True

        return json.dumps(result, ensure_ascii=False)

# ============================================================================
# 5. DeepSeekAnalyst — AI 信号审核
# ============================================================================
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
            pass

    def review_signal(
        self,
        symbol: str,
        direction: str,
        price: float,
        ema: float,
        rsi: float,
        atr: float,
        recent_closes: List[float],
        adx: float = 0.0,
        market_regime: Optional[str] = None,
        tf_context: Optional[Dict[str, Any]] = None,
        funding_rate: Optional[float] = None,
        vol_ratio: float = 1.0,
        strategy: str = "pullback",
        confidence: int = 50,
        bonuses: Optional[list] = None,
    ) -> Tuple[str, str]:
        """
        将候选信号打包成 Prompt 发给 DeepSeek。
        返回: (decision, reason)
          decision ∈ {"CONFIRM", "REJECT"}
        """
        # ── 构造 System Prompt（含市场环境上下文） ──
        base_currency = symbol.split("/")[0]

        system_prompt = (
            "你是一名顶级加密货币首席量化分析师。你需要审核由量化策略生成的交易信号。\n\n"
            "## 信号规则回顾 (默认阈值，实际可能因市场偏向自动调整)\n"
            "- 回调做多: 价格 > EMA200（上涨趋势），且 RSI 超卖（默认<40，牛市中可放宽到55）\n"
            "- 回调做空: 价格 < EMA200（下跌趋势），且 RSI 超买（默认>60，熊市中可放宽到45）\n"
            "- 动量追多: 价格突破近期高点 + ADX>25 + RSI在55-78动量区（强势不回调）\n"
            "- 动量策略止损更宽仓位更小，适合捕捉单边趋势\n\n"
            "## 额外参考指标\n"
            "- 成交量比率 (VolRatio): 当前K线量 / 近20根均量\n"
            "- 资金费率: 正值=多头拥挤(做多谨慎)，负值=空头拥挤(做空谨慎)\n"
            "- 交易时段: 波动性因时段而异，周末/凌晨信号需更谨慎评估\n\n"
            "## 你的任务\n"
            "1. 结合价格位置、动量指标、多时间周期趋势、成交量、资金费率和宏观市场情绪，判断信号胜率\n"
            "2. 如果有明显的反向风险、震荡市特征、或宏观环境不利，请 REJECT\n"
            "3. 成交量不足、资金费率极端不利时更应谨慎\n"
            "4. 如果大趋势支持且宏观环境有利，可以更积极 CONFIRM\n"
            "5. 快速做出判断，不要过度分析\n\n"
            "## 【必须遵守】输出规则\n"
            "你的回复必须以一个 JSON 对象结尾，不要只输出分析。\n"
            "JSON 格式: {\"decision\": \"CONFIRM\"| \"REJECT\", \"reason\": \"一句话原因\"}\n"
            "示例结尾: {\"decision\": \"CONFIRM\", \"reason\": \"趋势强劲+RSI超卖+多周期共振\"}\n"
            "示例结尾: {\"decision\": \"REJECT\", \"reason\": \"资金费率极端+成交量萎缩+逆势信号\"}\n"
            "注意: JSON 必须作为回复的最后一部分，不要用 ``` 包裹，直接输出。"
        )

        # ── v3.0: 注入新闻上下文 ──
        if self.news_context:
            system_prompt += f"\n\n## 最新加密货币新闻\n{self.news_context}\n"
            system_prompt += "请综合考虑以上新闻信息来辅助判断信号风险。\n"

        # ── v3.1: 注入交易智慧 ──
        if self.wisdom_context:
            system_prompt += f"\n{self.wisdom_context}\n"
            system_prompt += "请参考以上历史交易教训来辅助判断。\n"

        # ── 注入市场宏观环境 ──
        if self.market_ctx:
            market_text = self.market_ctx.get_system_context()
            if market_text:
                system_prompt += f"\n\n## 当前市场宏观环境\n{market_text}\n"
                system_prompt += (
                    "\n请综合考虑以上宏观环境信息来辅助判断。"
                    "极度恐惧时对做多信号可以更宽松，极度贪婪时对做多信号要更谨慎。"
                )

        # ── 构造 User Prompt ──
        recent_str = ", ".join(f"{c:.4f}" for c in recent_closes[-5:])

        # 多时间周期上下文
        tf_section = ""
        if tf_context:
            for tf_name, ctx in tf_context.items():
                if ctx and ctx.get("trend") != "unknown":
                    tf_section += (
                        f"\n{tf_name}趋势: {ctx['trend']} | "
                        f"{tf_name}EMA{self.config.higher_tf_ema_period}: {ctx.get('ema50', 'N/A')} | "
                        f"{tf_name}ADX: {ctx.get('adx', 'N/A')} | "
                        f"{tf_name}状态: {ctx.get('regime', 'unknown')}"
                    )
        if market_regime:
            regime_labels = {
                "strong_trend": "强趋势市（大趋势明确，顺势信号胜率高）",
                "weak_trend": "弱趋势市（有方向但不够强，注意假突破）",
                "ranging": "震荡市（无明确方向，信号胜率低）",
                "conflicting": "多周期冲突（方向不一致，建议观望）",
            }
            tf_section += (
                f"\n综合市场状态: {regime_labels.get(market_regime, market_regime)}"
            )

        # 资金费率 + 交易时段上下文 (v2.1 + v2.3)
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)
        funding_section = ""
        if funding_rate is not None:
            funding_pct = funding_rate * 100
            if funding_rate > self.config.funding_warn_long:
                funding_note = "⚠️ 多头拥挤，做多需谨慎"
            elif funding_rate < self.config.funding_warn_short:
                funding_note = "⚠️ 空头拥挤，做空需谨慎"
            else:
                funding_note = "正常"
            funding_section = (
                f"\n资金费率: {funding_pct:+.4f}% ({funding_note})"
            )
        session_section = (
            f"\n当前交易时段: {session['label']} — {session['advice']}"
        )

        user_prompt = f"""当前时间: {now_str()}

交易对: {symbol}
信号策略: {strategy}  (pullback=回调抄底, momentum=追涨突破)
信号方向: {direction}
量化和AI置信度: {confidence}/100 {' | 加成: ' + ', '.join(bonuses) if bonuses else ''}
当前价格: {price:.4f}
EMA({self.config.ema_period}): {ema:.4f}
RSI({self.config.rsi_period}): {rsi:.2f}
ADX({self.config.adx_period}): {adx:.2f}
ATR({self.config.atr_period}): {atr:.4f}
成交量比率(当前/均量): {vol_ratio:.2f}
最近5根K线收盘价: [{recent_str}]
{tf_section}{funding_section}{session_section}

请审核以上信号。你的回复必须以 JSON 结尾，不要只输出分析。"""

        # 追加: 强制 JSON 结尾提醒
        user_prompt += '\n\n⚠️ 请务必在回复末尾输出: {"decision": "CONFIRM"|"REJECT", "reason": "一句话原因"}'

        # v4.0: 告知 AI 当前实际阈值（熊市/牛市偏向已自动调整）
        if strategy == "pullback":
            if direction == "SHORT":
                user_prompt += f'\n📌 当前做空回调阈值: RSI>{self.config.rsi_overbought}（可能因市场偏向已自动降至最低45）'
            else:
                user_prompt += f'\n📌 当前做多回调阈值: RSI<{self.config.rsi_oversold}（可能因市场偏向已自动升至最高55）'
        user_prompt += '\n📌 请以实际RSI值判断，而非死守默认阈值。如果多周期趋势一致且资金费率支持，RSI接近阈值即可通过。'

        try:
            # 构建请求体
            request_body = {
                "model": self.config.deepseek_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": self.config.deepseek_temp,
                "max_tokens": 800,  # v4.0: 增加空间，确保分析后有空间输出JSON
                "response_format": {"type": "json_object"},  # v4.0: 强制JSON输出
            }

            # 可选: 添加 Function Call 工具
            if self.config.deepseek_enable_tools:
                request_body["tools"] = self.TOOLS
                request_body["tool_choice"] = "auto"

            resp = self.session.post(
                self.config.deepseek_url,
                json=request_body,
                timeout=self.config.deepseek_timeout,
            )

            if resp.status_code != 200:
                logger.error(f"❌ DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_threshold:
                    self._circuit_open = True
                    logger.error(f"🔌 DeepSeek 断路器熔断! 连续 {self._consecutive_failures} 次失败")
                return "REJECT", f"API错误 {resp.status_code}"

            body = resp.json()
            msg = body["choices"][0]["message"]

            # ── 处理 Function Calls ──
            tool_calls = msg.get("tool_calls")
            if tool_calls and self.market_ctx:
                logger.info(f"🔧 DeepSeek 请求了 {len(tool_calls)} 个 tool call(s)")
                # 执行 function calls
                tool_results = []
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    if func_name == "get_crypto_sentiment":
                        try:
                            args = json.loads(tc["function"]["arguments"])
                            asset = args.get("asset", base_currency)
                            sentiment_data = self.market_ctx.get_sentiment_for_symbol(
                                f"{asset}/USDT"
                            )
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": sentiment_data,
                            })
                            logger.info(f"📊 获取 {asset} 情绪数据完成")
                        except Exception as e:
                            logger.warning(f"⚠️  Tool call 执行失败: {e}")
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": json.dumps({"error": str(e)}),
                            })

                if tool_results:
                    # 二次调用: 带 tool 结果继续
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                        msg,
                        *tool_results,
                    ]
                    resp2 = self.session.post(
                        self.config.deepseek_url,
                        json={
                            "model": self.config.deepseek_model,
                            "messages": messages,
                            "temperature": self.config.deepseek_temp,
                            "max_tokens": 800,  # v4.0: 增加空间，确保分析后有空间输出JSON
                            "response_format": {"type": "json_object"},
                            "tool_choice": "none",
                        },
                        timeout=self.config.deepseek_timeout,
                    )
                    if resp2.status_code == 200:
                        body = resp2.json()
                        msg = body["choices"][0]["message"]

            # ── 提取最终决策 ──
            # v3.7 fix: 始终合并 content + reasoning_content
            raw = self._merge_content_reasoning(msg)
            logger.info(f"📩 DeepSeek 原始回复: {raw[:200]}")

            decision_json = self._extract_json(raw, context="entry_review")
            decision = decision_json.get("decision", "REJECT").upper().strip()
            reason = decision_json.get("reason", "无")

            if decision not in ("CONFIRM", "REJECT"):
                logger.warning(f"⚠️  DeepSeek 返回未知 decision={decision}，视为 REJECT")
                decision = "REJECT"

            logger.info(f"📋 DeepSeek 决策: {decision} | {reason}")
            return decision, reason

        except requests.Timeout:
            logger.error("❌ DeepSeek 超时")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open = True
                logger.error(f"🔌 DeepSeek 断路器熔断! 连续 {self._consecutive_failures} 次失败")
            return "REJECT", "请求超时"
        except Exception as e:
            logger.error(f"❌ DeepSeek 异常: {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open = True
                logger.error(f"🔌 DeepSeek 断路器熔断! 连续 {self._consecutive_failures} 次失败")
            return "REJECT", str(e)

    def review_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        current_price: float,
        unrealized_pnl: float,
        roi: float,
        holding_hours: float,
        ema: float,
        rsi: float,
        atr: float,
        adx: float,
        recent_closes: List[float],
        market_regime: str = "",
        tf_context: Optional[Dict[str, Any]] = None,
        funding_rate: Optional[float] = None,
    ) -> Tuple[str, str]:
        """
        v3.6: AI 持仓审核 — 审查已持有仓位是否应该提前退出。
        返回: (decision, reason)
          decision ∈ {"HOLD", "CLOSE"}
        """
        # ── 构造 System Prompt ──
        system_prompt = (
            "你是一名顶级加密货币交易员，正在管理一个**已持有的仓位**。\n"
            "你的任务：判断是继续持有 (HOLD) 还是立即平仓 (CLOSE)。\n\n"
            "## 平仓信号规则\n"
            "- 趋势反转: 价格突破 EMA 且 RSI 确认（做多跌破EMA+RSI<45 → CLOSE; 做空升破EMA+RSI>55 → CLOSE）\n"
            "- 动量衰竭: ADX 显著下降（比开仓时下降 >10 点）→ 趋势减弱，考虑 CLOSE\n"
            "- 浮盈回撤风险: 价格在高位/低位徘徊但 RSI 从极端区回到中性 → 可能反转，考虑 CLOSE\n"
            "- 时间风险: 持仓过久（>6小时）但盈利未继续扩大 → 效率降低，考虑 CLOSE\n"
            "- 宏观不利: 多周期 TF 方向不一致 → 信号可靠性下降\n\n"
            "## 持仓规则\n"
            "- 趋势仍在延续 + 多TF一致 + 浮盈在扩大 → HOLD\n"
            "- 浮盈可观（ROI>20%）但趋势有转弱迹象 → 优先 CLOSE 锁定利润\n"
            "- 浮亏但趋势未反转 → 可 HOLD（让 SL 处理）\n"
            "- 浮亏且趋势已反转 → CLOSE 止损（比 SL 更快）\n\n"
            "## 【必须遵守】输出规则\n"
            "你的回复必须以一个 JSON 对象结尾，不要只输出分析。\n"
            "JSON 格式: {\"decision\": \"HOLD\" | \"CLOSE\", \"reason\": \"一句话原因\"}\n"
            "示例结尾: {\"decision\": \"HOLD\", \"reason\": \"趋势延续+浮盈扩大+多TF一致\"}\n"
            "示例结尾: {\"decision\": \"CLOSE\", \"reason\": \"趋势反转+RSI极端+建议锁利\"}\n"
            "注意: JSON 必须作为回复的最后一部分，不要用 ``` 包裹，直接输出。"
        )
        if self.wisdom_context:
            system_prompt += f"\n{self.wisdom_context}\n"
            system_prompt += "请参考以上历史交易教训来辅助判断。\n"

        # ── 构造 User Prompt ──
        recent_str = ", ".join(f"{c:.4f}" for c in recent_closes[-5:])

        # 多时间周期
        tf_section = ""
        if tf_context:
            for tf_name, ctx in tf_context.items():
                if ctx and ctx.get("trend") != "unknown":
                    tf_section += (
                        f"\n{tf_name}趋势: {ctx['trend']} | "
                        f"{tf_name}EMA: {ctx.get('ema50', 'N/A')} | "
                        f"{tf_name}ADX: {ctx.get('adx', 'N/A')} | "
                        f"{tf_name}状态: {ctx.get('regime', 'unknown')}"
                    )
        if market_regime:
            regime_labels = {
                "strong_trend": "强趋势市",
                "weak_trend": "弱趋势市",
                "ranging": "震荡市（持仓不利）",
                "conflicting": "多周期冲突（高风险）",
            }
            tf_section += (
                f"\n综合市场状态: {regime_labels.get(market_regime, market_regime)}"
            )

        # 资金费率
        funding_section = ""
        if funding_rate is not None:
            funding_pct = funding_rate * 100
            if funding_rate > self.config.funding_warn_long:
                funding_note = "⚠️ 多头拥挤"
            elif funding_rate < self.config.funding_warn_short:
                funding_note = "⚠️ 空头拥挤"
            else:
                funding_note = "正常"
            funding_section = f"\n资金费率: {funding_pct:+.4f}% ({funding_note})"

        pnl_sign = "+" if unrealized_pnl >= 0 else ""
        user_prompt = f"""当前时间: {now_str()}

交易对: {symbol}
持仓方向: {direction}
入场价格: {entry_price:.4f}
当前价格: {current_price:.4f}
浮盈: {pnl_sign}{unrealized_pnl:.2f} USDT (ROI: {roi*100:.1f}%)
已持仓: {holding_hours:.1f} 小时
EMA: {ema:.4f}
RSI: {rsi:.2f}
ADX: {adx:.2f}
ATR: {atr:.4f}
最近5根K线收盘价: [{recent_str}]
{tf_section}{funding_section}

请判断应该 HOLD 还是 CLOSE。你的回复必须以 JSON 结尾，不要只输出分析。"""

        # 追加: 强制 JSON 结尾提醒
        user_prompt += '\n\n⚠️ 请务必在回复末尾输出: {"decision": "HOLD"|"CLOSE", "reason": "一句话原因"}'

        logger.info(f"🔍 正在请求 DeepSeek 审核持仓 {symbol} {direction} (ROI={roi*100:.1f}%)…")

        try:
            request_body = {
                "model": self.config.deepseek_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": self.config.deepseek_temp,
                "max_tokens": 800,  # v4.0: 增加空间，确保分析后有空间输出JSON
                "response_format": {"type": "json_object"},
            }

            resp = self.session.post(
                self.config.deepseek_url,
                json=request_body,
                timeout=self.config.deepseek_timeout,
            )

            if resp.status_code != 200:
                logger.error(f"❌ DeepSeek 持仓审核 HTTP {resp.status_code}: {resp.text[:200]}")
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_threshold:
                    self._circuit_open = True
                    logger.error("🔌 DeepSeek 断路器熔断!")
                return "HOLD", f"API错误 {resp.status_code}"

            body = resp.json()
            msg = body["choices"][0]["message"]
            # v3.7 fix: 始终合并 content + reasoning_content
            raw = self._merge_content_reasoning(msg)
            logger.info(f"📩 DeepSeek 持仓审核回复: {raw[:200]}")

            decision_json = self._extract_json(raw, context="position_review")
            decision = decision_json.get("decision", "HOLD").upper().strip()
            reason = decision_json.get("reason", "无")

            if decision not in ("HOLD", "CLOSE"):
                logger.warning(f"⚠️  DeepSeek 持仓审核返回未知 decision={decision}，视为 HOLD")
                decision = "HOLD"

            logger.info(f"📋 DeepSeek 持仓审核: {symbol} {direction} → {decision} | {reason}")
            return decision, reason

        except requests.Timeout:
            logger.error("❌ DeepSeek 持仓审核超时")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open = True
            return "HOLD", "请求超时"
        except Exception as e:
            logger.error(f"❌ DeepSeek 持仓审核异常: {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open = True
            return "HOLD", str(e)

    @staticmethod
    def _merge_content_reasoning(msg: Dict[str, Any]) -> str:
        """v3.7: 正确合并 DeepSeek 推理模型的 content + reasoning_content。

        关键修复: deepseek-v4-pro 是推理模型，JSON 决策通常在 reasoning_content，
        但 content 可能包含对话文本。之前的 `content or reasoning_content` 逻辑
        会优先取 content 并丢弃 reasoning_content（当 content 非空时），
        导致 JSON 决策被静默丢弃 → 回退到默认值 → 96.5% 解析失败。

        现在: 始终合并两者，reasoning_content 优先放在前面（含关键 JSON）。
        """
        c = (msg.get("content") or "").strip()
        r = (msg.get("reasoning_content") or "").strip()
        # reasoning_content 优先 → 确保 JSON 决策在最前面
        if r and c:
            return f"{r}\n{c}"
        return r or c

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

        # 2. 如果没有代码块，尝试正则匹配 JSON 对象 (v3.7: 支持嵌套)
        if not candidates:
            for m in re.finditer(r'\{[^{}]*"decision"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL):
                candidates.append(m.group(0))

        # 3. 尝试每个候选
        for cand in candidates:
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue

        # 4. 直接解析全文本
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
            logger.warning(f"⚠️  正则推断 decision={d}")
            return {"decision": d, "reason": "正则推断"}

        # v3.7: context 感知的默认值
        defaults = {
            "entry_review": {"decision": "REJECT", "reason": "解析失败-默认拒绝"},
            "position_review": {"decision": "HOLD", "reason": "解析失败-默认持有"},
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
            pass
        return ""


# ============================================================================
# 6. TradeLogger — 日志持久化 (JSONL)
# ============================================================================
class TradeLogger:
    """所有信号、AI 决策、交易执行记录写入 logs/trades.jsonl (v2: 扩展字段)"""

    def __init__(self, log_dir: str = "logs"):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.log_dir = os.path.join(script_dir, log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        # v3.6: 按日分割日志，防止单文件无限膨胀
        self.today = today_str()
        self.log_path = os.path.join(self.log_dir, f"trades_{self.today}.jsonl")
        logger.info(f"📝 交易日志: {self.log_path}")

    def _write(self, record: Dict[str, Any]):
        """追加一行 JSON 到日志文件（按日分割）"""
        record.setdefault("timestamp", now_iso())
        # v3.6: 检查日期是否变更，自动切换文件
        today = today_str()
        if today != self.today:
            self.today = today
            self.log_path = os.path.join(self.log_dir, f"trades_{today}.jsonl")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"写入日志失败: {e}")

    def log_signal(self, sig: Dict[str, Any]):
        """记录候选信号"""
        self._write({
            "event": "SIGNAL",
            "symbol": sig["symbol"],
            "direction": sig["direction"],
            "price": sig["price"],
            "ema": sig["ema"],
            "rsi": sig["rsi"],
            "atr": sig["atr"],
            "adx": sig.get("adx"),
            "recent_closes": sig.get("recent_closes", [])[-5:],
        })

    def log_ai_decision(self, symbol: str, direction: str,
                        decision: str, reason: str):
        """记录 DeepSeek 决策"""
        self._write({
            "event": "AI_DECISION",
            "symbol": symbol,
            "direction": direction,
            "decision": decision,
            "reason": reason,
        })

    def log_trade(self, symbol: str, direction: str, price: float,
                  sl: float, tp_info: Any, amount: int,
                  order_id: Optional[str], success: bool,
                  entry_fee: float = 0.0, total_estimated_fee: float = 0.0,
                  margin_ratio_used: float = 0.0):
        """记录交易执行 (v2: 含手续费 & 部分TP信息)"""
        self._write({
            "event": "TRADE",
            "symbol": symbol,
            "direction": direction,
            "entry_price": price,
            "sl": sl,
            "tp_info": tp_info,
            "amount_contracts": amount,
            "order_id": order_id,
            "success": success,
            "entry_fee_usdt": round(entry_fee, 4),
            "total_estimated_fee_usdt": round(total_estimated_fee, 4),
            "margin_ratio_used": round(margin_ratio_used, 4),
        })

    def log_risk(self, event_type: str, details: str):
        """记录风控事件"""
        self._write({
            "event": "RISK",
            "risk_type": event_type,
            "details": details,
        })

    def log_cycle(self, cycle: int, candidates: int, trades: int,
                  balance: float, pnl: float = 0.0,
                  cumulative_fees: float = 0.0,
                  market_regime: str = "",
                  equity: float = 0.0, unrealized_pnl: float = 0.0,
                  initial_equity: float = 0.0, all_time_pnl: float = 0.0):
        """记录每轮扫描摘要 (v3.1: 初始资金 + 总盈亏)"""
        self._write({
            "event": "CYCLE",
            "cycle": cycle,
            "candidates": candidates,
            "trades_this_cycle": trades,
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "daily_pnl_pct": round(pnl, 4),
            "cumulative_fees": round(cumulative_fees, 4),
            "market_regime": market_regime,
            "initial_equity": round(initial_equity, 2),
            "all_time_pnl": round(all_time_pnl, 2),
        })

    def log_adx_skip(self, symbol: str, adx: float):
        """记录因 ADX 过低被过滤的信号"""
        self._write({
            "event": "ADX_SKIP",
            "symbol": symbol,
            "adx": round(adx, 2),
        })

    def log_tf_skip(self, symbol: str, direction: str, reason: str,
                    tf_context: Optional[Dict[str, Any]] = None):
        """记录因多TF趋势不一致被过滤的信号 (v2 新增)"""
        self._write({
            "event": "TF_SKIP",
            "symbol": symbol,
            "direction": direction,
            "reason": reason,
            "tf_context": (
                {k: v.get("trend") for k, v in tf_context.items()}
                if tf_context else {}
            ),
        })

    def log_concurrent_skip(self, num_existing: int, max_allowed: int):
        """记录因并发持仓满被跳过的轮次 (v2 新增)"""
        self._write({
            "event": "CONCURRENT_SKIP",
            "num_existing": num_existing,
            "max_allowed": max_allowed,
        })

    def log_vol_skip(self, symbol: str, vol_ratio: float):
        """记录因成交量不足被过滤的信号 (v2.1 新增)"""
        self._write({
            "event": "VOL_SKIP",
            "symbol": symbol,
            "vol_ratio": round(vol_ratio, 2),
        })

    def log_direction_skip(self, symbol: str, direction: str, reason: str):
        """v3.7: 记录因方向胜率过低被跳过的信号"""
        self._write({
            "event": "DIRECTION_SKIP",
            "symbol": symbol,
            "direction": direction,
            "reason": reason,
        })

    def log_btc_filter(self, symbol: str, direction: str,
                       btc_change: float, reason: str):
        """记录因 BTC 联动被拦截的信号 (v2.1 新增)"""
        self._write({
            "event": "BTC_FILTER",
            "symbol": symbol,
            "direction": direction,
            "btc_change_pct": round(btc_change * 100, 2),
            "reason": reason,
        })

    # ── v3.5: 结构化日志 — 支持自学习复盘 ──

    def log_position_close(self, symbol: str, direction: str, strategy: str,
                           entry_price: float, exit_price: float,
                           pnl: float, pnl_pct: float, close_reason: str,
                           holding_hours: float = 0):
        """记录平仓事件（自学习核心数据源）"""
        self._write({
            "event": "POSITION_CLOSE",
            "symbol": symbol,
            "direction": direction,
            "strategy": strategy,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "close_reason": close_reason,  # TP / SL / MANUAL / ROTATION
            "holding_hours": round(holding_hours, 1),
        })

    def log_signal_full(self, symbol: str, direction: str, strategy: str,
                        price: float, ema: float, rsi: float, atr: float,
                        adx: float, confidence: int, bonuses: list,
                        market_regime: str):
        """记录完整信号上下文（消融测试数据源）"""
        self._write({
            "event": "SIGNAL_FULL",
            "symbol": symbol,
            "direction": direction,
            "strategy": strategy,
            "price": round(price, 2),
            "ema": round(ema, 2),
            "rsi": round(rsi, 2),
            "atr": round(atr, 4),
            "adx": round(adx, 2),
            "confidence": confidence,
            "bonuses": bonuses,
            "market_regime": market_regime,
        })

    def log_cycle_positions(self, cycle: int, positions: list):
        """记录每轮持仓快照（PnL轨迹追踪）"""
        self._write({
            "event": "CYCLE_POSITIONS",
            "cycle": cycle,
            "positions": positions,  # [{symbol, side, entry, mark, upl, margin}]
        })

    def log_sl_tp_trigger(self, symbol: str, order_type: str,
                          trigger_price: float, fill_price: float):
        """记录 SL/TP 触发（复盘止损止盈效果）"""
        self._write({
            "event": "SL_TP_TRIGGER",
            "symbol": symbol,
            "order_type": order_type,  # "SL" or "TP"
            "trigger_price": round(trigger_price, 4),
            "fill_price": round(fill_price, 4),
            "slippage": round(abs(fill_price - trigger_price) / trigger_price * 100, 4),
        })

    def log_ai_exit_decision(self, symbol: str, direction: str,
                             decision: str, reason: str,
                             entry_price: float, current_price: float,
                             roi: float, holding_hours: float):
        """v3.6: 记录 AI 持仓审核决策"""
        self._write({
            "event": "AI_EXIT_DECISION",
            "symbol": symbol,
            "direction": direction,
            "decision": decision,  # HOLD or CLOSE
            "reason": reason,
            "entry_price": round(entry_price, 4),
            "current_price": round(current_price, 4),
            "roi": round(roi * 100, 1),
            "holding_hours": round(holding_hours, 1),
        })


# ============================================================================
# 7. RiskMonitor — 日内风控
# ============================================================================
class RiskMonitor:
    """
    风控监控器 v4.0 — 连续亏损冷却 + 百分比硬止损
    ===============================================
    v4.0 核心: 连续亏损 N 次 → 暂停 M 小时 (非百分比一刀切)
      - 连续亏损 3 次 → 暂停 4 小时
      - 连续亏损 5 次 → 暂停 24 小时
      - 币种越多, 阈值越宽 (动态调整)
      - 日亏损 -8% 作为硬性兜底 (百分比锁保留)
    """

    # v4.0: 基础阈值 (10 币种)
    BASE_STREAK_WARN = 3      # 连续亏损此次数 → 暂停
    BASE_STREAK_HARD = 5      # 连续亏损此次数 → 长暂停
    BASE_COOLDOWN_WARN_H = 4  # 暂停小时
    BASE_COOLDOWN_HARD_H = 24 # 长暂停小时
    HARD_LOSS_PCT = -0.08     # 日内 -8% 硬兜底

    def __init__(self, config: ConfigManager, exchange: ExchangeInterface):
        self.config = config
        self.exchange = exchange
        self.day_start_equity: float = 0.0
        self.current_equity: float = 0.0
        self.daily_pnl_pct: float = 0.0
        self._current_date: str = ""
        self.cumulative_fees: float = 0.0
        self.initial_equity: float = config.initial_equity

        # v4.0: 连续亏损追踪
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.recent_pnls: List[float] = []  # 最近 20 笔 PnL (USDT)
        self.cooldown_until: float = 0.0     # 冷却结束时间戳
        self.cooldown_reason: str = ""

        self._refresh()
        if self.initial_equity <= 0 and self.current_equity > 0:
            self.initial_equity = self.current_equity
            logger.info(f"💰 初始资金自动检测: {self.initial_equity:.2f} USDT")

    # ════════════════════════════════════════════
    # v4.0: 动态阈值 (基于币种数)
    # ════════════════════════════════════════════

    def _get_thresholds(self) -> tuple:
        """根据监控币种数返回 (streak_warn, streak_hard, cooldown_warn_h, cooldown_hard_h)"""
        symbol_count = len(getattr(self.config, 'SYMBOLS', self.config.DEFAULT_SYMBOLS))
        if symbol_count <= 10:
            mult = 1.0
        elif symbol_count <= 20:
            mult = 1.3   # 币种多 → 阈值放宽 30%
        elif symbol_count <= 50:
            mult = 1.6   # 放宽 60%
        else:
            mult = 2.0   # 放宽 100%

        return (
            max(2, round(self.BASE_STREAK_WARN * mult)),
            max(3, round(self.BASE_STREAK_HARD * mult)),
            self.BASE_COOLDOWN_WARN_H,
            self.BASE_COOLDOWN_HARD_H,
        )

    # ════════════════════════════════════════════
    # 核心 API
    # ════════════════════════════════════════════

    def _refresh(self):
        acct = self.exchange.get_account_summary()
        self._refresh_with_data(acct)

    def _refresh_with_data(self, acct: Dict[str, Any]):
        today = today_str()
        equity = acct["equity"]
        if today != self._current_date:
            self._current_date = today
            self.cumulative_fees = 0.0
            self.day_start_equity = equity
            self.current_equity = equity
            self.daily_pnl_pct = 0.0
            logger.info(f"🌅 新交易日 {today} | 起始权益: {self.day_start_equity:.2f} USDT")
        else:
            self.current_equity = equity
            if self.day_start_equity > 0:
                self.daily_pnl_pct = (
                    (self.current_equity - self.day_start_equity) / self.day_start_equity
                )

    def record_trade_fees(self, fees: float):
        self.cumulative_fees += fees

    def record_closed_trade(self, pnl: float, symbol: str = ""):
        """v4.0: 记录已平仓交易 PnL, 追踪连续亏损"""
        is_loss = pnl < 0

        if is_loss:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        else:
            self.consecutive_wins += 1
            self.consecutive_losses = 0  # 盈利 → 重置

        self.recent_pnls.append(pnl)
        if len(self.recent_pnls) > 20:
            self.recent_pnls = self.recent_pnls[-20:]

        # 检查是否触发冷却
        self._check_streak_cooldown(symbol)

    def _check_streak_cooldown(self, symbol: str = ""):
        """v4.0: 连续亏损触发冷却"""
        now = time.time()
        streak_warn, streak_hard, cooldown_warn, cooldown_hard = self._get_thresholds()

        if self.consecutive_losses >= streak_hard:
            until = now + cooldown_hard * 3600
            if until > self.cooldown_until:
                self.cooldown_until = until
                self.cooldown_reason = (
                    f"连续亏损{self.consecutive_losses}次 "
                    f"(阈值{streak_hard}) → 暂停{cooldown_hard}h"
                )
                logger.error(f"🚨 {self.cooldown_reason}")

        elif self.consecutive_losses >= streak_warn and self.consecutive_losses < streak_hard:
            until = now + cooldown_warn * 3600
            if until > self.cooldown_until:
                self.cooldown_until = until
                self.cooldown_reason = (
                    f"连续亏损{self.consecutive_losses}次 "
                    f"(阈值{streak_warn}) → 暂停{cooldown_warn}h"
                )
                logger.error(f"⚠️  {self.cooldown_reason}")

    def can_trade(self) -> bool:
        """检查是否允许交易 (v4.0: 连续亏损冷却 + 百分比硬兜底)"""
        self._refresh()

        now = time.time()

        # 1. 冷却检查: 冷却期未过?
        if now < self.cooldown_until:
            remaining_h = (self.cooldown_until - now) / 3600
            logger.warning(
                f"⛔ 冷却中: {self.cooldown_reason} | "
                f"剩余 {remaining_h:.1f}h"
            )
            return False

        # 2. 冷却期已过 → 自动恢复
        if self.cooldown_until > 0 and now >= self.cooldown_until:
            logger.info(f"🟢 冷却期结束, 恢复交易 (连胜{self.consecutive_wins}笔)")
            self.cooldown_until = 0.0
            self.cooldown_reason = ""
            self.consecutive_losses = 0  # 重新计数

        # 3. 百分比硬兜底: -8%
        if self.daily_pnl_pct < self.HARD_LOSS_PCT:
            logger.error(
                f"🚨 日内亏损 {self.daily_pnl_pct*100:.2f}% > "
                f"{abs(self.HARD_LOSS_PCT)*100:.0f}%，硬止损！"
            )
            return False

        return True

    def can_trade_with_data(self, acct: Dict[str, Any]) -> bool:
        self._refresh_with_data(acct)
        return self.can_trade()  # 共用冷却逻辑 (不重复 refresh)

    def get_status(self) -> Dict[str, Any]:
        all_time_pnl = (self.current_equity - self.initial_equity) if self.initial_equity > 0 else 0.0
        all_time_pnl_pct = (all_time_pnl / self.initial_equity * 100) if self.initial_equity > 0 else 0.0
        streak_warn, streak_hard, _, _ = self._get_thresholds()
        cooldown_remaining = max(0, (self.cooldown_until - time.time()) / 3600) if self.cooldown_until > 0 else 0

        blocked = time.time() < self.cooldown_until or self.daily_pnl_pct < self.HARD_LOSS_PCT
        return {
            "date": self._current_date,
            "initial_equity": self.initial_equity,
            "start_equity": self.day_start_equity,
            "current_equity": self.current_equity,
            "pnl_pct": round(self.daily_pnl_pct * 100, 3),
            "all_time_pnl": round(all_time_pnl, 2),
            "all_time_pnl_pct": round(all_time_pnl_pct, 2),
            "blocked": blocked,
            "cumulative_fees": round(self.cumulative_fees, 4),
            # v4.0
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "streak_warn": streak_warn,
            "streak_hard": streak_hard,
            "cooldown_until": self.cooldown_until if self.cooldown_until > 0 else None,
            "cooldown_remaining_h": round(cooldown_remaining, 1),
            "cooldown_reason": self.cooldown_reason,
        }


# ============================================================================
# 8. TradeExecutor — 风控 & 下单 (v2: 动态仓位 + 部分止盈 + 手续费)
# ============================================================================
class TradeExecutor:
    """封装：仓位计算 → 杠杆设置 → 止盈止损计算 → 下单 (v2: 全面增强)"""

    def __init__(self, config: ConfigManager, exchange: ExchangeInterface, tlogger: TradeLogger):
        self.config = config
        self.exchange = exchange
        self.logger = tlogger
        # ── v3.0: 安全层引用（由 DeepSeekQuantBot 在加载 safety 后设置） ──
        self.safety: Any = None
        self._skip_safety: bool = True  # 默认跳过安全校验，由 bot 启用

    # v4.0: 市场状态 → 仓位/SL 乘数
    REGIME_MULTIPLIERS = {
        "strong_trend":          {"pos": 1.20, "sl": 1.00, "tp": 1.10},
        "weak_trend":            {"pos": 1.00, "sl": 1.00, "tp": 1.00},
        "ranging":               {"pos": 0.70, "sl": 0.80, "tp": 1.20},
        "low_vol_ranging":       {"pos": 0.80, "sl": 0.70, "tp": 1.30},
        "high_vol_strong_trend": {"pos": 0.80, "sl": 1.30, "tp": 0.80},
        "low_vol_weak_trend":    {"pos": 1.00, "sl": 0.85, "tp": 1.15},
        "conflicting":           {"pos": 0.50, "sl": 1.00, "tp": 1.00},
    }

    def execute(self, symbol: str, direction: str, price: float,
                atr: float, adx: float = 25.0,
                strategy: str = "pullback",
                position_multiplier: float = 1.0,
                market_regime: str = "unknown") -> Dict[str, Any]:
        """
        执行完整交易流程 (v4.0: 状态驱动仓位/SL/TP)。

        v4.0: market_regime - 市场状态标签，控制仓位乘数和 SL/TP 宽度
        v2.0: position_multiplier - 组合排名仓位系数
        """
        result = {
            "success": False, "order_id": None,
            "sl": 0.0, "tp_info": [],
            "amount": 0, "entry_fee": 0.0,
            "total_fee": 0.0, "margin_ratio_used": 0.0,
        }

        # ── 1. 保证金比例 (v3.2: 联动config调参) ──
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)
        if strategy == "momentum":
            margin_ratio = self.config.momentum_margin_ratio * session["margin_mult"]
        else:
            s_margin_min = self.config.adx_margin_min * session["margin_mult"]
            s_margin_max = self.config.adx_margin_max * session["margin_mult"]
            adx_clamped = max(20.0, min(40.0, adx))
            adx_ratio = (adx_clamped - 20.0) / (40.0 - 20.0)
            margin_ratio = s_margin_min + adx_ratio * (s_margin_max - s_margin_min)
        result["margin_ratio_used"] = margin_ratio

        # ── 2. 仓位计算 ──
        balance = self.exchange.fetch_usdt_balance()
        if balance <= 0:
            logger.warning("⚠️  余额为 0，跳过下单")
            return result

        # ── v3.6: Kelly 仓位系数 ──
        kelly_mult = 1.0
        try:
            if self.safety is not None and not self.safety._skip_safety:
                # 实盘模式: 从自学习引擎获取 Kelly 系数
                # 通过 config 的 kelly_multiplier 属性获取 (由 learner 更新)
                kelly_mult = getattr(self.config, 'kelly_multiplier', 1.0)
        except Exception:
            pass
        # v4.0: 市场状态仓位/SL 调整
        regime_cfg = self.REGIME_MULTIPLIERS.get(market_regime, {"pos": 1.0, "sl": 1.0, "tp": 1.0})
        regime_pos_mult = regime_cfg.get("pos", 1.0)
        regime_sl_mult = regime_cfg.get("sl", 1.0)
        regime_tp_mult = regime_cfg.get("tp", 1.0)

        # v4.0: ADX 动态 TP — 强趋势放远止盈让利润跑, 弱趋势收紧早锁利
        if adx > 35:
            adx_tp_mult = 1.5
        elif adx > 25:
            adx_tp_mult = 1.2
        elif adx < 15:
            adx_tp_mult = 0.6
        elif adx < 20:
            adx_tp_mult = 0.8
        else:
            adx_tp_mult = 1.0
        effective_tp_mult = regime_tp_mult * adx_tp_mult

        if regime_pos_mult != 1.0 or regime_sl_mult != 1.0 or adx_tp_mult != 1.0:
            logger.info(
                f"📊 状态驱动: {market_regime} → "
                f"仓位{regime_pos_mult:.1f}x SL{regime_sl_mult:.1f}x "
                f"TP{regime_tp_mult:.1f}x ADX-TP{adx_tp_mult:.1f}x"
            )

        margin = balance * margin_ratio * kelly_mult * position_multiplier * regime_pos_mult
        # v4.0: 单仓硬上限 8% (防止多个乘数叠加后仓位过大)
        MAX_MARGIN_PCT = 0.08
        if margin > balance * MAX_MARGIN_PCT:
            logger.warning(
                f"⚠️  仓位超限: {margin:.0f}U ({margin/balance*100:.1f}%) "
                f"→ 截断至 {balance*MAX_MARGIN_PCT:.0f}U ({MAX_MARGIN_PCT*100:.0f}%)"
            )
            margin = balance * MAX_MARGIN_PCT

        # v4.0: 总保证金上限 (防止多仓叠加耗尽余额)
        MAX_TOTAL_MARGIN_PCT = 0.50
        try:
            acct = self.exchange.get_account_summary()
            existing_margin = acct.get("used_margin", 0)
            if existing_margin + margin > balance * MAX_TOTAL_MARGIN_PCT:
                old_margin = margin
                margin = max(0, balance * MAX_TOTAL_MARGIN_PCT - existing_margin)
                if margin <= 0:
                    logger.error(
                        f"❌ 总保证金已超限: 已用{existing_margin:.0f}U "
                        f"+ 新仓{margin:.0f}U > {balance*MAX_TOTAL_MARGIN_PCT:.0f}U → 拒绝开仓"
                    )
                    result["success"] = False
                    return result
                logger.warning(
                    f"⚠️  总保证金超限: 已用{existing_margin:.0f}U "
                    f"+ 新仓{old_margin:.0f}U > {balance*MAX_TOTAL_MARGIN_PCT:.0f}U "
                    f"→ 缩减至{margin:.0f}U"
                )
        except Exception:
            pass

        # v4.0: 先用默认杠杆，后续验证后可能修正
        effective_leverage = self.config.leverage
        position_value = margin * effective_leverage
        contract_size = self.exchange.get_contract_size(symbol)
        min_amount = self.exchange.get_min_amount(symbol)

        amount_contracts = position_value / (price * contract_size)
        # v3.2: 沙箱合约面值可能偏大（BTC contractSize=1），ceil 到最小单位
        if amount_contracts < min_amount:
            amount_contracts = min_amount  # 用最小可开张数
            # 重新计算实际所需保证金和仓位价值
            position_value = amount_contracts * price * contract_size
            margin = position_value / effective_leverage  # v4.0: 用实际杠杆
            # v4.0 fix: 用 8% 上限替代 50%, 防止 min_amount 撑破仓位上限
            if margin > balance * MAX_MARGIN_PCT:
                logger.warning(
                    f"⚠️  最少需要 {amount_contracts} 张, 保证金={margin:.0f}U "
                    f"({margin/balance*100:.1f}%) > {MAX_MARGIN_PCT*100:.0f}%上限, 余额={balance:.0f}U → 拒绝 {symbol}"
                )
                return result
            result["margin_ratio_used"] = margin / balance
            logger.info(
                f"📐 {symbol} 仓位调整至最小: 张数={amount_contracts} "
                f"保证金={margin:.2f} 仓位={position_value:.2f}"
            )
        else:
            amount_contracts = math.floor(amount_contracts)
            # 如果 floor 归零但原始值 > 0，至少用 min_amount
            if amount_contracts == 0 and min_amount > 0:
                amount_contracts = min_amount
                position_value = amount_contracts * price * contract_size
                margin = position_value / effective_leverage  # v4.0: 用实际杠杆
                # v4.0 fix: 用 8% 上限替代 50%
                if margin > balance * MAX_MARGIN_PCT:
                    logger.warning(
                        f"⚠️  floor归零, 最少{min_amount}张需保证金={margin:.0f}U "
                        f"({margin/balance*100:.1f}%) > {MAX_MARGIN_PCT*100:.0f}%上限 → 拒绝 {symbol}"
                    )
                    return result
                result["margin_ratio_used"] = margin / balance
                logger.info(
                    f"📐 {symbol} floor归零, 调整为最小张数={amount_contracts} "
                    f"保证金={margin:.2f}"
                )

        result["amount"] = amount_contracts

        # ── 3. 手续费估算 ──
        taker_fee = self.exchange.get_taker_fee(symbol)
        entry_fee = position_value * taker_fee
        # 平仓费预估 (SL或TP触发，均为taker；最坏情况2次成交)
        exit_fee = position_value * taker_fee * len(self.config.tp_split_ratios)
        total_estimated_fee = entry_fee + exit_fee
        result["entry_fee"] = entry_fee
        result["total_fee"] = total_estimated_fee

        logger.info(
            f"📐 {symbol} 仓位计算: 余额={balance:.2f} | "
            f"ADX={adx:.2f} → 保证金率={margin_ratio*100:.1f}% | "
            f"保证金={margin:.2f} | 仓位价值={position_value:.2f} | "
            f"张数={amount_contracts} | "
            f"预估总手续费={total_estimated_fee:.4f} USDT"
        )

        # ── v3.0: 实盘安全检查 ──
        if not self._skip_safety and self.safety:
            # 最小仓位
            ok, reason = self.safety.check_min_position_value(position_value, symbol)
            if not ok:
                logger.warning(f"⛔ 安全校验未通过 {symbol}: {reason}")
                return result
            # 费后利润
            tp1_mult = self.config.tp_atr_mults[0] if self.config.tp_atr_mults else 2.0
            ok, reason = self.safety.check_profit_after_fees(
                position_value, atr, taker_fee, tp1_mult, price
            )
            if not ok:
                logger.warning(f"⛔ 利润不足 {symbol}: {reason}")
                return result

        # ── 4. 设置杠杆 + 验证 ──
        self.exchange.set_leverage(symbol, self.config.leverage)
        # v4.0: 验证杠杆是否生效 (沙箱可能拒绝, 默认只有10x)
        effective_leverage = self.config.leverage
        try:
            pos_check = self.exchange.fetch_position(symbol)
            actual_lev = float(pos_check.get("leverage") or pos_check.get("info", {}).get("leverage", 0) or 0)
            if actual_lev > 0 and actual_lev != self.config.leverage:
                logger.warning(
                    f"⚠️  杠杆设置失败: 请求{self.config.leverage}x → 实际{actual_lev:.0f}x"
                )
                effective_leverage = actual_lev
                # v4.0 fix: 杠杆变了，重算仓位价值和张数
                position_value = margin * effective_leverage
                amount_contracts = position_value / (price * contract_size)
                if amount_contracts < min_amount:
                    amount_contracts = min_amount
                    position_value = amount_contracts * price * contract_size
                    margin = position_value / effective_leverage
                else:
                    amount_contracts = math.floor(amount_contracts)
                    if amount_contracts == 0 and min_amount > 0:
                        amount_contracts = min_amount
                        position_value = amount_contracts * price * contract_size
                        margin = position_value / effective_leverage
                if margin > balance * MAX_MARGIN_PCT:
                    margin = balance * MAX_MARGIN_PCT
                    position_value = margin * effective_leverage
                    amount_contracts = max(min_amount, math.floor(position_value / (price * contract_size)))
                # v4.0 fix: 用最终合约数反算真实保证金，防止 min_amount 撑破上限
                real_margin = amount_contracts * price * contract_size / effective_leverage
                if real_margin > balance * MAX_MARGIN_PCT:
                    logger.warning(
                        f"⚠️  杠杆修正后保证金{real_margin:.0f}U({real_margin/balance*100:.1f}%) > "
                        f"{MAX_MARGIN_PCT*100:.0f}%上限, min={min_amount}张 — 拒绝开仓"
                    )
                    return result
                margin = real_margin
                result["amount"] = amount_contracts
                result["margin_ratio_used"] = margin / balance if balance > 0 else 0
                logger.info(
                    f"📐 杠杆修正重算: margin={margin:.2f} position_value={position_value:.2f} "
                    f"contracts={amount_contracts} lev={effective_leverage}x"
                )
        except Exception:
            pass

        # ── 5. 止损 & 部分止盈计算 ──
        if strategy == "momentum":
            sl_dist = self.config.momentum_sl_mult * atr
        else:
            sl_dist = self.config.sl_atr_mult * session["sl_mult"] * atr
        # v4.0: 市场状态 SL 宽度调整
        sl_dist *= regime_sl_mult
        # v4.0: SL 宽度硬上限 (防止多乘数叠加后 SL 过宽, 如 4.29x ATR)
        max_sl_dist = price * 0.15  # 最大 15% 价格
        min_sl_dist = price * 0.005  # 最小 0.5% (防止过于紧)
        if sl_dist > max_sl_dist:
            logger.warning(f"⚠️  SL距离{sl_dist/price*100:.1f}% > 15% → 截断")
            sl_dist = max_sl_dist
        elif sl_dist < min_sl_dist:
            sl_dist = min_sl_dist
        # 滑点缓冲: SL 向不利方向偏移
        slippage = price * self.config.slippage_buffer

        if direction == "LONG":
            side = "buy"
            sl_price = price - sl_dist - slippage
        else:
            side = "sell"
            sl_price = price + sl_dist + slippage

        if sl_price <= 0:
            logger.error(f"❌ SL 价格异常: {sl_price:.4f}")
            return result

        result["sl"] = sl_price

        # 部分止盈价格 (v4.0: 市场状态缩放TP)
        tp_parts = []
        for i, (ratio, mult) in enumerate(
            zip(self.config.tp_split_ratios, self.config.tp_atr_mults)
        ):
            tp_dist = mult * atr * effective_tp_mult  # v4.0: 趋势+ADX双因子缩放TP
            if direction == "LONG":
                tp_price = price + tp_dist
            else:
                tp_price = price - tp_dist
            if tp_price > 0:
                tp_parts.append((tp_price, ratio))
                logger.info(
                    f"📏  TP{i+1}: {tp_price:.4f} ({ratio*100:.0f}%) @ {mult}×ATR"
                )

        result["tp_info"] = [
            {"price": round(p, 4), "ratio": r} for p, r in tp_parts
        ]

        logger.info(
            f"📏 {symbol} {direction}: 入场≈{price:.4f} | "
            f"SL={sl_price:.4f} (距离={sl_dist:.4f} + 滑点={slippage:.4f}) | "
            f"盈亏比≈1:{self.config.tp_atr_mults[-1]/self.config.sl_atr_mult:.1f} | "
            f"预估手续费={total_estimated_fee:.4f}"
        )

        # ── 6. 下单 (部分止盈) ──
        # ── v3.0: 带限价后备的市价单 ──
        if not self._skip_safety and self.safety and self.config.limit_fallback_enabled:
            order = self.safety.place_with_limit_fallback(
                symbol, side, amount_contracts, sl_price, tp_parts
            )
        else:
            order = self.exchange.create_market_order_with_partial_tp(
                symbol=symbol,
                side=side,
                amount=amount_contracts,
                sl_price=sl_price,
                tp_parts=tp_parts,
            )

        if order:
            # ── v3.0: 订单成交确认 ──
            if not self._skip_safety and self.safety and self.config.order_confirmation_enabled:
                confirmed, _ = self.safety.confirm_order_fill(
                    str(order.get("id", "")), symbol,
                    self.config.order_confirmation_timeout
                )
                if not confirmed:
                    logger.error(f"⛔ 订单 {order.get('id')} 未确认成交，标记为失败")
                    result["order_id"] = str(order.get("id", "N/A"))
                    return result

            result["success"] = True
            result["order_id"] = str(order.get("id", "N/A"))
            self.logger.log_trade(
                symbol=symbol, direction=direction, price=price,
                sl=sl_price, tp_info=result["tp_info"],
                amount=amount_contracts,
                order_id=result["order_id"], success=True,
                entry_fee=entry_fee, total_estimated_fee=total_estimated_fee,
                margin_ratio_used=margin_ratio,
            )
        else:
            self.logger.log_trade(
                symbol=symbol, direction=direction, price=price,
                sl=sl_price, tp_info=result["tp_info"],
                amount=amount_contracts,
                order_id=None, success=False,
                entry_fee=entry_fee, total_estimated_fee=total_estimated_fee,
                margin_ratio_used=margin_ratio,
            )

        return result


# ============================================================================
# 9. DeepSeekQuantBot — 主循环编排器 (v2: 全面升级)
# ============================================================================
class DeepSeekQuantBot:
    """
    主控机器人 (v2):
      - 每 5 分钟扫描全部币种
      - 多时间周期趋势过滤 -> 本地指标 -> ADX过滤 -> AI 审核 -> 风控 -> 下单
      - 已有持仓的币种自动跳过
      - 市场情绪 & 新闻上下文增强 AI 决策
    """

    def __init__(self):
        logger.info("=" * 60)
        logger.info("🚀 DeepSeekQuantBot v3.0 启动中 …")
        logger.info("=" * 60)

        # 初始化模块
        self.config    = ConfigManager()
        self.exchange  = ExchangeInterface(self.config)
        # v2.0: 解析动态币种 (ALL / TOP_N)
        self.config.resolve_dynamic_symbols(self.exchange.exchange)
        self.indicator = IndicatorCalculator(self.config)
        self.market_ctx = MarketContextManager(self.config.market_context_ttl)
        self.analyst   = DeepSeekAnalyst(self.config, self.market_ctx)
        self.tlogger   = TradeLogger()
        self.riskmon   = RiskMonitor(self.config, self.exchange)
        self.executor  = TradeExecutor(self.config, self.exchange, self.tlogger)

        logger.info(f"📋 配置: {self.config}")
        logger.info(f"📋 监控币种: {self.config.SYMBOLS}")
        logger.info(f"📋 ADX阈值: {self.config.adx_threshold} | "
                     f"日内亏损上限: {self.config.daily_loss_limit*100:.0f}%")
        logger.info("📋 v2.x特性: 多TF趋势过滤 | 市场情绪 | 部分止盈 | ADX动态仓位 | 手续费追踪 | 时段感知")
        s = SessionManager.get_session()
        logger.info(f"📋 当前时段: {s['label']} | ADX阈值={s['adx_threshold']} | 仓位系数={s['margin_mult']}x | {s['advice']}")
        logger.info(f"📋 最大并发: {self.config.max_concurrent_positions} | "
                     f"保证金范围: {self.config.adx_margin_min*100:.0f}%-{self.config.adx_margin_max*100:.0f}%")

        # 初始化市场上下文
        try:
            self.market_ctx.ensure_fresh()
        except Exception as e:
            logger.warning(f"⚠️  市场上下文初始化失败: {e}")

        # 运行统计
        self.total_scans = 0
        self.total_trades = 0
        self.stats = {
            "start_time": now_iso(),
            "adx_skips": 0,
            "vol_skips": 0,
            "btc_filters": 0,
            "tf_skips": 0,
            "signals": 0,
            "ai_confirms": 0,
            "ai_rejects": 0,
            "trades": 0,
            "momentum_signals": 0,
            "momentum_trades": 0,
            "short_signals": 0,
            "short_trades": 0,
        }
        self._status_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "status.json"
        )
        # ── v2.7: 多TF分析缓存 (同一周期内复用，避免重复拉取1h/4h K线) ──
        self._tf_cache: Dict[str, Dict[str, Any]] = {}
        self._market_commentary: str = ""
        # ── v3.6: 仓位开仓时间追踪 (AI持仓审核用) ──
        self._position_open_times: Dict[str, float] = {}

        # ── v3.0: 可选特性管理器 ──
        self.grid_manager: Any = None
        self.portfolio: Any = None
        self.safety: Any = None
        self.news_client: Any = None

        if self.config.grid_enabled:
            try:
                from grid_strategy import GridManager
                self.grid_manager = GridManager(self.config, self.exchange, self.tlogger)
                logger.info("📋 网格策略: 已启用")
            except Exception as e:
                logger.warning(f"⚠️  网格策略加载失败: {e}")

        if self.config.portfolio_manager_enabled:
            try:
                from portfolio_manager import PortfolioManager
                self.portfolio = PortfolioManager(self.config, self.exchange, self.tlogger)
                logger.info("📋 投资组合管理: 已启用")
            except Exception as e:
                logger.warning(f"⚠️  投资组合管理加载失败: {e}")

        if self.config.news_sentiment_enabled:
            try:
                from news_integration import CryptoPanicClient
                self.news_client = CryptoPanicClient(
                    self.config.cryptopanic_api_key or None
                )
                logger.info("📋 新闻集成: 已启用" + (
                    " (CryptoPanic)" if self.config.cryptopanic_api_key else " (免费源)"
                ))
            except Exception as e:
                logger.warning(f"⚠️  新闻集成加载失败: {e}")

        # ── v3.7: 订单流监控 (OI) ──
        self.flow_monitor: Any = None
        try:
            from flow_monitor import FlowMonitor
            self.flow_monitor = FlowMonitor(self.exchange)
            logger.info("📋 订单流监控: 已启用 (OI追踪+背离检测)")
        except Exception as e:
            logger.warning(f"⚠️  订单流监控加载失败: {e}")

        # ── v3.7: 统一评分引擎 ──
        self.scorer: Any = None
        try:
            from signal_scorer import SignalScorer
            self.scorer = SignalScorer()
            logger.info("📋 统一评分: 已启用 (加权7维度→AUTO/AI/REJECT)")
        except Exception as e:
            logger.warning(f"⚠️  统一评分加载失败: {e}")

        # ── v3.7: 马可夫状态检测 ──
        self.markov_regime: Any = None
        try:
            from markov_regime import MarkovRegime
            self.markov_regime = MarkovRegime(window=20, threshold=0.03, min_train=50)
            logger.info("📋 马可夫状态: 已启用 (3状态转移矩阵)")
        except Exception as e:
            logger.warning(f"⚠️  马可夫状态加载失败: {e}")

        if self.config.emergency_stop_enabled or self.config.limit_fallback_enabled:
            try:
                from safety_manager import SafetyManager
                self.safety = SafetyManager(self.config, self.exchange, self.tlogger)
                self.executor.safety = self.safety
                self.executor._skip_safety = False
            except Exception as e:
                logger.warning(f"⚠️  安全层加载失败: {e}")

        # ── v3.1: 自我学习引擎 ──
        self.learner: Any = None
        if self.config.self_learner_enabled:
            try:
                from self_learner import SelfLearner
                self.learner = SelfLearner(self.config, self.tlogger)
                self.learner.purge_repetitive()  # v2.0: 清理历史垃圾智慧
                logger.info("📋 自我学习引擎: 已启用 (v2.0)")
            except Exception as e:
                logger.warning(f"⚠️  自我学习引擎加载失败: {e}")

        # ── v3.2: 移动止损 ──
        self.trailing_sl: Any = None
        if self.config.trailing_sl_enabled:
            try:
                from trailing_sl import TrailingStopManager
                self.trailing_sl = TrailingStopManager(self.config, self.exchange, self.tlogger)
                logger.info("📋 移动止损: 已启用")
            except Exception as e:
                logger.warning(f"⚠️  移动止损加载失败: {e}")

        # ── v3.2: 绩效追踪 ──
        self.perf: Any = None
        try:
            from performance_tracker import PerformanceTracker
            self.perf = PerformanceTracker()
            if self.perf.trades:
                logger.info(f"📋 绩效追踪: 已启用 ({len(self.perf.trades)}笔历史)")
            else:
                logger.info("📋 绩效追踪: 已启用")
        except Exception as e:
            logger.warning(f"⚠️  绩效追踪加载失败: {e}")

        # ── v3.2: 健康检查 ──
        self.health: Any = None
        try:
            from health_monitor import HealthMonitor
            self.health = HealthMonitor(
                self.config, self.exchange, self.analyst, self.riskmon, self.tlogger
            )
            logger.info("📋 健康检查: 已启用")
        except Exception as e:
            logger.warning(f"⚠️  健康检查加载失败: {e}")

        # ── v3.7: 遗传进化器 ──
        self.evolver: Any = None
        self._last_evolve_ts: float = 0.0
        try:
            from genetic_evolver import GeneticEvolver
            self.evolver = GeneticEvolver(
                self.config, self.config.deepseek_api_key,
                self.config.deepseek_url, self.config.deepseek_model,
            )
            logger.info("📋 遗传进化器: 已启用 (每2h)")
        except Exception as e:
            logger.warning(f"⚠️  遗传进化器加载失败: {e}")

        # ── v3.0: 打印已启用的新特性 ──
        v3_features = []
        if self.grid_manager:
            v3_features.append("网格策略")
        if self.portfolio:
            v3_features.append("投资组合管理")
        if self.news_client:
            v3_features.append("新闻集成")
        if self.safety:
            v3_features.append("实盘安全")
        if self.config.deepseek_anomaly_detection:
            v3_features.append("AI异常检测")
        if self.config.deepseek_market_commentary:
            v3_features.append("AI市场评论")
        if self.learner:
            v3_features.append("自我学习引擎")
        if self.trailing_sl:
            v3_features.append("移动止损")
        if self.perf:
            v3_features.append("绩效追踪")
        if self.health:
            v3_features.append("健康检查")
        if self.markov_regime:
            v3_features.append("马可夫状态")
        if self.flow_monitor:
            v3_features.append("订单流监控")
        if self.evolver:
            v3_features.append("遗传进化器")
        if v3_features:
            logger.info(f"📋 v3.0新特性: {' | '.join(v3_features)}")

    def _update_status_file(self, acct: Dict[str, Any] = None):
        """写入 status.json 供外部快速查看 (v2.7: 可选预取账户数据)"""
        risk = self.riskmon.get_status()
        if acct is None:
            acct = self.exchange.get_account_summary()
        status = {
            "timestamp": now_iso(),
            "runtime_hours": round(
                (now() - datetime_from_iso(self.stats["start_time"])).total_seconds() / 3600, 2
            ),
            "cycle": self.total_scans,
            # -- 账户全景 (v2.5) --
            "account": {
                "equity": acct["equity"],             # 总权益 = total + 未实现盈亏
                "total_balance": acct["total_balance"],  # 交易所 total (free + used)
                "available": acct["free"],            # 可用余额
                "used_margin": acct["used_margin"],   # 已用保证金
                "unrealized_pnl": acct["unrealized_pnl"],  # 未实现盈亏
            },
            # -- v3.1: 总盈亏 (基于初始资金) --
            "initial_equity": risk["initial_equity"],
            "all_time_pnl": risk["all_time_pnl"],
            "all_time_pnl_pct": risk["all_time_pnl_pct"],
            # -- 兼容旧字段 --
            "balance": acct["total_balance"],
            "pnl_pct": risk["pnl_pct"],
            "cumulative_fees": risk["cumulative_fees"],
            "positions": acct["positions_detail"],   # 持仓详情列表
            "position_count": len(acct["positions_detail"]),
            "max_positions": self.config.max_concurrent_positions,
            "trades": self.stats["trades"],
            "signals": self.stats["signals"],
            "momentum_signals": self.stats["momentum_signals"],
            "momentum_trades": self.stats["momentum_trades"],
            "short_signals": self.stats["short_signals"],
            "short_trades": self.stats["short_trades"],
            "performance": self.perf.get_metrics() if self.perf else {},
            "trailing_sl": {
                "enabled": self.trailing_sl is not None,
                "tracking": list(self.trailing_sl._trails.keys()) if self.trailing_sl else [],
            },
            "health": {
                "api_ok": getattr(getattr(self, 'health', None), '_last_check_ok', True),
                "circuit_breaker": self.analyst.circuit_breaker_open(),
                "error_count": getattr(getattr(self, 'health', None), '_error_count', 0),
            },
            "ai_confirm_rate": (
                round(self.stats["ai_confirms"] / self.stats["signals"], 2)
                if self.stats["signals"] > 0 else None
            ),
            "filters": {
                "adx": self.stats["adx_skips"],
                "vol": self.stats["vol_skips"],
                "btc": self.stats["btc_filters"],
                "tf": self.stats["tf_skips"],
            },
            "market": {
                "fng": self.market_ctx.fng_cache.get("value"),
                "fng_label": self.market_ctx.fng_cache.get("classification"),
                "trending": self.market_ctx.trending_cache[:5] if self.market_ctx.trending_cache else [],
            },
            "session": SessionManager.get_session(
                self.config.adx_threshold, self.config.vol_ratio_threshold),
            "market_commentary": getattr(self, '_market_commentary', ''),
        }
        try:
            # 原子写入: 先写临时文件，再 rename，避免崩溃时文件损坏
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json", dir=os.path.dirname(self._status_file)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(status, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._status_file)
            except Exception:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as e:
            logger.error(f"❌ 写入 status.json 失败: {e}")

    # ==================================================================
    # 多时间周期分析 (v2 新增)
    # ==================================================================
    def _analyze_tf_context(self, symbol: str) -> Dict[str, Any]:
        """分析 1h 和 4h 的大趋势背景 (v2.6: 带缓存，同周期内复用)"""
        # ── 缓存命中 ──
        cache_key = f"{symbol}"
        if cache_key in self._tf_cache:
            return self._tf_cache[cache_key]

        contexts = {}
        for tf in self.config.higher_timeframes:
            try:
                df = self.exchange.fetch_ohlcv_tf(symbol, tf, self.config.higher_tf_limit)
                if len(df) < 20:
                    logger.warning(f"⚠️  {symbol} {tf} K线不足 ({len(df)}根)，跳过")
                    contexts[tf] = {"trend": "unknown", "regime": "unknown"}
                    continue

                df = self.indicator.compute_tf_indicators(
                    df, self.config.higher_tf_ema_period
                )

                latest = df.iloc[-1]
                close = float(latest["close"])
                ema = float(latest["ema"])
                atr = float(latest["atr"])
                adx = float(latest["adx"])

                if pd.isna(ema) or pd.isna(adx):
                    contexts[tf] = {"trend": "unknown", "regime": "unknown"}
                    continue

                above_ema = close > ema
                trend = "bullish" if above_ema else "bearish"
                regime = "trending" if adx > self.config.higher_tf_adx_threshold else "ranging"

                contexts[tf] = {
                    "trend": trend,
                    "above_ema50": above_ema,
                    "ema50": round(ema, 4),
                    "atr": round(atr, 4),
                    "adx": round(adx, 2),
                    "regime": regime,
                }
            except Exception as e:
                logger.warning(f"⚠️  {symbol} {tf} 分析失败: {e}")
                contexts[tf] = {"trend": "unknown", "regime": "unknown"}

        self._tf_cache[cache_key] = contexts
        return contexts

    def _check_trend_alignment(
        self, direction: str, tf_context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        检查 15m 信号是否与 1h/4h 趋势一致。
        返回 (aligned: bool, reason: str)
        """
        reasons = []
        aligned = True
        for tf in self.config.higher_timeframes:
            ctx = tf_context.get(tf, {})
            trend = ctx.get("trend", "unknown")
            regime = ctx.get("regime", "unknown")

            if regime == "ranging" or trend == "unknown":
                reasons.append(f"{tf}: 震荡/未知 (跳过判断)")
                continue

            if direction == "LONG" and trend == "bullish":
                reasons.append(f"{tf}: ✅ 看涨一致")
            elif direction == "SHORT" and trend == "bearish":
                reasons.append(f"{tf}: ✅ 看跌一致")
            else:
                reasons.append(f"{tf}: ⚠️ 方向不一致 (15m={direction}, {tf}={trend})")
                aligned = False

        return aligned, "; ".join(reasons) if reasons else "无TF数据"

    def _detect_market_regime(self, tf_context: Dict[str, Any],
                              atr: float = 0, close: float = 0,
                              adx: float = 0, vol_ratio: float = 1.0) -> Dict[str, Any]:
        """v3.7: 增强市场状态识别 — 多维度回归综合判断。

        输入:
          - tf_context: 多 TF 趋势上下文 (必需)
          - atr, close: 用于波动率分级 (可选, 默认 0 → normal vol)
          - adx: 趋势强度 (可选, 默认 0 → weak)
          - vol_ratio: 量比 (可选, 默认 1.0 → 正常)

        返回 dict (向后兼容: 用 .get("regime") 取标签):
        """
        # ── 1. 多 TF 趋势一致性 (原有逻辑) ──
        regimes = []
        trends = []
        for tf in self.config.higher_timeframes:
            ctx = tf_context.get(tf, {})
            regimes.append(ctx.get("regime", "unknown"))
            trends.append(ctx.get("trend", "unknown"))
        both_trending = all(r == "trending" for r in regimes)
        any_trending = any(r == "trending" for r in regimes)
        same_direction = len(set(trends)) == 1 and "unknown" not in trends

        if both_trending and same_direction:
            trend_regime = "strong_trend"
            trend_conf = 80
        elif both_trending and not same_direction:
            trend_regime = "conflicting"
            trend_conf = 40
        elif any_trending:
            trend_regime = "weak_trend"
            trend_conf = 55
        else:
            trend_regime = "ranging"
            trend_conf = 60

        # ── 2. 波动率分级 (ATR%) ──
        atr_pct = (atr / close * 100) if close > 0 else 0
        # 参考范围: crypto 15m ATR% 通常 0.1%-3%
        if atr_pct < 0.3:
            volatility = "low"
            vol_conf = 70
        elif atr_pct < 1.0:
            volatility = "normal"
            vol_conf = 60
        else:
            volatility = "high"
            vol_conf = 75

        # ── 3. ADX 趋势强度分层 ──
        if adx < 20:
            adx_tier = "none"
            adx_conf = 65
        elif adx < 30:
            adx_tier = "weak"
            adx_conf = 55
        else:
            adx_tier = "strong"
            adx_conf = 70

        # ── 4. 综合标签 ──
        recommended = []
        detail_parts = []

        # 核心判断: 趋势 × 波动
        if trend_regime == "strong_trend":
            if volatility == "high":
                label = "high_vol_strong_trend"
                recommended = ["momentum", "pullback", "ema_cross"]
                detail_parts.append("高波强趋势-顺势追击")
            else:
                label = "strong_trend"
                recommended = ["momentum", "pullback", "ema_cross"]
                detail_parts.append("强趋势-动量/交叉优先")

        elif trend_regime == "weak_trend":
            if volatility == "low":
                label = "low_vol_weak_trend"
                recommended = ["pullback", "ema_cross"]
                detail_parts.append("低波弱趋势-回调/交叉")
            else:
                label = "weak_trend"
                recommended = ["pullback", "ema_cross"]
                detail_parts.append("弱趋势-回调/交叉")

        elif trend_regime == "ranging":
            if volatility == "low":
                label = "low_vol_ranging"
                recommended = ["grid", "pullback", "bollinger"]
                detail_parts.append("低波震荡-网格/布林优先")
            elif volatility == "high":
                label = "high_vol_ranging"
                recommended = ["pullback", "bollinger"]
                detail_parts.append("高波震荡-回调/布林")
            else:
                label = "ranging"
                recommended = ["grid", "pullback", "bollinger"]
                detail_parts.append("震荡市-网格/回调/布林")

        else:  # conflicting
            if volatility == "high":
                label = "high_vol_conflicting"
                recommended = []
                detail_parts.append("高波冲突-建议观望")
            else:
                label = "conflicting"
                recommended = ["pullback"]
                detail_parts.append("多周期冲突-保守回调")

        # ADX 微调
        if adx_tier == "strong" and "momentum" not in recommended:
            recommended.insert(0, "momentum")
            detail_parts.append("ADX强势→加推动量")
        elif adx_tier == "none" and "momentum" in recommended:
            recommended.remove("momentum")
            detail_parts.append("ADX弱势→移除动量")

        # 量比调整
        if vol_ratio > 2.0:
            detail_parts.append(f"放量{vol_ratio:.1f}x-信号可靠")
        elif vol_ratio < 0.5 and vol_ratio > 0:
            detail_parts.append("缩量-信号可信度降低")

        # ── 综合置信度 ──
        confidence = int((trend_conf + vol_conf + adx_conf) / 3)

        return {
            "regime": label,
            "volatility": volatility,
            "trend_strength": adx_tier,
            "atr_pct": round(atr_pct, 3),
            "recommended": recommended,
            "confidence": confidence,
            "detail": " | ".join(detail_parts),
        }

    # ==================================================================
    # v3.6: AI 持仓审核
    # ==================================================================
    def _review_open_positions(self, positions_detail: list):
        """
        v3.6: 定期审查所有持仓，AI 判断是否应提前平仓。
        每 N 轮执行一次（默认 N=3，即每15分钟）。
        仅审查 ROI > ai_position_review_min_roi 的仓位。
        """
        logger.info(f"🔍 AI 持仓审核: 审查 {len(positions_detail)} 个持仓 …")

        for pos_d in positions_detail:
            symbol = pos_d.get("symbol", "")
            sym_full = f"{symbol}/USDT:USDT"
            side = pos_d.get("side", "LONG")
            entry = pos_d.get("entry_price", 0)
            mark = pos_d.get("mark_price", 0)
            upl = pos_d.get("unrealized_pnl", 0)
            margin = pos_d.get("margin", 0)
            contracts = pos_d.get("contracts", 0)

            if entry <= 0 or abs(contracts) <= 0:
                continue

            roi = upl / margin if margin > 0 else 0

            # 只审核有显著浮盈或浮亏的仓位（跳过接近 0 的）
            if abs(roi) < self.config.ai_position_review_min_roi:
                logger.debug(f"🔍 {symbol} ROI={roi*100:.1f}% < {self.config.ai_position_review_min_roi*100:.0f}%，跳过审核")
                continue

            try:
                # ── 拉取 K 线 + 计算指标 ──
                df = self.exchange.fetch_ohlcv(sym_full)
                if len(df) < 50:
                    logger.debug(f"🔍 {symbol} K线不足，跳过审核")
                    continue
                df = self.indicator.compute_all(df)
                latest = df.iloc[-1]
                ema = float(latest.get("ema", entry))
                rsi = float(latest.get("rsi", 50))
                atr = float(latest.get("atr", entry * 0.01))
                adx = float(latest.get("adx", 20))
                recent_closes = [float(c) for c in df["close"].tail(10).tolist()]

                # ── 多 TF 分析 ──
                tf_context = self._analyze_tf_context(sym_full)
                regime_info = self._detect_market_regime(tf_context)
                market_regime = regime_info.get("regime", "unknown")

                # ── 估算持有时长 ──
                holding_hours = 0.5  # 默认 0.5 小时（至少已过一轮）
                open_ts = getattr(self, '_position_open_times', {}).get(sym_full)
                if open_ts:
                    holding_hours = (time.time() - open_ts) / 3600.0

                # ── 资金费率 ──
                funding_rate = None
                try:
                    funding_rate = self.exchange.fetch_funding_rate(sym_full)
                except Exception:
                    pass

                # ── 调用 AI 审核 ──
                decision, reason = self.analyst.review_position(
                    symbol=sym_full,
                    direction=side,
                    entry_price=entry,
                    current_price=mark,
                    unrealized_pnl=upl,
                    roi=roi,
                    holding_hours=holding_hours,
                    ema=ema,
                    rsi=rsi,
                    atr=atr,
                    adx=adx,
                    recent_closes=recent_closes,
                    market_regime=market_regime,
                    tf_context=tf_context,
                    funding_rate=funding_rate,
                )

                # ── 记录决策 ──
                self.tlogger.log_ai_exit_decision(
                    symbol=symbol, direction=side,
                    decision=decision, reason=reason,
                    entry_price=entry, current_price=mark,
                    roi=roi, holding_hours=holding_hours,
                )

                # v4.0: AI 解析失败 → 规则接管 (不再盲目 HOLD)
                if "失败" in str(reason) or "默认" in str(reason):
                    if roi > 0.40:
                        decision, reason = "CLOSE", f"规则接管-盈利{roi*100:.0f}%"
                        logger.info(f"📋 规则接管: {symbol} 盈利{roi*100:.0f}%→平仓")
                    elif roi < -0.25 and holding_hours > 1.0:
                        decision, reason = "CLOSE", f"规则接管-亏损{roi*100:.0f}%超1h"
                        logger.info(f"📋 规则接管: {symbol} 亏损{roi*100:.0f}%→止损")
                    elif holding_hours > 6.0 and abs(roi) < 0.03:
                        decision, reason = "CLOSE", f"规则接管-僵尸仓{holding_hours:.0f}h"
                        logger.info(f"📋 规则接管: {symbol} 僵尸仓→平仓")

                # ── 执行平仓 ──
                if decision == "CLOSE":
                    logger.info(f"🔔 AI 建议平仓 {symbol} {side}: {reason}")
                    try:
                        # v3.6 fix: 不先取消 SL/TP (会导致 Bitget 触发止损再拒绝平仓)
                        # 直接用封装好的市价平仓方法
                        cts = abs(int(contracts))
                        close_side = "buy" if side == "SHORT" else "sell"
                        pos_side = "short" if side == "SHORT" else "long"
                        close_o = self.exchange.create_market_order_close(
                            sym_full, cts, close_side, pos_side
                        )
                        if close_o:
                            logger.info(f"✅ AI 平仓 {symbol} {side}: {close_o.get('id', '?')} | {reason}")
                            self.tlogger.log_position_close(
                                symbol=symbol, direction=side, strategy="pullback",
                                entry_price=entry, exit_price=mark,
                                pnl=upl, pnl_pct=roi * 100,
                                close_reason="AI_EXIT", holding_hours=holding_hours,
                            )
                        else:
                            logger.warning(f"⚠️  AI 平仓 {symbol} 返回空 (可能已平仓)")
                    except Exception as e:
                        logger.error(f"❌ AI 平仓 {symbol} 失败: {e}")
                else:
                    logger.info(f"🤚 AI 持仓 {symbol} {side}: HOLD | {reason}")

            except Exception as e:
                logger.warning(f"🔍 AI 持仓审核 {symbol} 异常: {e}")
                continue

            # 币种间延迟
            time.sleep(0.5)

    # ==================================================================
    # 核心扫描逻辑
    # ==================================================================
    def scan_single_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        扫描单个币种:
          1. 拉取K线 → 2. 计算指标 → 3. 信号初筛
        返回候选信号字典，或 None（无信号 / 异常）
        """
        try:
            df = self.exchange.fetch_ohlcv(symbol)
            min_bars = max(self.config.rsi_period, self.config.atr_period) + 1
            if len(df) < min_bars:
                logger.warning(f"⚠️  {symbol} K线不足 ({len(df)}根 < {min_bars})，跳过")
                return None

            df = self.indicator.compute_all(df)

            latest = df.iloc[-1]
            close = float(latest["close"])
            ema   = float(latest["ema"])
            rsi   = float(latest["rsi"])
            atr   = float(latest["atr"])
            adx   = float(latest["adx"])

            if pd.isna(ema) or pd.isna(rsi) or pd.isna(atr) or pd.isna(adx):
                logger.debug(f"{symbol} 指标未就绪 (NaN)，跳过")
                return None

            # ── ① ADX 震荡过滤 (v3.6: 移除沙箱绕过，让过滤真正生效) ──
            session = SessionManager.get_session(
                self.config.adx_threshold, self.config.vol_ratio_threshold)
            # v3.6: 沙箱也不跳过ADX过滤，但阈值更宽松
            effective_adx = session["adx_threshold"]
            if self.config.is_sandbox:
                effective_adx = max(10, effective_adx - 6)  # 沙箱模式放宽但不全跳
            if adx < effective_adx:
                logger.debug(
                    f"{symbol} ADX={adx:.2f} < {effective_adx} "
                    f"({session['label']}阈值, 基础={self.config.adx_threshold})，跳过"
                )
                self.tlogger.log_adx_skip(symbol, adx)
                self.stats["adx_skips"] += 1
                return None

            # ── ② 成交量确认 (沙箱跳过——量数据不可靠；实盘严格过滤) ──
            if not self.config.is_sandbox:
                try:
                    cur_vol = float(df["volume"].iloc[-1])
                    avg_vol = float(df["volume"].tail(20).mean())
                    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
                except Exception:
                    vol_ratio = 1.0
                if vol_ratio < session["vol_ratio"]:
                    logger.debug(
                        f"{symbol} 量比={vol_ratio:.2f} < {session['vol_ratio']} "
                        f"({session['label']})，跳过"
                    )
                    self.tlogger.log_vol_skip(symbol, vol_ratio)
                    return None
            else:
                vol_ratio = 1.0  # 沙箱默认通过

            # ── v3.6: 趋势方向校验 (EMA50 vs EMA200) ──
            ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
            ema200 = float(df["close"].ewm(span=200, adjust=False).mean().iloc[-1])
            ema200_prev = float(df["close"].ewm(span=200, adjust=False).mean().iloc[-6])
            is_bearish_trend = ema200 < ema200_prev and ema50 < ema200  # EMA200下降 + 50在200下方
            is_bullish_trend = ema200 > ema200_prev and ema50 > ema200   # EMA200上升 + 50在200上方

            # ── v3.7: RSI 干旱自适应 —— 连续无信号时放宽阈值 ──
            drought_cycles = getattr(self, '_drought_cycles', 0)
            effective_oversold = self.config.rsi_oversold
            effective_overbought = self.config.rsi_overbought
            if drought_cycles >= 20:
                drought_relax = min(15, (drought_cycles - 20) // 10)
                effective_oversold = max(15, effective_oversold + drought_relax)
                effective_overbought = min(85, effective_overbought - drought_relax)
                if drought_cycles % 20 == 0:
                    logger.info(f"🌵 干旱自适应: RSI阈值放宽±{drought_relax} "
                                f"(超卖{effective_oversold}/超买{effective_overbought})")

            # v4.0: 大趋势偏向 — 熊市不产LONG信号(必被过滤), 牛市不产SHORT
            bearish_bias = is_bearish_trend
            bullish_bias = is_bullish_trend
            block_long = bearish_bias   # 熊市: 禁止做多信号
            block_short = bullish_bias  # 牛市: 禁止做空信号
            if bearish_bias:
                # 熊市: 做空门槛从 overbought 降到 overbought-10 (最低45)
                effective_overbought = max(45, effective_overbought - 10)
                # 做多需要更深超卖
                effective_oversold = max(10, effective_oversold - 5)
            elif bullish_bias:
                # 牛市: 做多门槛从 oversold 升到 oversold+10 (最高55)
                effective_oversold = min(55, effective_oversold + 10)
                effective_overbought = min(85, effective_overbought + 5)

            # ── ③ 信号初筛 ──
            direction = None
            strategy = "pullback"  # default

            # 回调策略：趋势中等待回调 (v3.7: 干旱时自适应放宽)
            if close > ema and rsi < effective_oversold:
                direction = "LONG"
                strategy = "pullback"
            elif close < ema and rsi > effective_overbought:
                direction = "SHORT"
                strategy = "pullback"

            # ── ④ 动量突破策略 (v2.4): 顺势追涨 + v3.2: 顺势追跌 ──
            if direction is None and self.config.momentum_enabled:
                # 做多动量：价格>EMA50 + ADX强 + RSI在动量区 + 突破近期高点
                if close > ema and rsi > self.config.momentum_rsi_min and rsi < self.config.momentum_rsi_max and adx > self.config.momentum_adx_threshold:
                    momentum_ema = float(df["close"].ewm(span=self.config.momentum_ema_period, adjust=False).mean().iloc[-1])
                    if close > momentum_ema:
                        recent_high = float(df["high"].tail(self.config.momentum_breakout_bars).max())
                        if close >= recent_high * 0.998:
                            direction = "LONG"
                            strategy = "momentum"
                # v3.2: 做空动量：价格<EMA50 + ADX强 + RSI在弱势动量区 + 跌破近期低点
                elif close < ema and rsi < (100 - self.config.momentum_rsi_min) and rsi > (100 - self.config.momentum_rsi_max) and adx > self.config.momentum_adx_threshold:
                    momentum_ema = float(df["close"].ewm(span=self.config.momentum_ema_period, adjust=False).mean().iloc[-1])
                    if close < momentum_ema:
                        recent_low = float(df["low"].tail(self.config.momentum_breakout_bars).min())
                        if close <= recent_low * 1.002:
                            direction = "SHORT"
                            strategy = "momentum"

            # ── v3.7: ⑤ EMA 交叉策略 —— 快慢线金叉死叉 ──
            if direction is None and self.config.momentum_enabled:
                ema_fast = float(df["close"].ewm(span=9, adjust=False).mean().iloc[-1])
                ema_slow = float(df["close"].ewm(span=21, adjust=False).mean().iloc[-1])
                ema_fast_prev = float(df["close"].ewm(span=9, adjust=False).mean().iloc[-2])
                ema_slow_prev = float(df["close"].ewm(span=21, adjust=False).mean().iloc[-2])
                # 金叉: 快线上穿慢线
                if ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow:
                    # 额外确认: 价格 > EMA200 (多头环境) 或 RSI 不超买
                    if close > ema200 or rsi < 60:
                        direction = "LONG"
                        strategy = "ema_cross"
                # 死叉: 快线下穿慢线
                elif ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow:
                    if close < ema200 or rsi > 40:
                        direction = "SHORT"
                        strategy = "ema_cross"

            # ── v3.7: ⑥ 布林带均值回归 —— 震荡市触碰轨道反弹 ──
            if direction is None:
                bb_std = float(df["close"].rolling(20).std().iloc[-1])
                bb_mid = float(df["close"].rolling(20).mean().iloc[-1])
                bb_upper = bb_mid + 2 * bb_std
                bb_lower = bb_mid - 2 * bb_std
                bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
                # 布林带收窄 (<2%) → 突破在即 → 跳过，不加反向信号
                if bb_width > 0.02:
                    # 价格跌破下轨 → 超卖反弹做多 (需 RSI 不极端)
                    if close <= bb_lower and rsi < 45 and rsi > 20:
                        direction = "LONG"
                        strategy = "bollinger"
                    # 价格突破上轨 → 超买回调做空 (需 RSI 不极端)
                    elif close >= bb_upper and rsi > 55 and rsi < 80:
                        direction = "SHORT"
                        strategy = "bollinger"

            # v4.0: 大趋势方向偏向 — 熊市阻止LONG, 牛市阻止SHORT (必被过滤, 不如不生)
            if direction == "LONG" and block_long:
                logger.debug(f"{symbol} LONG信号在熊市被阻止(必被多TF过滤)")
                return None
            if direction == "SHORT" and block_short:
                logger.debug(f"{symbol} SHORT信号在牛市被阻止(必被多TF过滤)")
                return None

            if direction is None:
                logger.debug(
                    f"{symbol} 无信号 | price={close:.4f} "
                    f"EMA={ema:.4f} RSI={rsi:.2f} ADX={adx:.2f} ATR={atr:.4f}"
                )
                return None

            # ── v3.7: 方向自动开关 —— 滚动胜率过低时暂停该方向 (v4.0: 死锁逃生门) ──
            if self.learner:
                force = getattr(self, '_force_allow_direction', False)
                viable, skip_reason = self.learner.is_direction_viable(
                    direction, force_allow=force, symbol=symbol)
                if not viable:
                    logger.info(f"🚫 {symbol} {direction} 方向已暂停: {skip_reason}")
                    self.tlogger.log_direction_skip(symbol, direction, skip_reason)
                    return None

            # ── v3.6: 趋势方向校验 —— 逆势信号降级或拦截 (v3.7: 使用自适应阈值) ──
            if direction == "LONG" and strategy == "pullback":
                if is_bearish_trend:
                    if rsi > effective_oversold * 0.7:
                        logger.debug(
                            f"⛔ {symbol} LONG-pullback 被拦截: 下跌趋势(EMA50<EMA200) "
                            f"且RSI={rsi:.1f}不够超卖(需<{effective_oversold:.0f})"
                        )
                        self.stats["adx_skips"] += 1
                        return None
                    else:
                        logger.info(f"⚠️  {symbol} LONG-pullback 逆势通过 (RSI={rsi:.1f}深度超卖)")
                        strategy = "counter_trend"
            elif direction == "SHORT" and strategy == "pullback":
                if is_bullish_trend:
                    if rsi < effective_overbought * 1.2:
                        logger.debug(
                            f"⛔ {symbol} SHORT-pullback 被拦截: 上涨趋势(EMA50>EMA200) "
                            f"且RSI={rsi:.1f}不够超买(需>{effective_overbought:.0f})"
                        )
                        self.stats["adx_skips"] += 1
                        return None
                    else:
                        logger.info(f"⚠️  {symbol} SHORT-pullback 逆势通过 (RSI={rsi:.1f}深度超买)")
                        strategy = "counter_trend"

            # ── v3.2: 高胜率加成 ──
            confidence = 50  # 基础分
            bonuses = []

            # 1. MACD 背离检测 (值 20 分)
            macd_hist = float(df["macd_hist"].iloc[-1])
            macd_prev = float(df["macd_hist"].iloc[-3])
            price_change = close - float(df["close"].iloc[-6])
            macd_change = macd_hist - macd_prev
            if direction == "LONG" and price_change < 0 and macd_change > 0:
                confidence += 20
                bonuses.append("MACD底背离")
            elif direction == "SHORT" and price_change > 0 and macd_change < 0:
                confidence += 20
                bonuses.append("MACD顶背离")

            # 2. 布林带极端位 (值 15 分)
            bb_lower = float(df["bb_lower"].iloc[-1])
            bb_upper = float(df["bb_upper"].iloc[-1])
            if direction == "LONG" and close <= bb_lower * 1.01:
                confidence += 15
                bonuses.append("布林下轨")
            elif direction == "SHORT" and close >= bb_upper * 0.99:
                confidence += 15
                bonuses.append("布林上轨")

            # 3. 量价确认 (值 10 分)
            prev_vol = float(df["volume"].iloc[-3])
            curr_vol = float(df["volume"].iloc[-1])
            vol_up = curr_vol > prev_vol * 1.2
            if direction == "LONG" and close > float(df["close"].iloc[-2]) and vol_up:
                confidence += 10
                bonuses.append("放量上涨")
            elif direction == "SHORT" and close < float(df["close"].iloc[-2]) and vol_up:
                confidence += 10
                bonuses.append("放量下跌")

            # 4. RSI 极端位 (值 10 分)
            if direction == "LONG" and rsi < 30:
                confidence += 10
                bonuses.append("RSI深度超卖")
            elif direction == "SHORT" and rsi > 70:
                confidence += 10
                bonuses.append("RSI深度超买")

            # 5. MACD 金叉/死叉 (值 5 分)
            macd_line = float(df["macd"].iloc[-1])
            macd_sig = float(df["macd_signal"].iloc[-1])
            if direction == "LONG" and macd_line > macd_sig and macd_hist > 0:
                confidence += 5
                bonuses.append("MACD多头")
            elif direction == "SHORT" and macd_line < macd_sig and macd_hist < 0:
                confidence += 5
                bonuses.append("MACD空头")

            # 6. ATR 扩张确认 (值 10 分) — ATR放大说明是真突破不是假晃
            atr_prev5 = float(df["atr"].iloc[-6:-1].mean())
            atr_now = float(df["atr"].iloc[-1])
            if atr_now > atr_prev5 * 1.15:
                confidence += 10
                bonuses.append("ATR扩张")

            # 7. K线实体比例 (值 10 分) — 阳线实体>影线=买方决心强
            open_p = float(df["open"].iloc[-1])
            high_p = float(df["high"].iloc[-1])
            low_p = float(df["low"].iloc[-1])
            body = abs(close - open_p)
            upper_shadow = high_p - max(close, open_p)
            lower_shadow = min(close, open_p) - low_p
            if direction == "LONG" and close > open_p and body > (upper_shadow + lower_shadow) * 1.5:
                confidence += 10
                bonuses.append("强阳线")
            elif direction == "SHORT" and close < open_p and body > (upper_shadow + lower_shadow) * 1.5:
                confidence += 10
                bonuses.append("强阴线")

            # 8. 连续K线确认 (值 10 分) — 前 2 根 K 线也同向=趋势已启动
            prev1_close = float(df["close"].iloc[-2])
            prev2_close = float(df["close"].iloc[-3])
            if direction == "LONG" and prev1_close > prev2_close and close > prev1_close:
                confidence += 10
                bonuses.append("3连阳")
            elif direction == "SHORT" and prev1_close < prev2_close and close < prev1_close:
                confidence += 10
                bonuses.append("3连阴")

            # 9. RSI 背离 (值 15 分) — 价格与RSI方向不一致=反转前兆
            rsi_now = float(df["rsi"].iloc[-1])
            rsi_3ago = float(df["rsi"].iloc[-4])
            price_3ago = float(df["close"].iloc[-4])
            if direction == "LONG" and close < price_3ago and rsi_now > rsi_3ago:
                confidence += 15
                bonuses.append("RSI底背离")
            elif direction == "SHORT" and close > price_3ago and rsi_now < rsi_3ago:
                confidence += 15
                bonuses.append("RSI顶背离")

            bonus_str = " | ".join(bonuses) if bonuses else "无加成"
            # cap at 100
            confidence = min(100, confidence)

            # ── v3.7: 统一加权评分 ──
            bonus_str = " | ".join(bonuses) if bonuses else "无加成"
            if self.scorer:
                # 收集各维度数据
                sentiment = getattr(self, '_sentiment_signal', {})
                flow_signal = None
                if self.flow_monitor:
                    try:
                        flow_signal = self.flow_monitor.get_flow_signal(
                            f"{symbol}/USDT:USDT", close)
                    except Exception:
                        pass
                markov_result = None
                if self.markov_regime:
                    try:
                        markov_result = self.markov_regime.detect(
                            df["close"].tail(100).tolist(), symbol)
                    except Exception:
                        pass

                # 构建简化的 regime_info (单TF近似，供评分器使用)
                # 后续 run_once 中的多TF分析会做更精确的判断
                regime_info = None
                try:
                    atr_pct = atr / close if close > 0 else 0
                    if adx > 25:
                        regime_label = "trending"
                        recommended = ["momentum", "pullback", "ema_cross"]
                    else:
                        regime_label = "ranging"
                        recommended = ["bollinger", "grid", "pullback"]
                    regime_info = {
                        "regime": regime_label,
                        "recommended": recommended,
                        "confidence": 55,
                        "volatility": "high" if atr_pct > 0.01 else "normal",
                        "trend_strength": "strong" if adx > 30 else ("weak" if adx < 20 else "moderate"),
                        "detail": f"单TF{regime_label} ADX={adx:.1f} ATR%={atr_pct*100:.2f}",
                    }
                except Exception:
                    pass

                score_result = self.scorer.score(
                    sig={"direction": direction, "strategy": strategy,
                         "confidence": confidence, "bonuses": bonuses},
                    regime_info=regime_info,
                    sentiment=sentiment,
                    flow_signal=flow_signal,
                    markov_result=markov_result,
                    learner=self.learner,
                )
                confidence = score_result["score"]
                tier = score_result["tier"]

                logger.info(
                    f"💡 {symbol} {direction} [{strategy}] "
                    f"→ {score_result['summary']}"
                )

                # 三层决策: <50 拒绝
                if tier == "REJECT":
                    logger.info(f"⛔ {symbol} 评分{confidence}<{self.scorer.AI_THRESHOLD} → 自动拒绝")
                    return None

                # v4.0: 完美信号强制AI审核 — bonus全满的"假突破"最危险
                bonus_count = len([b for b in bonuses if b != "无加成"])
                if tier == "AUTO" and bonus_count >= 7:
                    tier = "AI"
                    logger.info(
                        f"🔍 {symbol} 评分{confidence}但因{bonus_count}个加成→降级AI审核 "
                        f"(完美信号风险)"
                    )
            else:
                tier = "AI"  # 无评分器 → 回退 AI 审核

            logger.info(
                f"💡 {symbol} 触发 {direction} 信号 [{strategy}]! "
                f"置信度={confidence} ({bonus_str}) "
                f"price={close:.4f} EMA={ema:.4f} RSI={rsi:.2f} ADX={adx:.2f} "
                f"ATR={atr:.4f} VolRatio={vol_ratio:.2f}"
            )

            return {
                "symbol": symbol,
                "direction": direction,
                "strategy": strategy,
                "price": close,
                "ema": ema,
                "rsi": rsi,
                "atr": atr,
                "adx": adx,
                "recent_closes": df["close"].tail(10).tolist(),
                "vol_ratio": round(vol_ratio, 2),
                "confidence": confidence,
                "tier": tier,
                "bonuses": bonuses,
            }

        except Exception as e:
            logger.error(f"❌ 扫描 {symbol} 异常: {e}")
            return None

    def scan_all(self) -> List[Dict[str, Any]]:
        """扫描全部币种，返回候选信号列表。v2.0: 自适应限流"""
        # 先获取已有持仓
        positions = self.exchange.get_open_positions()
        held_symbols = set(positions.keys())

        # v2.0: 根据币种数量自适应延迟 (确保 300s 周期内完成)
        symbol_count = len(self.config.SYMBOLS)
        if symbol_count <= 15:
            delay = 0.2
        elif symbol_count <= 30:
            delay = 0.12
        elif symbol_count <= 50:
            delay = 0.08
        else:
            delay = 0.05  # 100+ 币种, 极速模式

        est_time = symbol_count * 2.5  # 每个约 2.5s (含 API)
        if est_time > 250:
            logger.warning(
                f"⚠️  监控{symbol_count}个币种, 预计扫描{est_time:.0f}s, "
                f"接近5分钟周期上限"
            )

        candidates: List[Dict[str, Any]] = []
        scan_start = time.time()
        for i, symbol in enumerate(self.config.SYMBOLS):
            if symbol in held_symbols:
                continue

            signal = self.scan_single_symbol(symbol)
            if signal:
                candidates.append(signal)

            # 自适应延迟
            if delay > 0:
                time.sleep(delay)

            # 每 10 个币种报告进度
            if (i + 1) % 10 == 0:
                elapsed = time.time() - scan_start
                logger.debug(f"📊 扫描进度: {i+1}/{symbol_count} ({elapsed:.0f}s)")

        scan_elapsed = time.time() - scan_start
        logger.info(
            f"🔍 扫描完成: {symbol_count}币种 → {len(candidates)}信号 "
            f"({scan_elapsed:.1f}s)"
        )
        self.total_scans += 1
        return candidates

    # ==================================================================
    # 主循环
    # ==================================================================
    def run_once(self):
        """执行一次完整的扫描→多TF→AI审核→风控→执行周期 (v2.7: 账户数据一轮只查一次)"""
        cycle = self.total_scans + 1
        trades_this_cycle = 0

        # ── v2.7: 清空本周期 TF 缓存 + 重置 DeepSeek 断路器 ──
        self._tf_cache.clear()
        self.analyst.circuit_breaker_reset()

        # ── v2.7: 一轮只查一次账户全景，后续传给 riskmon/status/log ──
        acct = self.exchange.get_account_summary()

        # ── v3.0: 紧急停止检查 ──
        if self.safety:
            if not hasattr(self, '_equity_history'):
                self._equity_history: List[Tuple[float, float]] = []
            now_ts = time.time()
            self._equity_history.append((now_ts, acct["equity"]))
            cutoff = now_ts - self.config.emergency_window
            self._equity_history = [(ts, eq) for ts, eq in self._equity_history if ts >= cutoff]
            triggered, reason = self.safety.check_emergency_stop(
                acct["equity"], self._equity_history
            )
            if triggered:
                logger.error(f"🚨 紧急停止已触发: {reason}")
                positions = self.exchange.get_open_positions()
                closed = self.safety.emergency_close_all(positions)
                self.tlogger.log_risk("EMERGENCY_STOP", f"已平仓 {closed} 个持仓: {reason}")
                self.riskmon.cooldown_until = time.time() + 86400  # 紧急停止: 24h 冷却
                self.riskmon.cooldown_reason = "紧急停止"
                return

        logger.info(f"\n{'─' * 50}")
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)

        # ── v2.3: 每轮刷新市场情绪 ──
        try:
            self.market_ctx.ensure_fresh()
        except Exception:
            pass

        # ── v2.7: 用预取数据刷新风控，避免重复 API 调用 ──
        self.riskmon._refresh_with_data(acct)
        risk_status = self.riskmon.get_status()
        logger.info(f"🔄 第 {cycle} 轮扫描 — {now_str('%H:%M:%S')} | {session['label']}")
        logger.info(f"📊 日内风控: PnL={risk_status['pnl_pct']:+.2f}% | "
                     f"权益={risk_status['current_equity']:.2f} | "
                     f"累计手续费={risk_status['cumulative_fees']:.4f} | "
                     f"{'🔒锁定' if risk_status['blocked'] else '🟢正常'}")
        logger.info(f"{'─' * 50}")

        # ── v3.2: 移动止损检查 ──
        if self.trailing_sl and acct.get("positions_detail"):
            for pos_d in acct["positions_detail"]:
                sym_full = f"{pos_d['symbol']}/USDT:USDT"
                side = pos_d["side"]
                mark = pos_d["mark_price"]
                entry = pos_d["entry_price"]
                # 获取当前 SL
                try:
                    stop_ords = self.exchange.fetch_open_orders(sym_full)
                    current_sl = None
                    for o in stop_ords:
                        if self.exchange._is_reduce_only(o) and o.get("type") == "market":
                            t = float(o.get("info", {}).get("triggerPrice", 0) or 0)
                            if (side == "LONG" and t < mark) or (side == "SHORT" and t > mark):
                                current_sl = t
                                break
                    if current_sl:
                        try:
                            df = self.indicator.compute_all(
                                self.exchange.fetch_ohlcv(sym_full)
                            )
                            atr_v = float(df["atr"].iloc[-1])
                            new_sl = self.trailing_sl.check_and_update(
                                sym_full, side, mark, current_sl,
                                atr_v if not pd.isna(atr_v) and atr_v > 0 else mark * 0.01,
                                entry
                            )
                            if new_sl:
                                # 找到现有 TP 价格，避免 SL 更新时摧毁 TP
                                existing_tp = 0
                                try:
                                    all_stop = self.exchange.exchange.fetch_open_orders(
                                        sym_full, params={"stop": True}
                                    ) or []
                                    for o in all_stop:
                                        if self.exchange._is_reduce_only(o):
                                            tp_val = float(o.get("info", {}).get("triggerPrice", 0) or 0)
                                            # TP: LONG 止盈价高于入场价, SHORT 止盈价低于入场价
                                            if side == "LONG" and tp_val > entry:
                                                existing_tp = tp_val
                                                break
                                            if side == "SHORT" and tp_val < entry:
                                                existing_tp = tp_val
                                                break
                                except Exception:
                                    pass
                                # 取消旧 SL/TP，重新挂载，保留现有 TP
                                self.exchange.set_position_sl_tp(
                                    sym_full, "buy" if side == "LONG" else "sell",
                                    new_sl, existing_tp
                                )
                        except Exception:
                            pass
                except Exception:
                    pass

        # ── v3.3: 盈利锁仓 —— 浮盈超过 2x ATR 后移动 TP 锁定利润 ──
        if acct.get("positions_detail"):
            for pos_d in acct["positions_detail"]:
                sym_full = f"{pos_d['symbol']}/USDT:USDT"
                upl = pos_d.get("unrealized_pnl", 0)
                entry = pos_d.get("entry_price", 0)
                side = pos_d.get("side", "LONG")
                margin = pos_d.get("margin", 0)
                if margin <= 0 or entry <= 0:
                    continue
                roi = upl / margin  # 保证金回报率
                if roi > 0.5:  # 盈利超过保证金的50%，锁仓
                    try:
                        orders = self.exchange.exchange.fetch_open_orders(sym_full, params={"stop": True}) or []
                        reduce_orders = [o for o in orders if self.exchange._is_reduce_only(o)]
                        if len(reduce_orders) >= 8:
                            logger.warning(f"🔒 {pos_d['symbol']} SL/TP堆积({len(reduce_orders)}个)，先清理后锁仓")
                            for o in reduce_orders:
                                self.exchange.cancel_order(str(o.get('id','')), sym_full)
                        # 重新设置锁仓 TP (移到当前盈利的50%位置)
                        mark = pos_d.get("mark_price", entry)
                        if side == "SHORT":
                            lock_tp = round(entry - (entry - mark) * 0.5, 4)
                            lock_sl = round(entry + (mark - entry) * 0.1, 4) if mark > entry else round(mark * 1.005, 4)
                        else:
                            lock_tp = round(entry + (mark - entry) * 0.5, 4)
                            lock_sl = round(entry - (entry - mark) * 0.1, 4) if mark < entry else round(mark * 0.995, 4)
                        if lock_tp > 0 and lock_sl > 0 and lock_tp != lock_sl:
                            logger.info(f"🔒 {pos_d['symbol']} 盈利锁仓: ROI={roi*100:.0f}% → TP={lock_tp} SL={lock_sl}")
                            close_side = "buy" if side == "SHORT" else "sell"
                            self.exchange.set_position_sl_tp(sym_full, close_side, lock_sl, lock_tp)
                    except Exception as e:
                        logger.debug(f"🔒 锁仓 {pos_d['symbol']} 失败: {e}")

        # ── v3.3: 持仓实时分析 (自学习 v2.0) ──
        if self.learner and acct.get("positions_detail"):
            try:
                pos_insight = self.learner.analyze_open_positions(
                    acct["positions_detail"], session["label"]
                )
                if pos_insight:
                    logger.info(f"🧠 持仓分析:\n{pos_insight}")
            except Exception:
                pass

        # ── v3.6: 浮亏自动止损 —— ROI < AUTO_SL_ROI_THRESHOLD 且持仓 > AUTO_SL_MIN_HOURS ──
        if self.config.auto_sl_enabled and acct.get("positions_detail"):
            for pos_d in acct["positions_detail"]:
                margin = pos_d.get("margin", 0)
                upl = pos_d.get("unrealized_pnl", 0)
                if margin <= 0:
                    continue
                roi = upl / margin  # 保证金回报率
                if roi >= self.config.auto_sl_roi_threshold:
                    continue  # 浮亏未达阈值，跳过

                # 检查持仓时长
                sym_full = f"{pos_d['symbol']}/USDT:USDT"
                open_ts = self._position_open_times.get(sym_full)
                if not open_ts:
                    continue  # 无开仓时间记录，跳过
                holding_hours = (time.time() - open_ts) / 3600.0
                if holding_hours < self.config.auto_sl_min_hours:
                    continue  # 持仓不足，让正常SL处理

                # 触发浮亏止损
                side = pos_d.get("side", "LONG")
                entry = pos_d.get("entry_price", 0)
                contracts = abs(int(pos_d.get("contracts", 0)))
                mark = pos_d.get("mark_price", 0)
                sym = pos_d["symbol"]
                logger.warning(
                    f"🚨 {sym} {side} 浮亏止损触发: ROI={roi*100:.0f}% "
                    f"持仓{holding_hours:.1f}h | 浮亏{upl:+.2f}"
                )
                try:
                    # v3.6 fix: 使用 close_position API 正确平仓
                    close_side = "buy" if side == "SHORT" else "sell"
                    pos_side = "short" if side == "SHORT" else "long"
                    close_o = self.exchange.create_market_order_close(
                        sym_full, contracts, close_side, pos_side
                    )
                    if close_o:
                        logger.info(f"✅ 浮亏止损平仓 {sym}: {close_o.get('id', '?')}")
                        # 计算 PnL%
                        position_value = entry * contracts
                        pnl_pct = (upl / (position_value / self.config.leverage)) * 100 if position_value > 0 else roi * 100
                        self.tlogger.log_position_close(
                            symbol=sym, direction=side, strategy="pullback",
                            entry_price=entry, exit_price=mark,
                            pnl=upl, pnl_pct=pnl_pct,
                            close_reason="AUTO_SL", holding_hours=round(holding_hours, 1),
                        )
                    else:
                        logger.warning(f"⚠️  浮亏止损平仓 {sym} 返回空 (可能已平仓)")
                except Exception as e:
                    logger.error(f"❌ 浮亏止损平仓 {sym} 失败: {e}")

        # ── v3.7: ① 僵尸仓退出 —— 持仓 >4h 且 |ROI| < 2% 自动平仓释放资金 ──
        if acct.get("positions_detail"):
            stale_hours = getattr(self.config, 'stale_exit_hours', 4.0)
            stale_roi_limit = getattr(self.config, 'stale_roi_limit', 0.02)
            for pos_d in acct["positions_detail"]:
                sym_full = f"{pos_d['symbol']}/USDT:USDT"
                open_ts = self._position_open_times.get(sym_full)
                if not open_ts:
                    continue
                holding_hours = (time.time() - open_ts) / 3600.0
                if holding_hours < stale_hours:
                    continue
                margin = pos_d.get("margin", 0)
                upl = pos_d.get("unrealized_pnl", 0)
                roi = upl / margin if margin > 0 else 0
                if abs(roi) > stale_roi_limit:
                    continue  # 有显著盈亏，让正常 SL/TP 处理
                # 僵尸仓：持仓久 + 几乎不赚不亏 → 平仓释放保证金
                side = pos_d.get("side", "LONG")
                contracts = abs(int(pos_d.get("contracts", 0)))
                sym = pos_d["symbol"]
                logger.warning(
                    f"🧟 {sym} {side} 僵尸仓退出: 持仓{holding_hours:.1f}h "
                    f"ROI={roi*100:.1f}% |浮盈{upl:+.2f}| < {stale_roi_limit*100:.0f}%"
                )
                try:
                    close_side = "buy" if side == "SHORT" else "sell"
                    pos_side = "short" if side == "SHORT" else "long"
                    close_o = self.exchange.create_market_order_close(
                        sym_full, contracts, close_side, pos_side)
                    if close_o:
                        logger.info(f"✅ 僵尸仓平仓 {sym}: {close_o.get('id', '?')}")
                        self.tlogger.log_position_close(
                            symbol=sym, direction=side, strategy="pullback",
                            entry_price=pos_d.get("entry_price", 0),
                            exit_price=pos_d.get("mark_price", 0),
                            pnl=upl, pnl_pct=roi * 100,
                            close_reason="STALE_EXIT",
                            holding_hours=round(holding_hours, 1),
                        )
                except Exception as e:
                    logger.warning(f"🧟 僵尸仓平仓 {sym} 失败: {e}")

        # ── v3.7: ② 波动率暴增熔断 —— ATR 突然翻倍时收紧全仓止损 ──
        if acct.get("positions_detail"):
            if not hasattr(self, '_atr_history'):
                self._atr_history: Dict[str, list] = {}
            vol_spike_threshold = 2.5  # ATR 超过均值的倍数触发熔断
            for pos_d in acct["positions_detail"]:
                sym = pos_d["symbol"]
                sym_full = f"{sym}/USDT:USDT"
                try:
                    # 获取当前 ATR
                    df = self.exchange.fetch_ohlcv_tf(sym_full, timeframe="15m", limit=30)
                    atr_val = float(df["close"].diff().abs().rolling(14).mean().iloc[-1])
                    float(df["close"].iloc[-1])
                    if pd.isna(atr_val) or atr_val <= 0:
                        continue
                    # 追踪 ATR 历史
                    if sym not in self._atr_history:
                        self._atr_history[sym] = []
                    self._atr_history[sym].append(atr_val)
                    if len(self._atr_history[sym]) > 20:
                        self._atr_history[sym] = self._atr_history[sym][-20:]
                    hist = self._atr_history[sym]
                    if len(hist) < 10:
                        continue
                    avg_atr = sum(hist[:-1]) / (len(hist) - 1)
                    if avg_atr <= 0:
                        continue
                    spike_ratio = atr_val / avg_atr
                    if spike_ratio > vol_spike_threshold:
                        # 触发熔断：收紧止损到 1.0x ATR
                        side = pos_d.get("side", "LONG")
                        entry = pos_d.get("entry_price", 0)
                        tight_sl = (entry - atr_val * 1.0 if side == "LONG"
                                    else entry + atr_val * 1.0)
                        logger.warning(
                            f"⚡ {sym} 波动率暴增: ATR={atr_val:.4f} "
                            f"({spike_ratio:.1f}x均值{avg_atr:.4f}) → 收紧SL至{tight_sl:.4f}"
                        )
                        # 使用 place-pos-tpsl 更新止损
                        try:
                            tp_price = (entry + atr_val * 2.0 if side == "LONG"
                                        else entry - atr_val * 2.0)
                            self.exchange.set_position_sl_tp(
                                sym_full, "buy" if side == "LONG" else "sell",
                                tight_sl, tp_price)
                            self.tlogger.log_risk(
                                "VOL_SPIKE", f"{sym} ATR {spike_ratio:.1f}x → SL收紧")
                        except Exception as e:
                            logger.warning(f"⚡ {sym} 波动熔断更新SL失败: {e}")
                except Exception:
                    pass  # OI/ATR 获取失败不影响主循环

        # ── v3.6: AI 持仓审核 —— 定期审查已持仓是否需要提前退出 ──
        if self.config.ai_position_review_enabled and acct.get("positions_detail"):
            if not self.analyst.circuit_breaker_open():
                # 按 interval 间隔执行 (默认每3轮=15分钟)
                if self.total_scans > 0 and self.total_scans % self.config.ai_position_review_interval == 0:
                    self._review_open_positions(acct["positions_detail"])
            else:
                logger.debug("🔌 断路器熔断，跳过 AI 持仓审核")

        # ── v3.5: 清理无主订单（已平仓但计划单残留的币种）──
        current_symbols = {f"{p['symbol']}/USDT:USDT" for p in acct.get("positions_detail", [])}
        for sym in self.config.SYMBOLS:
            sym_full = f"{sym}/USDT:USDT"
            if sym_full in current_symbols:
                continue  # 有仓位，正常处理
            # 无仓位但有计划单 → 平仓残留，全部取消
            try:
                orders = self.exchange.exchange.fetch_open_orders(sym_full, params={"stop": True}) or []
            except Exception:
                continue
            reduce_orders = [o for o in orders if self.exchange._is_reduce_only(o)]
            if reduce_orders:
                logger.warning(f"🧹 {sym} 已无仓位但有{len(reduce_orders)}个残留计划单，全部取消")
                for o in reduce_orders:
                    self.exchange.cancel_order(str(o.get('id','')), sym_full)

        # ── v3.4: 每轮自动清理超额订单 (在仓币种) ──
        try:
            for p in acct.get("positions_detail", []):
                sym_full = f"{p['symbol']}/USDT:USDT"
                try:
                    orders = self.exchange.exchange.fetch_open_orders(sym_full, params={"stop": True}) or []
                except Exception:
                    continue
                reduce_orders = [o for o in orders if self.exchange._is_reduce_only(o)]
                if len(reduce_orders) > 4:
                    logger.warning(f"🧹 {p['symbol']} 超额订单({len(reduce_orders)}个)，自动清理...")
                    reduce_orders.sort(key=lambda o: str(o.get('id', '')))
                    for o in reduce_orders[:-2]:
                        self.exchange.cancel_order(str(o.get('id','')), sym_full)
        except Exception:
            pass

        # ── v3.6: 每3轮检查仓位 TPSL 保护 (从 position info 读取，兼容 place-pos-tpsl) ──
        if self.total_scans > 0 and self.total_scans % 3 == 0:
            try:
                bare = 0
                for p in acct.get("positions_detail", []):
                    sym_full = f"{p['symbol']}/USDT:USDT"
                    entry = p.get("entry_price", 0)
                    side = p.get("side", "LONG")
                    mark = p.get("mark_price", 0)

                    # v4.0 fix: 用新的 _has_position_tpsl (查计划单 + pos-tpsl API)
                    has_sl, has_tp = self.exchange._has_position_tpsl(sym_full, entry, side)

                    if not has_sl or not has_tp:
                        if not has_sl and not has_tp:
                            bare += 1
                        close_side = "buy" if side == "SHORT" else "sell"
                        atr = max(entry * 0.01, abs(mark - entry) * 0.3)
                        sl_p = entry + 1.5 * atr if side == "SHORT" else entry - 1.5 * atr
                        if side == "SHORT" and sl_p <= mark:
                            sl_p = mark * 1.005
                        elif side == "LONG" and sl_p >= mark:
                            sl_p = mark * 0.995
                        tp_p = entry - 2.0 * atr if side == "SHORT" else entry + 2.0 * atr
                        missing = []
                        if not has_sl:
                            missing.append("SL")
                        if not has_tp:
                            missing.append("TP")
                        logger.warning(f"🛡️  {p['symbol']} 缺少{'/'.join(missing)}，补挂 SL={sl_p:.4f} TP={tp_p:.4f}")
                        self.exchange.set_position_sl_tp(sym_full, close_side, round(sl_p,4), round(tp_p,4))
                if bare == 0 and acct.get("positions_detail"):
                    logger.debug("🛡️  SL健康检查: 全部持仓已保护")
            except Exception as e:
                logger.warning(f"⚠️  SL健康检查异常: {e}")

        # 如果日内亏损已触发锁定，本轮只扫描不交易
        if not self.riskmon.can_trade_with_data(acct):
            logger.warning("⛔ 日内风控已锁定，本轮仅扫描不执行交易")
            candidates = self.scan_all()
            logger.info(f"📊 本轮候选信号: {len(candidates)} 个（锁定模式，不执行）")
            for sig in candidates:
                self.tlogger.log_signal(sig)
            all_time = self.riskmon.current_equity - self.riskmon.initial_equity
            self.tlogger.log_cycle(cycle, len(candidates), 0,
                                    self.riskmon.current_equity,
                                    self.riskmon.daily_pnl_pct,
                                    self.riskmon.cumulative_fees,
                                    "locked", 0, 0,
                                    self.riskmon.initial_equity, all_time)
            return

        # ── v4.0: 方向死锁检测 —— 长时间无信号+0持仓 → 强制放行一轮 ──
        positions_count = len(acct.get("positions_detail", []))
        drought = getattr(self, '_drought_cycles', 0)
        if not hasattr(self, '_force_allow_direction'):
            self._force_allow_direction = False
        # v4.0 fix: 逃生门冷却 — 放行一次后需等干旱重新累积 6 轮才能再放行
        if not hasattr(self, '_escape_hatch_cooldown'):
            self._escape_hatch_cooldown = 0
        if self._escape_hatch_cooldown > 0:
            self._escape_hatch_cooldown -= 1

        # 干旱 ≥ 6 轮 (30min) + 无持仓 + 冷却期已过 → 放行一轮
        if (drought >= 6 and positions_count == 0 and self.learner
                and self._escape_hatch_cooldown == 0):
            self._force_allow_direction = True
            self._escape_hatch_cooldown = 12  # 放行后冷却 12 轮 (1小时)
            logger.warning(f"⚠️  干旱{drought}轮+0持仓 → 本周期强制放行方向开关 (冷却12轮)")
        else:
            self._force_allow_direction = False

        candidates = self.scan_all()
        logger.info(f"📊 本轮候选信号: {len(candidates)} 个")

        # ── v3.7: 干旱计数器 —— 追踪连续无成交周期 ──
        # v4.0 fix: trades_this_cycle 在本位置永远是 0 (交易在后面执行),
        # 所以改用 candidates 判断 - 有信号说明干旱结束
        if not hasattr(self, '_drought_cycles'):
            self._drought_cycles = 0
        if len(candidates) == 0:
            self._drought_cycles += 1
        else:
            self._drought_cycles = 0  # 有候选信号 → 干旱结束

        # ── v3.0: 网格订单轮询 ──
        if self.grid_manager:
            grid_status = self.grid_manager.poll_grids()
            if grid_status.get("filled_orders", 0) > 0:
                logger.info(f"📋 网格成交: {grid_status['filled_orders']} 笔")

        # ── v2: 并发持仓限制 + v3.7 时段动态上限 ──
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)
        effective_max_pos = min(self.config.max_concurrent_positions,
                                session.get("max_positions", self.config.max_concurrent_positions))
        num_existing = self.exchange.count_open_positions()
        max_new = max(0, effective_max_pos - num_existing)
        if max_new <= 0 and candidates:
            # ── v3.3: 仓位轮动 —— 满仓时依次尝试换掉亏损仓 ──
            rotated = 0
            pos_list = sorted(acct.get("positions_detail", []),
                             key=lambda x: x.get("unrealized_pnl", 0))  # 亏损最多的排前面
            for sig in candidates[:3]:
                if rotated >= 1:
                    break
                new_conf = sig.get("confidence", 50)
                if new_conf < 55:
                    continue
                for worst in pos_list:
                    worst_upl = worst.get("unrealized_pnl", 0)
                    worst_symbol = worst.get("symbol", "?")
                    if worst_upl >= 0:  # 只换亏损仓位
                        continue
                    logger.info(
                        f"🔄 仓位轮动: 平掉 {worst_symbol} "
                        f"(浮亏{worst_upl:+.2f}) → 开 {sig['symbol']} "
                        f"(置信度={new_conf})"
                    )
                    try:
                        sym_full = f"{worst_symbol}/USDT:USDT"
                        w_side = "buy" if worst.get("side") == "SHORT" else "sell"
                        w_cts = int(worst.get("contracts", 0))
                        w_hold = "short" if worst.get("side") == "SHORT" else "long"
                        if w_cts > 0:
                            # v3.6 fix: 用 close_position API (遵守 Bitget v2 文档)
                            close_o = self.exchange.create_market_order_close(
                                sym_full, w_cts, w_side, w_hold
                            )
                            logger.info(f"🔄 平仓 {worst_symbol}: {close_o.get('id', '?') if close_o else 'CANCELLED'}")
                            rotated += 1
                            self.tlogger.log_risk("ROTATION_CLOSE",
                                f"平{worst_symbol}浮亏{worst_upl:+.2f}→开{sig['symbol']}")
                            num_existing = self.exchange.count_open_positions()
                            max_new = max(0, self.config.max_concurrent_positions - num_existing)
                            break  # 成功换仓，跳出最弱仓位循环
                    except Exception as e:
                        logger.warning(f"🔄 轮动平仓 {worst_symbol} 失败: {e}")
                        continue  # 尝试下一个亏损仓位

        if max_new <= 0:
            logger.warning(
                f"⛔ 已达最大并发持仓 ({num_existing}/{self.config.max_concurrent_positions})，"
                f"跳过本轮交易"
            )
            self.tlogger.log_concurrent_skip(num_existing, self.config.max_concurrent_positions)
            for sig in candidates:
                self.tlogger.log_signal(sig)
            all_time = self.riskmon.current_equity - self.riskmon.initial_equity
            self.tlogger.log_cycle(cycle, len(candidates), 0,
                                    self.riskmon.current_equity,
                                    self.riskmon.daily_pnl_pct,
                                    self.riskmon.cumulative_fees,
                                    "concurrent_full", 0, 0,
                                    self.riskmon.initial_equity, all_time)
            # v3.5: 持仓快照
            if acct.get("positions_detail"):
                self.tlogger.log_cycle_positions(cycle, [
                    {"symbol": p["symbol"], "side": p["side"],
                     "entry": p["entry_price"], "mark": p["mark_price"],
                     "upl": p["unrealized_pnl"], "margin": p.get("margin", 0)}
                    for p in acct["positions_detail"]
                ])
            return

        if len(candidates) > max_new:
            logger.info(f"⏳ 候选信号 {len(candidates)} > 可开仓 {max_new}，取前 {max_new} 个")
            candidates = candidates[:max_new]

        # ── v2.7: BTC 联动过滤 (有候选信号时才查，空信号跳过省API) ──
        btc_change = None
        if candidates:
            btc_change = self.exchange.fetch_btc_change(self.config.btc_filter_timeframe)
        if btc_change is not None:
            logger.info(f"📊 BTC {self.config.btc_filter_timeframe} 涨跌幅: {btc_change*100:+.2f}%")

            filtered_candidates = []
            for sig in candidates:
                symbol = sig["symbol"]
                direction = sig["direction"]

                if "BTC" in symbol:
                    filtered_candidates.append(sig)
                    continue

                if (direction == "LONG" and
                        btc_change < self.config.btc_drop_block_long):
                    logger.warning(
                        f"⏭️  {symbol} {direction} 因 BTC 暴跌 {btc_change*100:+.1f}% 被拦截"
                    )
                    self.tlogger.log_btc_filter(
                        symbol, direction, btc_change, "btc_drop_block_long"
                    )
                    self.stats["btc_filters"] += 1
                    continue
                if (direction == "SHORT" and
                        btc_change > self.config.btc_pump_block_short):
                    logger.warning(
                        f"⏭️  {symbol} {direction} 因 BTC 暴涨 {btc_change*100:+.1f}% 被拦截"
                    )
                    self.tlogger.log_btc_filter(
                        symbol, direction, btc_change, "btc_pump_block_short"
                    )
                    self.stats["btc_filters"] += 1
                    continue

                filtered_candidates.append(sig)

            if len(filtered_candidates) < len(candidates):
                logger.info(
                    f"📊 BTC 过滤: {len(candidates)} → {len(filtered_candidates)} 个候选"
                )
            candidates = filtered_candidates
        else:
            btc_change = None  # 无法获取BTC数据时跳过滤镜

        # ── v3.0: 新闻上下文刷新 + v3.7: 情绪量化信号 ──
        self._sentiment_signal = {}
        if self.news_client and candidates:
            currencies = [s["symbol"].split("/")[0] for s in candidates]
            self.analyst.news_context = self.news_client.get_news_context(currencies)
            # v3.7: 获取量化情绪信号
            try:
                self._sentiment_signal = self.news_client.get_sentiment_signal(currencies)
                logger.info(
                    f"📊 情绪量化: score={self._sentiment_signal.get('score','?')} "
                    f"{self._sentiment_signal.get('signal','?')} "
                    f"→ {self._sentiment_signal.get('market_action','?')}"
                )
            except Exception as e:
                logger.debug(f"📊 情绪量化失败: {e}")
            # v3.7: 订单流摘要注入 AI
            if self.flow_monitor:
                try:
                    flow_summary = self.flow_monitor.get_summary(currencies)
                    if flow_summary:
                        self.analyst.news_context = (
                            (self.analyst.news_context or "") + "\n" + flow_summary)
                except Exception:
                    pass
        else:
            self.analyst.news_context = ""

        # ── v3.1: 交易智慧注入 ──
        if self.learner:
            self.analyst.wisdom_context = self.learner.get_full_context()  # v2.0: 智慧+策略绩效

        # ── v3.0: 投资组合排名 ──
        if self.portfolio and self.config.strength_ranking_enabled and len(candidates) > 1:
            try:
                self.portfolio.ensure_correlations_fresh(self.config.SYMBOLS)
                tf_ctxs = {}
                for sig in candidates:
                    tf_ctxs[sig["symbol"]] = self._tf_cache.get(sig["symbol"], {})
                candidates = self.portfolio.rank_candidates(candidates, tf_ctxs)
                logger.info(f"📊 投资组合排名完成 ({len(candidates)} 个候选)")

                # v2.0: 用组合排名重新评分
                total = len(candidates)
                if self.scorer:
                    for sig in candidates:
                        rank = sig.get("_rank", 1)
                        result = self.scorer.score(
                            sig={"direction": sig["direction"],
                                 "strategy": sig.get("strategy", "pullback"),
                                 "confidence": sig.get("confidence", 50),
                                 "bonuses": sig.get("bonuses", [])},
                            regime_info=None,  # 评分侧重组合维度
                            portfolio_rank=rank, portfolio_total=total,
                            learner=self.learner,
                        )
                        sig["_portfolio_score"] = result["score"]
                        # 组合排名信号注入 confidence
                        result["breakdown"].get("portfolio", {}).get("score", 50)
                        if rank <= 2:
                            sig["confidence"] = min(100, sig.get("confidence", 50) + 5)
                        elif rank > total * 0.7:
                            sig["confidence"] = max(30, sig.get("confidence", 50) - 10)

                # v2.0: 组合摘要 (供 AI context)
                sym_bases = [s["symbol"] for s in candidates[:5]]
                port_summary = self.portfolio.get_portfolio_summary(sym_bases)
                logger.info(f"📊 {port_summary}")
            except Exception as e:
                logger.warning(f"⚠️  投资组合排名失败: {e}")

        # ── v2.1: 资金费率获取 ──
        funding_rates: Dict[str, Optional[float]] = {}
        for sig in candidates:
            symbol = sig["symbol"]
            funding_rates[symbol] = self.exchange.fetch_funding_rate(symbol)

        for sig in candidates:
            # ── 记录信号 ──
            self.tlogger.log_signal(sig)
            # v3.5: 完整信号上下文（供自学习复盘）
            self.tlogger.log_signal_full(
                symbol=sig["symbol"], direction=sig["direction"],
                strategy=sig.get("strategy", "pullback"),
                price=sig["price"], ema=sig["ema"],
                rsi=sig["rsi"], atr=sig["atr"], adx=sig["adx"],
                confidence=sig.get("confidence", 50),
                bonuses=sig.get("bonuses", []),
                market_regime=self._detect_market_regime(
                    self._tf_cache.get(sig["symbol"], {})
                ).get("regime", "unknown"),
            )

            symbol    = sig["symbol"]
            direction = sig["direction"]
            price     = sig["price"]
            ema       = sig["ema"]
            rsi       = sig["rsi"]
            atr       = sig["atr"]
            adx       = sig["adx"]
            closes    = sig["recent_closes"]
            strategy  = sig.get("strategy", "pullback")

            # ── v2: 多时间周期分析 + v3.7 增强市场状态识别 ──
            tf_context = self._analyze_tf_context(symbol)
            vol_ratio_val = sig.get("vol_ratio", 1.0)
            regime_info = self._detect_market_regime(
                tf_context, atr=atr, close=price, adx=adx, vol_ratio=vol_ratio_val)
            market_regime = regime_info.get("regime", "unknown")
            # v4.0: 记录当前市场状态，供平仓归因使用
            self._current_regime = market_regime
            trend_aligned, alignment_reason = self._check_trend_alignment(direction, tf_context)

            logger.info(
                f"📊 多TF分析 [{symbol}]: {alignment_reason} | "
                f"市场: {market_regime} ({regime_info.get('detail','')}) "
                f"波动:{regime_info.get('volatility','?')} "
                f"趋势:{regime_info.get('trend_strength','?')} "
                f"推荐策略:{regime_info.get('recommended',[])}"
            )

            # ── v3.7: 策略路由 —— 根据市场状态过滤不匹配策略 ──
            # v4.0: bollinger/counter_trend 是均值回归/反转策略, 不受趋势约束
            recommended = regime_info.get("recommended", [])
            if recommended and strategy not in recommended:
                if strategy in ("bollinger", "counter_trend"):
                    logger.info(
                        f"🔄 {symbol} {strategy} 策略不受市场状态限制 "
                        f"(市场: {market_regime})，放行"
                    )
                else:
                    logger.info(
                        f"🔄 {symbol} 策略路由: {strategy} 不在推荐列表 "
                        f"{recommended} (市场: {market_regime})，跳过"
                    )
                    continue

            # v4.0: 多TF趋势不一致 → 过滤 (但均值回归策略豁免)
            if not trend_aligned and strategy not in ("bollinger", "counter_trend"):
                opposing_tfs = [
                    tf for tf in self.config.higher_timeframes
                    if tf_context.get(tf, {}).get("trend", "unknown") not in ("unknown", "")
                    and tf_context.get(tf, {}).get("regime", "unknown") != "ranging"
                ]
                if len(opposing_tfs) >= 1:
                    logger.warning(
                        f"⛔ {symbol} {direction} 信号与大趋势不一致，过滤 "
                        f"({alignment_reason})"
                    )
                    self.tlogger.log_tf_skip(symbol, direction, alignment_reason, tf_context)
                    self.stats["tf_skips"] += 1
                    continue

            # ── v2.1: 资金费率 ──
            funding_rate = funding_rates.get(symbol)

            # ── v2.4: 策略类型 ──
            strategy = sig.get("strategy", "pullback")

            # ── v3.0: 周末自动网格 (BTC/ETH/SOL 低波动) ──
            if self.grid_manager and self.grid_manager.should_deploy_weekend(symbol):
                try:
                    df = self.exchange.fetch_ohlcv(symbol)
                    levels_info = self.grid_manager.calculate_levels(symbol, df, price, atr)
                    if levels_info and levels_info.get("levels"):
                        ok = self.grid_manager.deploy_grid(symbol, levels_info, price)
                        if ok:
                            logger.info(f"📋 周末网格已部署 {symbol}")
                        continue
                except Exception as e:
                    logger.warning(f"⚠️  周末网格部署失败 {symbol}: {e}")

            # ── v3.0: 震荡市网格策略 ──
            if market_regime == "ranging" and self.grid_manager:
                if not self.grid_manager.is_active(symbol):
                    try:
                        df = self.exchange.fetch_ohlcv(symbol)
                        levels_info = self.grid_manager.calculate_levels(symbol, df, price, atr)
                        if levels_info and levels_info.get("levels"):
                            ok = self.grid_manager.deploy_grid(symbol, levels_info, price)
                            if ok:
                                logger.info(f"📋 震荡网格已部署 {symbol}")
                            continue  # 跳过常规信号流程
                    except Exception as e:
                        logger.warning(f"⚠️  网格部署失败 {symbol}: {e}")
                else:
                    logger.info(f"⏭️  {symbol} 网格已活跃，跳过常规信号")
                    continue

            # ── v4.0: 组合 VaR 分层检查 (5% 警告 / 8% 拒绝 / 12% 紧急) ──
            if self.portfolio and acct.get("positions_detail"):
                var_result = self.portfolio.check_portfolio_var(
                    acct["positions_detail"], acct.get("equity", 10000))
                var_pct = var_result.get("var_pct", 0)
                if var_pct >= 12:
                    logger.error(
                        f"🚨 {symbol} 组合VaR={var_pct}% > 12% 紧急阈值, 拒绝开仓并建议减仓"
                    )
                    self.tlogger.log_risk("VAR_EMERGENCY",
                        f"{symbol}: VaR={var_pct}% > 12%, 跳过所有新开仓")
                    continue
                elif var_pct >= 8:
                    logger.warning(
                        f"⛔ {symbol} 组合VaR={var_pct}% > 8% 拒绝阈值, 跳过"
                    )
                    self.tlogger.log_risk("VAR_REJECT",
                        f"{symbol}: VaR={var_pct}% > 8%")
                    continue
                elif var_pct >= 5:
                    logger.warning(
                        f"⚠️  {symbol} 组合VaR={var_pct}% > 5%, 降低仓位"
                    )
                    sig["confidence"] = max(30, sig.get("confidence", 50) - 15)

            # ── v3.0: 投资组合限制检查 ──
            if self.portfolio and acct.get("positions_detail"):
                existing_syms = [p["symbol"] for p in acct["positions_detail"]]
                ok, reason = self.portfolio.check_correlation_limit(symbol, existing_syms)
                if not ok:
                    logger.warning(f"⏭️  {symbol} 因相关性限制跳过: {reason}")
                    self.tlogger.log_risk("CORRELATION_LIMIT", f"{symbol} {direction}: {reason}")
                    continue
                ok, reason = self.portfolio.check_sector_exposure(symbol, existing_syms)
                if not ok:
                    logger.warning(f"⏭️  {symbol} 因板块暴露跳过: {reason}")
                    self.tlogger.log_risk("SECTOR_LIMIT", f"{symbol} {direction}: {reason}")
                    continue

            # ── v4.0: 资金费率硬阻止 ──
            funding_rate = funding_rates.get(symbol)
            if funding_rate is not None:
                if direction == "LONG" and funding_rate > self.config.funding_warn_long * 2:
                    logger.warning(
                        f"⛔ {symbol} LONG 资金费率{funding_rate*100:.3f}% > "
                        f"{self.config.funding_warn_long*200:.1f}%，拒绝开仓"
                    )
                    self.tlogger.log_risk("FUNDING_BLOCK",
                        f"{symbol} LONG: 费率{funding_rate*100:.3f}%")
                    continue
                if direction == "SHORT" and funding_rate < self.config.funding_warn_short * 2:
                    logger.warning(
                        f"⛔ {symbol} SHORT 资金费率{funding_rate*100:.3f}% < "
                        f"{self.config.funding_warn_short*200:.1f}%，拒绝开仓"
                    )
                    self.tlogger.log_risk("FUNDING_BLOCK",
                        f"{symbol} SHORT: 费率{funding_rate*100:.3f}%")
                    continue

            # ── v4.0: OI 极端背离硬阻止 ──
            if self.flow_monitor:
                blocked, reason = self.flow_monitor.should_block_trade(
                    f"{symbol}/USDT:USDT", direction, price)
                if blocked:
                    logger.warning(f"⛔ {symbol} OI极端背离: {reason}")
                    self.tlogger.log_risk("OI_BLOCK", f"{symbol} {direction}: {reason}")
                    continue

            # ── v4.0: 方向平衡 + 开仓间隔 ──
            pos_list = acct.get("positions_detail", [])
            if pos_list:
                longs = sum(1 for p in pos_list if p.get("side") == "LONG")
                len(pos_list) - longs
                long_pct = longs / len(pos_list) * 100 if pos_list else 0

                # 1. 方向极致集中 (100%) → 硬阻止同向新仓
                same_dir_pct = long_pct if direction == "LONG" else (100 - long_pct)
                if same_dir_pct >= 100 and len(pos_list) >= 3:
                    logger.warning(
                        f"⛔ {symbol} {direction} 方向100%集中({len(pos_list)}个持仓)，拒绝新开仓"
                    )
                    self.tlogger.log_risk("DIRECTION_LOCK",
                        f"{symbol} {direction}: 持仓{len(pos_list)}个全{same_dir_pct:.0f}%同向")
                    continue

                # 2. 方向高度集中 (>75%) → 提高开仓门槛 (至少3持仓才有意义)
                if len(pos_list) >= 3 and (
                    (direction == "LONG" and long_pct > 75) or
                    (direction == "SHORT" and (100 - long_pct) > 75)
                ):
                    sig["confidence"] = max(30, sig.get("confidence", 50) - 25)
                    logger.info(
                        f"⚠️  {symbol} {direction} 方向占比{max(long_pct,100-long_pct):.0f}%过高，"
                        f"置信度降至{sig['confidence']}"
                    )
                    # 如果降低后 < AI 阈值，且没有 AUTO 豁免 → 跳过
                    if sig["confidence"] < 45:
                        logger.info(f"⛔ {symbol} 方向集中+置信度过低 → 跳过")
                        continue

            # 3. 最小开仓间隔 (同一周期最多开 1 仓，60s 冷却)
            last_trade_ts = getattr(self, '_last_trade_ts', 0)
            if trades_this_cycle >= 1 or (time.time() - last_trade_ts < 60):
                if len(candidates) > 1:
                    logger.info(
                        f"⏱️  {symbol} 开仓间隔限制 "
                        f"(本周期已开{trades_this_cycle}仓, 距上次{time.time()-last_trade_ts:.0f}s)"
                    )
                    continue

            # ── v2.7: DeepSeek 断路器检查 ──
            tier = sig.get("tier", "AI")
            if self.analyst.circuit_breaker_open():
                logger.warning(f"🔌 DeepSeek 断路器已熔断，{symbol} 跳过 AI 审核→REJECT")
                decision, reason = "REJECT", "断路器熔断-跳过AI"
            elif tier == "AUTO":
                # v3.7: 高置信度信号跳过 AI，直接执行
                decision, reason = "CONFIRM", f"评分{sig['confidence']}>70-自动通过"
                logger.info(f"⚡ {symbol} AUTO决策: {reason}")
            else:
                # ── v2: AI 审核（含多TF + 市场情绪 + 资金费率上下文） ──
                decision, reason = self.analyst.review_signal(
                symbol=symbol,
                direction=direction,
                price=price,
                ema=ema,
                rsi=rsi,
                atr=atr,
                recent_closes=closes,
                adx=adx,
                market_regime=market_regime,
                tf_context=tf_context,
                funding_rate=funding_rate,
                vol_ratio=vol_ratio_val,
                strategy=strategy,
                confidence=sig.get("confidence", 50),
                bonuses=sig.get("bonuses", []),
            )

            # ── 记录 AI 决策 ──
            self.tlogger.log_ai_decision(symbol, direction, decision, reason)
            self.stats["signals"] += 1
            if strategy == "momentum":
                self.stats["momentum_signals"] += 1
            if direction == "SHORT":
                self.stats["short_signals"] += 1

            if decision == "CONFIRM":
                self.stats["ai_confirms"] += 1
                if strategy == "momentum":
                    self.stats["momentum_trades"] += 1
                if direction == "SHORT":
                    self.stats["short_trades"] += 1
                # ── 日内风控二次检查（AI 调用期间可能触发） ──
                if not self.riskmon.can_trade():
                    logger.warning(f"⛔ {symbol} AI 已确认，但日内风控已触发，放弃下单")
                    self.tlogger.log_risk("DAILY_LOSS_BLOCK",
                                          f"{symbol} {direction} AI=CONFIRM 但因日内亏损被拦截")
                    continue

                logger.info(f"✅ {symbol} {direction} → DeepSeek CONFIRM，执行下单")
                # v2.0: 组合排名仓位系数
                rank = sig.get("_rank", 1)
                total_cands = len(candidates) if candidates else 1
                pos_mult = 1.0
                if self.portfolio:
                    pos_mult = self.portfolio.get_position_multiplier(rank, total_cands)
                # v4.0: OI 订单流仓位乘数
                if self.flow_monitor:
                    oi_mult = self.flow_monitor.get_oi_position_multiplier(
                        f"{symbol}/USDT:USDT", direction, price)
                    pos_mult *= oi_mult
                    if oi_mult != 1.0:
                        logger.info(f"  📊 OI仓位系数 {oi_mult:.2f}x (总={pos_mult:.2f}x)")
                if pos_mult != 1.0:
                    logger.info(f"  📊 排名#{rank}/{total_cands} 总仓位系数 {pos_mult:.2f}x")
                result = self.executor.execute(symbol, direction, price, atr, adx, strategy,
                                               position_multiplier=pos_mult,
                                               market_regime=market_regime)
                if result["success"]:
                    self.total_trades += 1
                    trades_this_cycle += 1
                    self.stats["trades"] += 1
                    self._last_trade_ts = time.time()  # v4.0: 开仓间隔
                    # 记录手续费到风控
                    self.riskmon.record_trade_fees(result["total_fee"])
                    # ── v3.6: 记录仓位开仓时间 ──
                    sym_full = f"{symbol}/USDT:USDT"
                    if sym_full not in self._position_open_times:
                        self._position_open_times[sym_full] = time.time()
            else:
                logger.info(f"❌ {symbol} {direction} → DeepSeek REJECT: {reason}")
                self.stats["ai_rejects"] += 1

            time.sleep(1.0)

        # ── v3.0: 网格市场状态检查（清除非震荡市的网格） ──
        if self.grid_manager:
            for sym in list(self.grid_manager.grids.keys()):
                if self.grid_manager.is_active(sym):
                    sym_ctx = self._tf_cache.get(sym)
                    if sym_ctx:
                        regime = self._detect_market_regime(sym_ctx).get("regime", "unknown")
                        if regime != "ranging":
                            self.grid_manager.cancel_grid(sym)
                            logger.info(f"📋 市场不再震荡，取消 {sym} 网格")

        # ── v3.0: DeepSeek 异常检测 ──
        if self.config.deepseek_anomaly_detection and not self.analyst.circuit_breaker_open():
            if candidates or acct.get("positions_detail"):
                symbols_data = []
                for sig in candidates:
                    symbols_data.append({
                        "symbol": sig["symbol"], "direction": sig["direction"],
                        "price": sig["price"], "rsi": sig["rsi"],
                        "adx": sig["adx"], "vol_ratio": sig.get("vol_ratio", 1.0),
                    })
                cached_regime = "no_signal"
                for sym_key in self._tf_cache:
                    cached_regime = self._detect_market_regime(self._tf_cache[sym_key]).get("regime", "unknown")
                    break
                anomaly = self.analyst.review_market_anomaly(symbols_data, cached_regime)
                if anomaly:
                    self.tlogger.log_risk("AI_ANOMALY", anomaly)
                    logger.warning(f"🚨 DeepSeek 异常检测: {anomaly}")

        # ── v3.0: DeepSeek 市场评论 ──
        self._market_commentary = ""
        if self.config.deepseek_market_commentary and not self.analyst.circuit_breaker_open():
            cycle_data = {
                "cycle": cycle, "candidates": len(candidates),
                "trades": trades_this_cycle,
                "positions": len(acct["positions_detail"]),
                "regime": "no_signal",
            }
            for sym_key in self._tf_cache:
                cycle_data["regime"] = self._detect_market_regime(self._tf_cache[sym_key]).get("regime", "unknown")
                break
            self._market_commentary = self.analyst.generate_commentary(cycle_data)
            if self._market_commentary:
                logger.info(f"💬 DeepSeek 市场评论: {self._market_commentary}")

        # ── v3.1: 检测平仓交易 (当前持仓 vs 上一轮) ──
        prev_positions = getattr(self, '_prev_positions', {})
        # v4.0: 已处理平仓集合, 防止重复记录
        if not hasattr(self, '_closed_positions_done'):
            self._closed_positions_done: set = set()
        if self.learner and prev_positions:
            current_syms = {p["symbol"] + "/USDT:USDT" for p in acct["positions_detail"]}
            closed = {
                sym: pos for sym, pos in prev_positions.items()
                if sym not in current_syms and sym not in self._closed_positions_done
            }
            for sym, old_pos in closed.items():
                self._closed_positions_done.add(sym)  # v4.0: 标记已处理
                entry = float(old_pos.get("entryPrice", 0) or 0)
                mark = float(old_pos.get("markPrice", 0) or 0)
                contracts = abs(float(old_pos.get("contracts", 0) or 0))
                side = "LONG" if str(old_pos.get("side") or old_pos.get("info", {}).get("holdSide", "")).lower() == "long" else "SHORT"

                base = sym.replace("/USDT:USDT", "")

                # v4.0: 从 Bitget API 获取真实已实现 PnL (不再估算)
                real_pnl = self.exchange.fetch_closed_position_pnl(sym)
                if real_pnl and abs(real_pnl["pnl"]) > 0.01:
                    pnl = real_pnl["pnl"]
                    mark = real_pnl.get("exit_price", mark)
                    logger.info(
                        f"📊 {base} 真实PnL={pnl:+.2f} USDT "
                        f"(交易所数据, exit={mark:.4f})"
                    )
                else:
                    # 回退: API 不可用时用估算
                    mark = float(old_pos.get("markPrice", 0) or 0)
                    csize = self.exchange.get_contract_size(sym)
                    pnl = (mark - entry) * contracts * csize if side == "LONG" else (entry - mark) * contracts * csize
                    last_upnl = float(old_pos.get("unrealizedPnl", 0) or 0)
                    if abs(last_upnl) > abs(pnl) * 0.5 and abs(last_upnl) > 1:
                        pnl = last_upnl

                position_value = entry * contracts * (self.exchange.get_contract_size(sym))
                margin = float(old_pos.get("margin", position_value / self.config.leverage))
                pnl_pct = (pnl / margin) * 100 if margin > 0 else 0.0
                # v3.5: 取消残留计划单
                try:
                    for o in (self.exchange.exchange.fetch_open_orders(sym, params={"stop": True}) or []):
                        if self.exchange._is_reduce_only(o):
                            self.exchange.cancel_order(str(o.get('id','')), sym)
                except Exception:
                    pass
                # v4.0: 风险监控追踪连续亏损
                self.riskmon.record_closed_trade(pnl, base)
                # v4.0: 接线 PerformanceTracker
                if self.perf:
                    dur = 0.0
                    if sym in self._position_open_times:
                        dur = (time.time() - self._position_open_times[sym]) / 60.0
                    self.perf.record_trade(
                        symbol=base, direction=side,
                        entry=entry, exit_price=mark,
                        pnl=pnl, pnl_pct=pnl_pct,
                        strategy="pullback", duration_minutes=dur,
                    )
                self.tlogger.log_position_close(
                    symbol=base, direction=side, strategy="pullback",
                    entry_price=entry, exit_price=mark,
                    pnl=pnl, pnl_pct=pnl_pct,
                    close_reason="DETECTED", holding_hours=0,
                )
                self.learner.learn_from_closed_trade(
                    symbol=sym, direction=side,
                    entry_price=entry, exit_price=mark,
                    pnl=pnl, pnl_pct=pnl_pct,
                    strategy="pullback", ai_decision="CONFIRM",
                    market_regime=getattr(self, '_current_regime', 'unknown'),
                )
        # v4.0: 每日清理平仓记录 (防止内存泄漏)
        if getattr(self, '_closed_date', '') != today_str():
            self._closed_positions_done = set()
            self._closed_date = today_str()
        self._prev_positions = {
            f"{p['symbol']}/USDT:USDT": {
                "entryPrice": p["entry_price"],
                "markPrice": p["mark_price"],
                "contracts": p["contracts"],
                "unrealizedPnl": p.get("unrealized_pnl", 0),  # v4.0: 用于 PnL 交叉验证
                "margin": p.get("margin", 0),                  # v4.0: 用于 pnl_pct 计算
                "info": {"holdSide": "long" if p["side"] == "LONG" else "short"},
            }
            for p in acct["positions_detail"]
        }
        # ── v3.6: 清理已平仓的开仓时间记录 ──
        current_syms = {f"{p['symbol']}/USDT:USDT" for p in acct["positions_detail"]}
        for sym in list(self._position_open_times.keys()):
            if sym not in current_syms:
                del self._position_open_times[sym]
        # v3.4: 持久化仓位快照，重启后可检测平仓
        try:
            tmp = "positions_state.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "updated": now_iso(),
                    "positions": self._prev_positions,
                }, f, ensure_ascii=False)
            os.replace(tmp, "positions_state.json")
        except Exception:
            pass

        # ── v3.1: 周期性自我回顾 + v3.7: 紧急回顾 ──
        _review_trades = None
        if self.learner and (self.learner.should_review() or self.learner.should_emergency_review()):
            try:
                _review_trades = []
                with open(self.tlogger.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            evt = json.loads(line)
                            if evt.get("event") == "POSITION_CLOSE":
                                _review_trades.append(evt)
                            elif evt.get("event") == "TRADE" and evt.get("success"):
                                _review_trades.append(evt)
                        except Exception:
                            pass
            except Exception:
                _review_trades = None

        if self.learner and self.learner.should_review() and _review_trades:
            try:
                suggestions = self.learner.periodic_review(_review_trades)
                if suggestions:
                    self.tlogger.log_risk("LEARN_REVIEW", f"生成 {len(suggestions)} 条策略建议")
            except Exception as e:
                logger.warning(f"🧠 周期性回顾异常: {e}")

        if self.learner and self.learner.should_emergency_review() and _review_trades:
            try:
                logger.warning("🚨 连续亏损触发紧急回顾!")
                self.learner.mark_emergency_review_done()
                suggestions = self.learner.periodic_review(_review_trades)
                if suggestions:
                    self.tlogger.log_risk("EMERGENCY_REVIEW", f"紧急回顾: {len(suggestions)} 条建议")
            except Exception as e:
                logger.warning(f"🚨 紧急回顾异常: {e}")

        # ── v3.1: 过滤器诊断 ──
        if self.learner and len(candidates) == 0:
            try:
                diagnosis = self.learner.analyze_filter_effectiveness()
                if diagnosis:
                    logger.info(f"🧠 过滤器诊断: {diagnosis}")
            except Exception:
                pass

        # ── v3.6: ② 退出质量优化 (每 REVIEW_INTERVAL_HOURS 触发) ──
        if self.learner and self.learner.should_review():
            try:
                exit_opt = self.learner.periodic_exit_optimization()
                if exit_opt:
                    self.tlogger.log_risk("EXIT_OPTIMIZE", exit_opt)
            except Exception:
                pass

        # ── v3.6: ③ Kelly 仓位更新 ──
        if self.learner:
            try:
                # 用当前最活跃策略的 Kelly 系数更新 config
                # 默认用 pullback + 最近方向
                dominant_dir = "SHORT"  # 当前市场主方向
                kelly = self.learner.get_kelly_multiplier("pullback", dominant_dir)
                self.config.kelly_multiplier = kelly
            except Exception:
                pass

        # ── v3.7: ④ 遗传进化器 (每 2 小时，需 >= 10 笔交易) ──
        if self.evolver and self.learner:
            try:
                recent_trades = self.learner.tracker.recent_trades
                now_ts_val = time.time()

                # v4.0: 验证上一次进化 (每3轮检查一次，需要>=5笔新交易)
                if (getattr(self.evolver, '_applied_params', None) is not None and
                        len(recent_trades) >= 5 and
                        self.evolver._applied_at_cycle > 0 and
                        (cycle - self.evolver._applied_at_cycle) >= 3):
                    rolled_back = self.evolver.validate_and_rollback(recent_trades)
                    if rolled_back:
                        self.tlogger.log_risk("GENETIC_ROLLBACK", "参数退化，回滚到进化前")

                # v4.0: 新一轮进化 (2h间隔, >=10笔, 未收敛)
                if (not getattr(self.evolver, '_paused', False) and
                        len(recent_trades) >= 10 and
                        (now_ts_val - self._last_evolve_ts) > 7200):
                    self._last_evolve_ts = now_ts_val
                    result = self.evolver.evolve(recent_trades)
                    if result and result.get("best_variant"):
                        self.evolver.apply_best_variant(result)
                        self.evolver._applied_params = result["best_variant"]
                        self.evolver._applied_at_cycle = cycle
                        logger.info(f"🧬 遗传进化完成: {result.get('reasoning', '?')[:80]}")
                        self.tlogger.log_risk("GENETIC_EVOLVE",
                            f"应用最佳变体: {result.get('reasoning', '?')[:100]}")
            except Exception as e:
                logger.warning(f"🧬 遗传进化异常: {e}")

        # ── v4.0: 周度绩效快照 (每周日 00:00 UTC 附近, 604800s 间隔) ──
        if not hasattr(self, '_last_weekly_snapshot'):
            self._last_weekly_snapshot = 0.0
        if time.time() - self._last_weekly_snapshot > 604800:
            self._last_weekly_snapshot = time.time()
            try:
                if self.perf and len(self.perf.trades) >= 5:
                    m = self.perf.get_metrics()
                    logger.info(
                        f"📊 周度绩效: 交易{m['total_trades']}笔 胜率{m['win_rate']:.0f}% "
                        f"Sharpe={m['sharpe_ratio']:.2f} Sortino={m['sortino_ratio']:.2f} "
                        f"Calmar={m['calmar_ratio']:.2f} 最大回撤{m['max_drawdown_pct']:.1f}%"
                    )
                    self.tlogger.log_risk("WEEKLY_SNAPSHOT",
                        f"Sharpe={m['sharpe_ratio']:.2f} WR={m['win_rate']:.0f}% "
                        f"PnL={m['total_pnl']:+.2f} MaxDD={m['max_drawdown_pct']:.1f}%")
                # 因子归因
                if self.learner and hasattr(self.learner.tracker, 'get_factor_attribution'):
                    attr = self.learner.tracker.get_factor_attribution()
                    if attr.get("status") != "insufficient_data":
                        ranked = sorted(
                            [(k, v.get("spread", 0)) for k, v in attr.items()
                             if isinstance(v, dict) and v.get("spread") is not None],
                            key=lambda x: x[1], reverse=True)
                        if ranked:
                            logger.info(f"📊 因子归因: 最佳={ranked[0][0]}(Δ={ranked[0][1]:.1f}%) "
                                       f"最差={ranked[-1][0]}(Δ={ranked[-1][1]:.1f}%)")
            except Exception as e:
                logger.debug(f"📊 周度快照异常: {e}")

        # ── v3.6: ① 信号率自适应 (无需 DeepSeek，纯统计) ──
        if self.learner:
            try:
                # 收集最近周期数据
                if not hasattr(self, '_cycle_history'):
                    self._cycle_history = []
                self._cycle_history.append({
                    "cycle": cycle, "candidates": len(candidates),
                    "trades": trades_this_cycle,
                })
                if len(self._cycle_history) > 100:
                    self._cycle_history = self._cycle_history[-80:]

                tune_result = self.learner.adaptive_signal_rate_tune(self._cycle_history)
                if tune_result:
                    self.tlogger.log_risk("ADAPTIVE_TUNE", tune_result)
            except Exception:
                pass

        # ── v3.2: 健康检查 ──
        if self.health:
            try:
                self.health.check(
                    cycle, len(candidates), trades_this_cycle,
                    acct["positions_detail"],
                    errors=(1 if self.analyst.circuit_breaker_open() else 0)
                )
            except Exception:
                pass

        # ── 周期日志 (v2.7: 复用预取 acct，不再重复调用 API) ──
        # 从 TF 缓存中取任意已分析币种的 market_regime
        cached_regime = "no_signal"
        if candidates:
            for sym_key in self._tf_cache:
                cached_regime = self._detect_market_regime(self._tf_cache[sym_key]).get("regime", "unknown")
                break
        all_time = self.riskmon.current_equity - self.riskmon.initial_equity
        self.tlogger.log_cycle(cycle, len(candidates), trades_this_cycle,
                                self.riskmon.current_equity,
                                self.riskmon.daily_pnl_pct,
                                self.riskmon.cumulative_fees,
                                cached_regime,
                                acct["equity"], acct["unrealized_pnl"],
                                self.riskmon.initial_equity, all_time)

        # ── v2.7: 更新状态文件，复用预取 acct ──
        self._update_status_file(acct)

    def shutdown(self):
        """优雅退出: 写最终状态、关闭连接、记录停止事件"""
        logger.info("🛑 正在关闭 …")
        # ── v3.0: 取消所有网格 ──
        if self.grid_manager:
            try:
                self.grid_manager.cancel_all()
            except Exception as e:
                logger.warning(f"⚠️  网格清理失败: {e}")
        # ── v3.0: 关闭新闻客户端 ──
        if self.news_client:
            try:
                self.news_client.close()
            except Exception:
                pass
        # ── v3.1: 关闭学习引擎 ──
        if self.learner:
            try:
                self.learner.close()
            except Exception:
                pass
        try:
            self._update_status_file()
            self.tlogger.log_risk("SHUTDOWN", "bot stopped gracefully")
            logger.info("✅ 最终状态已保存")
        except Exception as e:
            logger.error(f"⚠️  关闭时保存状态失败: {e}")
        try:
            self.analyst.close()
        except Exception:
            pass
        logger.info("👋 DeepSeekQuantBot v3.0 已停止")

    def _detect_startup_closes(self):
        """v3.4: 启动时对比持久化仓位快照，检测停机期间的平仓"""
        state_file = "positions_state.json"
        if not os.path.exists(state_file):
            return
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                prev = json.load(f)
            prev_positions = prev.get("positions", {})
            if not prev_positions:
                return
            current_positions = self.exchange.get_open_positions()
            for sym, old_pos in prev_positions.items():
                if sym not in current_positions:
                    entry = float(old_pos.get("entryPrice", 0) or 0)
                    mark = float(old_pos.get("markPrice", 0) or 0)
                    contracts = abs(float(old_pos.get("contracts", 0) or 0))
                    side = "LONG" if str(old_pos.get("side") or old_pos.get("info", {}).get("holdSide", "")).lower() == "long" else "SHORT"
                    pnl = (mark - entry) * contracts if side == "LONG" else (entry - mark) * contracts
                    pnl_pct = (pnl / (entry * contracts / self.config.leverage)) * 100 if (entry * contracts) > 0 else 0
                    logger.info(
                        f"📋 检测到停机期平仓: {sym} {side} "
                        f"入场{entry:.4f} 估算pnl={pnl:+.2f} ({pnl_pct:+.1f}%)"
                    )
                    if self.learner:
                        base = sym.replace("/USDT:USDT", "")
                        # v4.0: learn_from_closed_trade 内部会调 record_closed_trade，不再重复调用
                        try:
                            self.learner.learn_from_closed_trade(
                                symbol=base, direction=side,
                                entry_price=entry, exit_price=mark,
                                pnl=pnl, pnl_pct=pnl_pct,
                                strategy="pullback", ai_decision="CONFIRM",
                                reason="停机期间平仓",
                                market_regime=getattr(self, '_current_regime', 'unknown'),
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"⚠️  启动平仓检测异常: {e}")

    def _ensure_all_positions_protected(self):
        """v3.3: 启动时为所有存量仓位检查并补挂 SL/TP (v3.5: 先清理超额)"""
        try:
            positions = self.exchange.get_open_positions()
            if not positions:
                return
            logger.info("🛡️  检查存量仓位止损保护 …")
            protected = 0
            cleaned = 0
            # v3.6: 填充存量仓位的开仓时间（保守估算为 30 分钟前）
            for sym, pos in positions.items():
                if abs(float(pos.get("contracts", 0) or 0)) > 0:
                    if sym not in self._position_open_times:
                        self._position_open_times[sym] = time.time() - 1800  # 假设已持仓30分钟
            for sym, pos in positions.items():
                contracts = abs(float(pos.get("contracts", 0) or 0))
                if contracts <= 0:
                    continue
                entry = float(pos.get("entryPrice", 0) or 0)
                if entry <= 0:
                    continue
                # 判断方向 (优先 ccxt 标准化字段, info.holdSide 为回退)
                pos_side_raw = pos.get("side") or pos.get("info", {}).get("holdSide", "")
                side = "buy" if str(pos_side_raw).lower() in ("long", "buy") else "sell"
                # v4.0 fix: 用新的 _has_position_tpsl 检测 (查计划单 + pos-tpsl API)
                side_str = "LONG" if str(pos_side_raw).lower() in ("long", "buy") else "SHORT"
                has_sl, has_tp = self.exchange._has_position_tpsl(sym, entry, side_str)

                # v3.5: 如果已有 SL/TP 但超额订单多，清理多余的
                try:
                    stop_orders = self.exchange.exchange.fetch_open_orders(sym, params={"stop": True}) or []
                except Exception:
                    stop_orders = []
                reduce_orders = [o for o in stop_orders if self.exchange._is_reduce_only(o)]
                if len(reduce_orders) > 4:
                    logger.warning(f"🧹 {sym} 启动清理: {len(reduce_orders)}→2个")
                    reduce_orders.sort(key=lambda o: str(o.get('id', '')))
                    for o in reduce_orders[:-2]:
                        self.exchange.cancel_order(str(o.get('id','')), sym)
                    cleaned += 1

                if has_sl and has_tp:
                    protected += 1
                    continue
                # 计算 ATR 估算 SL/TP
                try:
                    df = self.indicator.compute_all(
                        self.exchange.fetch_ohlcv(sym)
                    )
                    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]) else entry * 0.01
                except Exception:
                    atr = entry * 0.01
                sl_price = entry + 1.5 * atr if side == "sell" else entry - 1.5 * atr
                tp_price = entry - 2.0 * atr if side == "sell" else entry + 2.0 * atr
                sl_price = round(sl_price, 4)
                tp_price = round(tp_price, 4)
                if sl_price <= 0 or tp_price <= 0:
                    continue
                logger.info(f"🛡️  补挂 {sym} SL/TP: SL={sl_price} TP={tp_price}")
                self.exchange.set_position_sl_tp(sym, side, sl_price, tp_price)
                protected += 1
            if cleaned > 0:
                logger.info(f"🧹 启动时清理了 {cleaned} 个仓位的超额订单")
            logger.info(f"🛡️  存量仓位保护检查完成: {protected}/{len(positions)} 已保护")
        except Exception as e:
            logger.warning(f"⚠️  启动保护检查异常: {e}")

    def run(self):
        """主循环：每 5 分钟执行一次"""
        # v4.0: PID 文件锁 — 防重复启动
        pidfile = "/tmp/bot.pid"
        if os.path.exists(pidfile):
            try:
                with open(pidfile) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # 检查进程是否存在
                logger.error(f"❌ 已有 bot 实例在运行 (PID {old_pid})。退出。")
                sys.exit(1)
            except (OSError, ValueError):
                pass  # 进程不存在或 PID 无效，继续
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))

        logger.info("\n🎯 DeepSeekQuantBot v3.0 进入主循环 (每 5 分钟)")
        logger.info("按 Ctrl+C 停止\n")

        # ── v3.4: 检测停机期间的平仓 ──
        self._detect_startup_closes()

        # ── v3.3: 启动时为所有存量仓位补挂 SL/TP ──
        self._ensure_all_positions_protected()

        # 注册 SIGTERM 信号处理 (容器优雅关闭)
        def _handle_signal(signum, frame):
            logger.info(f"\n📡 收到信号 {signum}，优雅退出 …")
            self.shutdown()
            sys.exit(0)
        try:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        except Exception:
            pass  # 某些平台不支持 signal

        while True:
            try:
                cycle_start = time.time()
                self.run_once()

                # ── 计算休眠时间：对齐到下一个 5 分钟 ──
                _now = now()
                minutes_to_next_five = 5 - (_now.minute % 5)
                if minutes_to_next_five == 0 and _now.second > 5:
                    minutes_to_next_five = 5
                sleep_seconds = max(
                    minutes_to_next_five * 60 - _now.second, 30
                )

                elapsed = time.time() - cycle_start
                # v4.0: 持久化运行时参数 (每周期)
                try:
                    self.config.save_runtime_params()
                except Exception:
                    pass
                logger.info(
                    f"⏰ 本轮耗时 {elapsed:.1f}s，休眠 {sleep_seconds:.0f}s "
                    f"至下一轮 {now() + timedelta(seconds=sleep_seconds):%H:%M:%S}"
                )
                time.sleep(sleep_seconds)

            except KeyboardInterrupt:
                self.shutdown()
                break
            except Exception as e:
                logger.error(f"💥 主循环异常: {e}", exc_info=True)
                logger.info("⏳ 60 秒后重试 …")
                time.sleep(60)


# ============================================================================
# 主入口
# ============================================================================
if __name__ == "__main__":
    bot = DeepSeekQuantBot()
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.shutdown()
