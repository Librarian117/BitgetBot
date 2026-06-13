#!/usr/bin/env python3
"""config_manager.py — 配置管理 (从 .env 加载)"""

import json
import logging
import os
from typing import Dict, List

from dotenv import load_dotenv

from time_utils import now_iso

logger = logging.getLogger("QuantBot")


class ConfigManager:
    """从 .env 加载配置，集中管理所有策略参数"""

    # ── 默认监控币种（无 .env 配置时使用）──
    DEFAULT_SYMBOLS = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
        "DOGE/USDT:USDT", "DOT/USDT:USDT", "LINK/USDT:USDT",
        "LTC/USDT:USDT",
    ]

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
        self.adx_threshold  = 16     # ADX < 16 → 无趋势震荡，跳过信号
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
        self.daily_loss_limit = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))
        # v4.5: 日损触发后是否强制清仓 (默认 true — 真正的 emergency exit)
        self.daily_loss_close_all = os.getenv("DAILY_LOSS_CLOSE_ALL", "true").lower() == "true"
        # v4.5: 沙箱 emergency dry-run — 触发完整路径但不真实平仓, 用于演练
        self.emergency_dry_run = os.getenv("EMERGENCY_DRY_RUN", "false").lower() == "true"

        # ── v3.1: 初始资金 (用于计算总盈亏) ──
        self.initial_equity = float(os.getenv("INITIAL_EQUITY", "0"))

        # ── DeepSeek API ──
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        self.deepseek_url   = "https://api.deepseek.com/v1/chat/completions"
        self.deepseek_temp  = 0.3
        self.deepseek_timeout = 45
        self.deepseek_enable_tools = os.getenv("DEEPSEEK_ENABLE_TOOLS", "true").lower() == "true"

        # ── v4.1: AI 研究层开关 ──
        # 总开关: false=完全关闭所有 AI API 调用 (信号解释/复盘/审查/诊断)
        self.deepseek_master_switch = os.getenv("DEEPSEEK_MASTER_SWITCH", "true").lower() == "true"
        self.deepseek_explain_signal_enabled = os.getenv("DEEPSEEK_EXPLAIN_SIGNAL_ENABLED", "true").lower() == "true"
        self.deepseek_genetic_opinion_enabled = os.getenv("DEEPSEEK_GENETIC_OPINION_ENABLED", "true").lower() == "true"
        self.deepseek_reflection_enabled = os.getenv("DEEPSEEK_REFLECTION_ENABLED", "true").lower() == "true"
        self.deepseek_rule_generation_enabled = os.getenv("DEEPSEEK_RULE_GENERATION_ENABLED", "true").lower() == "true"
        self.deepseek_periodic_review_enabled = os.getenv("DEEPSEEK_PERIODIC_REVIEW_ENABLED", "true").lower() == "true"
        self.deepseek_filter_analysis_enabled = os.getenv("DEEPSEEK_FILTER_ANALYSIS_ENABLED", "true").lower() == "true"

        # 总开关覆盖所有子开关
        if not self.deepseek_master_switch:
            for attr in ("deepseek_explain_signal_enabled", "deepseek_genetic_opinion_enabled",
                         "deepseek_reflection_enabled", "deepseek_rule_generation_enabled",
                         "deepseek_periodic_review_enabled", "deepseek_filter_analysis_enabled"):
                setattr(self, attr, False)
            logger.info("🔌 DEEPSEEK_MASTER_SWITCH=false — 所有 AI API 调用已关闭")

        # ── v4.1: AI 决策层开关 (仅用于回退兼容) ──
        self.deepseek_signal_review_enabled = os.getenv("DEEPSEEK_SIGNAL_REVIEW_ENABLED", "false").lower() == "true"

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

        # ── v4.4: 交易质量守卫 ──
        # 禁止同一币种同时持有多空双向仓位 (Hedge模式)
        self.allow_hedge = os.getenv("ALLOW_HEDGE", "false").lower() == "true"
        # 最小风险回报比: TP距离/手续费距离 < 此值则拒绝开仓
        self.min_rr_ratio = float(os.getenv("MIN_RR_RATIO", "1.5"))
        # 沙箱也启用安全校验 (最小仓位/费后利润检查)
        self.sandbox_safety = os.getenv("SANDBOX_SAFETY", "true").lower() == "true"
        # v4.4: counter_trend 同质化限制 — 最多同时持有N个counter_trend仓位
        self.max_counter_trend_positions = int(os.getenv("MAX_COUNTER_TREND_POSITIONS", "1"))
        # v4.4: counter_trend RSI门槛 — LONG需RSI<=此值才为真正超卖
        self.ct_rsi_long_max = float(os.getenv("CT_RSI_LONG_MAX", "45"))
        self.ct_rsi_short_min = float(os.getenv("CT_RSI_SHORT_MIN", "55"))
        # v4.5: 单笔风险预算 — 按 SL 距离反推仓位，替代纯保证金比例
        self.risk_per_trade_pct = float(os.getenv("RISK_PER_TRADE_PCT", "0.02"))

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
        """v2.0: 从 symbols.json + .env 加载监控币种。"""
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
            usdt_perps.sort(key=lambda m: m.get("info", {}).get("baseVolume", 0) or 0, reverse=True)

            if self.symbols_mode == "top_n":
                usdt_perps = usdt_perps[:self.symbols_top_n]

            bases = []
            for m in usdt_perps:
                base = m.get("base", "")
                if base:
                    bases.append(base)
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
        """全面校验配置"""
        errors = []

        for key, val in [
            ("BITGET_API_KEY", self.bitget_api_key),
            ("BITGET_SECRET", self.bitget_secret),
            ("BITGET_PASSPHRASE", self.bitget_passphrase),
            ("DEEPSEEK_API_KEY", self.deepseek_api_key),
        ]:
            if not val:
                errors.append(f".env 缺失 {key}")

        if not (1 <= self.leverage <= 125):
            errors.append(f"LEVERAGE={self.leverage} 超出范围 [1, 125]")
        if not (0.01 <= self.margin_ratio <= 0.5):
            errors.append(f"MARGIN_RATIO={self.margin_ratio} 超出范围 [0.01, 0.5]")
        if not (0.01 <= self.daily_loss_limit <= 0.5):
            errors.append(f"DAILY_LOSS_LIMIT={self.daily_loss_limit} 超出范围 [0.01, 0.5]")
        # v4.1 fix: 遗传进化可能产生 MIN > MAX, 自动修正而非崩溃
        if self.adx_margin_min > self.adx_margin_max:
            avg = (self.adx_margin_min + self.adx_margin_max) / 2
            old_min, old_max = self.adx_margin_min, self.adx_margin_max
            self.adx_margin_min = round(avg * 0.85, 4)
            self.adx_margin_max = round(avg * 1.15, 4)
            logging.warning(
                f"⚠️  自动修正 ADX_MARGIN: MIN({old_min}) > MAX({old_max}) "
                f"→ MIN={self.adx_margin_min:.4f} MAX={self.adx_margin_max:.4f}"
            )
        if not (1 <= self.adx_threshold <= 60):
            errors.append(f"ADX_THRESHOLD={self.adx_threshold} 超出范围 [1, 60]")

        if len(self.tp_split_ratios) != len(self.tp_atr_mults):
            errors.append(f"TP_SPLIT_RATIOS={len(self.tp_split_ratios)} 项 != TP_ATR_MULTS={len(self.tp_atr_mults)} 项")
        else:
            ratio_sum = sum(self.tp_split_ratios)
            if abs(ratio_sum - 1.0) > 0.05:
                errors.append(f"TP_SPLIT_RATIOS 总和={ratio_sum:.2f}，应为 ~1.0")

        valid_tf = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w", "1M"}
        if self.timeframe not in valid_tf:
            errors.append(f"TIMEFRAME='{self.timeframe}' 无效，应为 {valid_tf}")
        for tf in self.higher_timeframes:
            if tf not in valid_tf:
                errors.append(f"HIGHER_TIMEFRAMES 含 '{tf}' 无效")

        if not (1 <= self.max_concurrent_positions <= 20):
            errors.append(f"MAX_CONCURRENT_POSITIONS={self.max_concurrent_positions} 超出 [1, 20]")

        if errors:
            raise ValueError(f"❌ 配置校验失败 ({len(errors)} 项):\n  " + "\n  ".join(errors))

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
