#!/usr/bin/env python3
"""
deepseek_quant_bot.py — 量化信号 + AI 研究顾问混合交易机器人 v4.5
=====================================================================
架构：高度模块化，专业风控，DeepSeek V4 AI 研究层

Phase 1 重构: 5 个服务类已提取为独立模块
  - indicator_calculator.py  — IndicatorCalculator (本地量化计算)
  - market_context.py        — MarketContextManager (市场情绪)
  - deepseek_analyst.py      — DeepSeekAnalyst (AI 研究层)
  - trade_logger.py          — TradeLogger (日志持久化)
  - risk_monitor.py          — RiskMonitor (日内风控)

外部模块 (18个):
  config_manager, exchange_interface, trade_executor, signal_scorer,
  session_manager, flow_monitor, genetic_evolver, self_learner,
  safety_manager, health_monitor, portfolio_manager, trailing_sl,
  grid_strategy, equity_auditor, news_integration, performance_tracker,
  markov_regime, quant_math

主控: DeepSeekQuantBot — 扫描→多TF→市场状态→量化评分→风控→下单 编排器
"""

import json
import logging
import os
import signal
import sys
import tempfile
import time
from datetime import timedelta
from time_utils import now, today_str, now_iso, now_str, datetime_from_iso
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config_manager import ConfigManager
from equity_auditor import EquityAuditor
from exchange_interface import ExchangeInterface
from quant_math import compute_quant_signals
from session_manager import SessionManager
from strategy_engine import evaluate_pullback, evaluate_momentum, evaluate_ema_cross, evaluate_bollinger  # P3
from filter_pipeline import (resolve_kalman, detect_market_regime,
                            filter_volume, adjust_direction_bias,
                            filter_btc_linkage)  # P3
from trade_executor import TradeExecutor

# ── Phase 1: 服务类提取 ──
from indicator_calculator import IndicatorCalculator
from market_context import MarketContextManager
from deepseek_analyst import DeepSeekAnalyst
from trade_logger import TradeLogger
from risk_monitor import RiskMonitor

# ============================================================================
# ◎ SessionManager — 交易时段感知 (v2.3)
# ============================================================================
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

logger = logging.getLogger("QuantBot")
logging.getLogger("ccxt").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Phase 1: 5 个服务类已提取到独立模块 (indicator_calculator, market_context, deepseek_analyst, trade_logger, risk_monitor) ──

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
        logger.info("🚀 DeepSeekQuantBot v4.5 启动中 …")
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

        # ── v4.1: 币种仓位微调 (保守, 基于统计阈值) ──
        # ETH: 去重后20%胜率, 仅降仓15%非拉黑
        self._coin_position_cap = {"ETH": 0.85}

        # ── v4.1: 独立 PnL 累计器 (用于资金审计对账) ──
        # 累计所有 POSITION_CLOSE 的 PnL, 与交易所权益对比
        self._bot_closed_pnl_total: float = 0.0
        self._bot_closed_trade_count: int = 0
        # v4.5→Phase2: 统一平仓出口 — 幂等缓存 + 去重
        self._closed_trades_cache: Dict[str, Any] = {}  # {dedup_key: result_dict}
        self._symbol_cooldowns: Dict[str, float] = {}    # {symbol: cooldown_until_ts}
        # v4.5: 仅追踪本次运行期间的盈亏, 跨重启不累计
        # 启动权益由交易所实际余额决定, 不用配置值

        # ── v4.1: 独立资金审计器 —— 账户净值是唯一真相 ──
        # v4.5: initial_equity 设为 0, 在首次 CYCLE 时用实际交易所权益校准
        self.equity_auditor = EquityAuditor(
            initial_equity=self.config.initial_equity or 0.0,
            alarm_threshold=50.0,
        )

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
            "adx_skips": 0,        # v4.5: ADX硬过滤已移除, 保留统计兼容
            "vol_skips": 0,
            "btc_filters": 0,
            "tf_skips": 0,
            "direction_skips": 0,  # v4.5: 趋势方向拒绝 (DIRECTION_BLOCK)
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
            os.path.dirname(os.path.abspath(__file__)), "data", "status.json"
        )
        # ── v2.7: 多TF分析缓存 (同一周期内复用，避免重复拉取1h/4h K线) ──
        self._tf_cache: Dict[str, Dict[str, Any]] = {}
        self._market_commentary: str = ""
        # ── v3.6: 仓位开仓时间追踪 (AI持仓审核用) ──
        self._position_open_times: Dict[str, float] = {}
        # ── v4.5: Regime 切换追踪 — Regime Transition Attribution ──
        self._regime_current: str = "unknown"
        self._regime_start_time: float = time.time()
        self._regime_start_cycle: int = 0

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
                "adx": self.stats["adx_skips"],        # v4.5: ADX硬过滤已移除, 此处应为0
                "vol": self.stats["vol_skips"],
                "btc": self.stats["btc_filters"],
                "tf": self.stats["tf_skips"],
                "direction": self.stats["direction_skips"],  # v4.5: DIRECTION_BLOCK 计数
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

                # v4.1: 1h/4h 用 iloc[-2] (最后一根已闭合K线)
                # iloc[-1] 是形成中的K线, close/high/low 不可靠
                idx = -2 if len(df) >= 2 else -1
                latest = df.iloc[idx]
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

    @staticmethod
    def _resolve_kalman(direction: str, strategy: str,
                        kalman_dir: str, kalman_score: float,
                        ema_kalman_conflict: bool,
                        is_bearish_trend: bool, is_bullish_trend: bool) -> Dict[str, Any]:
        """P3: wrapper → filter_pipeline.resolve_kalman()"""
        return resolve_kalman(direction, strategy, kalman_dir, kalman_score,
                              ema_kalman_conflict, is_bearish_trend, is_bullish_trend)

    # ==================================================================
    # v4.5→Phase2: 统一平仓出口 — 所有平仓路径的唯一记录点
    # ==================================================================

    # ── 去重键持久化 (崩溃恢复) ──
    _CLOSED_KEYS_FILE = "data/closed_trades_keys.txt"

    def _persist_closed_key(self, key: str):
        """追加去重键到持久化文件 (崩溃恢复用)"""
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._CLOSED_KEYS_FILE, "a", encoding="utf-8") as f:
                f.write(key + "\n")
        except Exception:
            pass

    def _restore_closed_keys(self):
        """启动时恢复去重键到内存缓存 (防止 _detect_startup_closes 重复处理)"""
        if not os.path.exists(self._CLOSED_KEYS_FILE):
            return
        try:
            restored = 0
            with open(self._CLOSED_KEYS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    key = line.strip()
                    if key and key not in self._closed_trades_cache:
                        self._closed_trades_cache[key] = {
                            "pnl": 0, "pnl_pct": 0, "pnl_source": "RESTORED",
                            "funding_fee": 0, "fees": 0, "trade_id": key,
                            "already_processed": True,
                        }
                        restored += 1
            if restored > 0:
                logger.info(f"📥 恢复 {restored} 个已平仓去重键 (防崩溃重复)")
        except Exception:
            logger.debug("⚠️ 去重键恢复失败", exc_info=True)

    @staticmethod
    def _build_dedup_key(close_order_result: Optional[Dict], symbol: str,
                         open_ts: float, contracts: float) -> str:
        """构造平仓去重键 (三级优先级)

        1. 交易所订单 ID (最可靠)
        2. 时间戳 + symbol + 张数
        3. 当前时间 (启动同步回退)
        """
        # 优先级 1: 交易所平仓订单 ID
        if close_order_result:
            oid = (close_order_result.get("id")
                   or close_order_result.get("orderId")
                   or close_order_result.get("order_id"))
            if oid:
                return f"order:{oid}"
        # 优先级 2: 开仓时间戳 + symbol + 张数 (不含 close_ts — 避免 race condition)
        if open_ts > 0:
            return f"ts:{symbol}:{int(open_ts)}:{int(contracts)}"
        # 优先级 3: symbol + 张数 (启动同步回退)
        return f"ts:{symbol}:0:{int(contracts)}"

    def _write_trade_ledger(self, trade: Dict[str, Any]):
        """v4.5→Phase2: 写入唯一交易账本 (单一数据源)

        logs/trade_ledger.jsonl — 每笔平仓仅一条记录
        后续 performance_tracker / equity_auditor 可统一从此读取
        """
        import json as _json
        trade_id = (f"{trade['symbol']}-{trade['side']}-"
                     f"{int(trade.get('open_ts', time.time()))}")
        record = {
            "event": "TRADE_CLOSE",
            "trade_id": trade_id,
            "symbol": trade["symbol"],
            "side": trade["side"],
            "strategy": trade.get("strategy", "pullback"),
            "entry": round(trade.get("entry", 0), 4),
            "exit": round(trade.get("exit", 0), 4),
            "pnl": round(trade["pnl"], 4),
            "pnl_pct": round(trade.get("pnl_pct", 0), 2),
            "pnl_source": trade.get("pnl_source", "FALLBACK"),
            "fee": round(trade.get("fee", 0), 4),
            "funding": round(trade.get("funding", 0), 4),
            "net_profit": round(trade.get("net_profit", 0), 4),  # P0: 审计字段
            "close_reason": trade.get("close_reason", "UNKNOWN"),
            "open_ts": int(trade.get("open_ts", 0)),
            "close_ts": int(trade.get("close_ts", time.time())),
            "duration_min": round(trade.get("duration_min", 0), 1),
        }
        try:
            with open("logs/trade_ledger.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.error("❌ Trade Ledger 写入失败", exc_info=True)

    def _finalize_closed_position(self,
        symbol: str,
        side: str,
        entry: float,
        contracts: float,
        margin: float,
        upl: float,
        close_reason: str,
        open_ts: float = 0,
        strategy: str = "pullback",
        close_order_result: dict = None,
        is_emergency: bool = False,
        mark_price: float = 0,
    ) -> Dict[str, Any]:
        """统一平仓出口 — 所有平仓路径的唯一记录点 (幂等)

        约束:
        1. 幂等: 同一 dedup_key 多次调用 → 返回缓存结果
        2. PnL 来源标记: "API" | "FALLBACK" — 禁止混算
        3. 单一写入: 所有 _bot_closed_pnl_total / perf / riskmon / learner 仅在此写入

        Returns:
            {pnl, pnl_pct, pnl_source, funding_fee, fees, trade_id, already_processed}
        """
        base = symbol.replace("/USDT:USDT", "")
        sym_full = symbol if "/" in symbol else f"{symbol}/USDT:USDT"

        # ── STEP 0: 幂等检查 ──
        dedup_key = self._build_dedup_key(close_order_result, sym_full, open_ts, contracts)
        if dedup_key in self._closed_trades_cache:
            cached = self._closed_trades_cache[dedup_key]
            if cached != "PROCESSING":
                cached["already_processed"] = True
                return cached
            else:
                logger.warning(f"⚠️ 递归调用 _finalize_closed_position 已防御: {dedup_key}")
                return {"pnl": upl, "pnl_pct": 0, "pnl_source": "FALLBACK",
                        "funding_fee": 0, "fees": 0, "trade_id": dedup_key,
                        "already_processed": True}

        self._closed_trades_cache[dedup_key] = "PROCESSING"

        try:
            # ── STEP 1: 获取真实 PnL (API 优先 + 重试) ──
            since_ts = open_ts if open_ts > 0 else time.time() - 3600
            max_retries = 2 if is_emergency else 4
            delays = [0.5, 1.0, 2.0, 3.0][:max_retries]

            real_pnl = None
            for delay in delays:
                time.sleep(delay)
                try:
                    real_pnl = self.exchange.fetch_closed_position_pnl(sym_full, since=since_ts)
                except Exception:
                    real_pnl = None
                if real_pnl and abs(real_pnl.get("pnl", 0)) > 0.001:
                    break

            # ── STEP 2: 确定 PnL 来源 (禁止混算) ──
            csize = self.exchange.get_contract_size(sym_full)
            taker_fee = self.exchange.get_taker_fee(sym_full)

            if real_pnl and abs(real_pnl.get("pnl", 0)) > 0.001:
                pnl = real_pnl["pnl"]
                pnl_source = "API"
                exit_price = real_pnl.get("exit_price", 0) or mark_price
                api_fee = real_pnl.get("fee", 0) + real_pnl.get("open_fee", 0)
                api_funding = real_pnl.get("funding_fee", 0)  # P0修复: adapter 现在返回 totalFunding
                api_net_profit = real_pnl.get("net_profit", 0)  # P0: 审计口径
                funding_fee = api_funding  # Bitget totalFunding 字段
            else:
                # v4.5→Phase2: Bitget fee 为负值 (openFee/closeFee < 0 = 支出)
                # FALLBACK 路径: pnl = upl - est_fee (已扣费), api_fee 统一为负值
                est_fee = entry * abs(contracts) * csize * taker_fee * 2
                pnl = upl - est_fee
                pnl_source = "FALLBACK"
                exit_price = 0
                api_fee = -est_fee              # 负值 = 支出 (与 API 路径一致)
                api_net_profit = pnl             # FALLBACK: pnl 已扣费 → net_profit = pnl
                # FALLBACK: 尝试从持仓 API 获取 funding fee
                try:
                    funding_fee = self.exchange.fetch_position_funding_fee(sym_full)
                except Exception:
                    funding_fee = 0
                logger.warning(
                    f"⚠️ {base} PnL回退: API不可用, upl={upl:+.2f} → "
                    f"pnl={pnl:+.2f} (已扣估费{est_fee:.4f})"
                )

            # ── STEP 3: 记录费用 (仅此一处写入 cumulative_fees) ──
            # P0修复: Bitget fee 字段为负值 (openFee/closeFee < 0 = 支出),
            # 原 > 0 判断导致 API 路径从不记录手续费
            if api_fee != 0:
                self.riskmon.record_trade_fees(abs(api_fee))
            if funding_fee != 0:
                self.riskmon.record_funding_fees(abs(funding_fee))

            # ── STEP 4: PnL% ──
            pnl_pct = (pnl / margin) * 100 if margin > 0 else 0.0

            # ── STEP 5: 清理 TPSL 缓存 ──
            try:
                _stale = [k for k in self.exchange._tpsl_cache if k[0] == sym_full]
                for k in _stale:
                    del self.exchange._tpsl_cache[k]
            except Exception:
                pass

            # ── STEP 6: 取消已平仓仓位的 TPSL (仅孤立单, 不影响其他活跃持仓) ──
            try:
                if hasattr(self.exchange, '_uta_cancel_orphan_tpsl'):
                    # UTA V3: 只取消当前 symbol+side 的 TPSL
                    raw_sym = sym_full.split(":")[0].replace("/", "")
                    active_set = {(raw_sym, side.lower())}
                    self.exchange._uta_cancel_orphan_tpsl(active_set)
                else:
                    # Classic V2 fallback
                    for o in (self.exchange.exchange.fetch_open_orders(
                        sym_full, params={"stop": True}) or []):
                        if self.exchange._is_reduce_only(o):
                            self.exchange.cancel_order(str(o.get('id', '')), sym_full)
            except Exception:
                pass

            # ── STEP 7: 单一写入入口 ──
            dur_min = 0.0
            if open_ts > 0:
                dur_min = (time.time() - open_ts) / 60.0

            final_exit_price = exit_price if exit_price > 0 else mark_price

            # 7a. 风险监控
            self.riskmon.record_closed_trade(pnl, base)

            # 7b. 绩效跟踪
            if self.perf:
                try:
                    self.perf.record_trade(
                        symbol=base, direction=side, entry=entry,
                        exit_price=final_exit_price, pnl=pnl, pnl_pct=pnl_pct,
                        strategy=strategy, duration_minutes=dur_min,
                    )
                except Exception:
                    logger.debug("⚠️ perf.record_trade 失败", exc_info=True)

            # 7c. 权益累加
            self._bot_closed_pnl_total += pnl
            self._bot_closed_trade_count += 1

            # 7d. 自学习
            try:
                self.learner.learn_from_closed_trade(
                    symbol=sym_full, direction=side,
                    entry_price=entry, exit_price=final_exit_price,
                    pnl=pnl, pnl_pct=pnl_pct,
                    strategy=strategy, ai_decision="CONFIRM",
                    market_regime=getattr(self, '_current_regime', 'unknown'),
                )
            except Exception:
                logger.debug("⚠️ learner 失败", exc_info=True)

            # 7e. 日志
            self.tlogger.log_position_close(
                symbol=base, direction=side, strategy=strategy,
                entry_price=entry, exit_price=final_exit_price,
                pnl=pnl, pnl_pct=pnl_pct, close_reason=close_reason,
                holding_hours=dur_min / 60.0, funding_fee=funding_fee,
            )

            factors = self._open_trade_factors.pop(sym_full, {}) if hasattr(self, '_open_trade_factors') else {}
            self.tlogger.log_exit_snapshot(
                symbol=base, direction=side,
                strategy=factors.get("strategy", strategy),
                entry_price=entry, exit_price=final_exit_price,
                pnl=pnl, pnl_pct=pnl_pct,
                exit_reason=close_reason,
                hold_minutes=dur_min,
                score=factors.get("confidence", 0),
                ema_trend=factors.get("ema_trend", ""),
                kalman_dir=factors.get("_kalman_dir", ""),
                market_regime=factors.get("market_regime", ""),
                funding_fee=funding_fee,
                pnl_source=pnl_source,
            )

            # 7f. Trade Ledger
            self._write_trade_ledger({
                "symbol": base, "side": side, "strategy": strategy,
                "entry": entry, "exit": final_exit_price,
                "pnl": pnl, "pnl_pct": pnl_pct,
                "fee": api_fee, "funding": funding_fee,
                "net_profit": api_net_profit,
                "close_reason": close_reason, "pnl_source": pnl_source,
                "open_ts": open_ts, "close_ts": time.time(),
                "duration_min": dur_min,
            })

            # v4.5→Phase2: 持久化去重键 — 防止崩溃后重启时 _detect_startup_closes 重复处理
            self._persist_closed_key(dedup_key)

            # ── STEP 8: 清理 ──
            self._position_open_times.pop(sym_full, None)

            # ── STEP 9: 币种冷却 ──
            if pnl < 0:
                self._symbol_cooldowns[base] = time.time() + 300

            # ── STEP 10: 缓存结果 ──
            result = {
                "pnl": pnl, "pnl_pct": pnl_pct, "pnl_source": pnl_source,
                "funding_fee": funding_fee, "fees": api_fee,
                "trade_id": dedup_key, "already_processed": False,
            }
            self._closed_trades_cache[dedup_key] = result
            # v4.5→Phase2: 同时注册 ts: 键 — 防止 pos-tpsl 检测路径重复
            if not dedup_key.startswith("ts:"):
                ts_fallback = f"ts:{sym_full}:{int(open_ts)}:{int(contracts)}"
                if ts_fallback not in self._closed_trades_cache:
                    self._closed_trades_cache[ts_fallback] = result
            return result

        except Exception as e:
            logger.error(f"💥 _finalize_closed_position 异常: {e}", exc_info=True)
            # v4.5→Phase2: 异常回退也扣除估算费 (与正常 FALLBACK 一致)
            csize = self.exchange.get_contract_size(sym_full)
            taker_fee = self.exchange.get_taker_fee(sym_full)
            est_fee = entry * abs(contracts) * csize * taker_fee * 2
            fallback_pnl = upl - est_fee
            # P0修复: pnl_source 区分正常 FALLBACK 和异常回退
            fallback = {"pnl": fallback_pnl, "pnl_pct": 0, "pnl_source": "FALLBACK_EXCEPTION",
                        "funding_fee": 0, "fees": est_fee, "trade_id": dedup_key,
                        "already_processed": False}
            self._closed_trades_cache[dedup_key] = fallback
            self._position_open_times.pop(sym_full, None)
            # P0修复: 异常回退必须写最小 ledger (防止静默财务黑洞)
            exc_dur_min = 0.0
            if open_ts > 0:
                exc_dur_min = (time.time() - open_ts) / 60.0
            try:
                self._write_trade_ledger({
                    "symbol": base, "side": side, "strategy": strategy,
                    "entry": entry, "exit": mark_price,
                    "pnl": fallback_pnl, "pnl_pct": 0,
                    "fee": est_fee, "funding": 0,
                    "close_reason": close_reason + "_EXCEPTION",
                    "pnl_source": "FALLBACK_EXCEPTION",
                    "open_ts": open_ts, "close_ts": time.time(),
                    "duration_min": exc_dur_min,
                })
            except Exception:
                pass  # 连 ledger 写入也失败时, 至少 dedup key 已缓存
            try:
                self._persist_closed_key(dedup_key)
            except Exception:
                pass
            return fallback

    def _audit_snapshot(self, acct: Dict[str, Any]):
        """v4.1: 独立资金审计快照 (在所有返回路径都调用)

        偏差 = 交易所权益 - (初始资金 + bot日志累计PnL - 手续费)
        偏差 > alarm_threshold → 报警 (说明日志统计不可信)
        """
        try:
            self.equity_auditor.snapshot(
                exchange_equity=acct["equity"],
                exchange_balance=acct.get("balance", acct["equity"]),
                exchange_margin=acct.get("used_margin", 0),
                exchange_upl=acct.get("unrealized_pnl", 0),
                bot_realized_pnl=self._bot_closed_pnl_total,  # 日志累计 PnL
                bot_fees=self.riskmon.cumulative_fees,
                bot_funding_fees=self.riskmon.cumulative_funding_fees,  # v4.5→Phase2
                open_positions=len(acct.get("positions_detail", [])),
                extra={
                    "bot_trade_count": self._bot_closed_trade_count,
                    "riskmon_equity": self.riskmon.current_equity,
                },
            )
            result = self.equity_auditor.check()
            if result["status"] == "ALARM":
                logger.error(
                    f"🚨 资金审计报警! 偏差={result['deviation']:+.2f} USDT "
                    f"(交易所={result['exchange_equity']:.2f} "
                    f"bot预期={result['expected_equity']:.2f}) "
                    f"累计报警{result['total_alarms']}次"
                )
        except Exception:
            pass  # 审计失败不影响交易

    def _detect_market_regime(self, tf_context: Dict[str, Any],
                              atr: float = 0, close: float = 0,
                              adx: float = 0, vol_ratio: float = 1.0,
                              markov_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """P3: wrapper → filter_pipeline.detect_market_regime()"""
        return detect_market_regime(
            tf_context, self.config.higher_timeframes,
            atr, close, adx, vol_ratio, markov_result)

    # ==================================================================
    # v4.5→P0: TPSL 触发平仓检测
    # ==================================================================
    def _detect_disappeared_positions(self, current_positions: list):
        """检测因 TPSL 触发而消失的持仓 → 查询历史 → finalize → ledger。

        返回 finalize 成功/失败数。
        """
        current_keys = set()
        for p in current_positions:
            sym = p.get("symbol", "")
            side = p.get("side", "LONG").lower()
            current_keys.add((sym, side))

        last_known = getattr(self, '_last_known_position_keys', set())
        disappeared = last_known - current_keys

        finalized = 0
        for sym, side in disappeared:
            sym_full = sym if "/" in sym else f"{sym}/USDT:USDT"
            # 幂等: 检查是否已经 finalize 过
            dedup_key = f"TPSL_DETECT_{sym}_{side}"
            if hasattr(self, '_closed_trades_cache') and dedup_key in self._closed_trades_cache:
                continue

            logger.info(f"🔍 检测到消失仓位: {sym} {side} → 查询 TPSL 平仓历史")
            try:
                # 查询 history-position 获取 PnL
                pos_side = "long" if side == "long" else "short"
                open_ts = getattr(self, '_position_open_times', {}).get(sym_full, time.time() - 3600)
                close_ctx = self.exchange._uta_get_closed_position(
                    sym_full, pos_side, since_sec=open_ts - 60)
                if not close_ctx:
                    logger.warning(f"⚠️ {sym} {side} 未找到平仓历史记录")
                    continue

                # 调用统一 finalize
                result = self._finalize_closed_position(
                    symbol=sym_full,
                    side=side.upper(),
                    entry=0,  # 从 position_open_times 或 history 补
                    contracts=0,
                    margin=1.0,
                    upl=0,
                    close_reason="TPSL_TRIGGERED",
                    open_ts=open_ts,
                    strategy=getattr(self, '_position_strategies', {}).get(sym_full, "unknown"),
                    mark_price=close_ctx.get("exit_price", 0),
                )
                finalized += 1
                logger.info(
                    f"✅ TPSL触发 finalize: {sym} {side} "
                    f"pnl={close_ctx.get('pnl'):+.4f} net={close_ctx.get('net_profit'):+.4f}"
                )
            except Exception as e:
                logger.error(f"❌ TPSL触发检测失败 {sym} {side}: {e}")

        # 更新 last_known
        self._last_known_position_keys = current_keys
        return finalized

    # ==================================================================
    # v3.6: AI 持仓审核
    # ==================================================================
    def _review_open_positions(self, positions_detail: list):
        """
        v4.5→Phase2: 规则引擎持仓审查 — 定期检查已持仓是否需要提前平仓。
        AI 决策层已禁用 (v4.1); 所有出场决策由硬编码规则引擎完成。
        每 N 轮执行一次（默认 N=3，即每15分钟）。
        仅审查 ROI > rule_exit_review_min_roi 的仓位。
        """
        logger.info(f"🔍 持仓审查 (规则引擎, AI决策层已禁用): {len(positions_detail)} 个持仓 …")

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
            if abs(roi) < self.config.rule_exit_review_min_roi:
                logger.debug(f"🔍 {symbol} ROI={roi*100:.1f}% < {self.config.rule_exit_review_min_roi*100:.0f}%，跳过审核")
                continue

            try:
                # ── 估算持有时长 ──
                holding_hours = 0.5  # 默认 0.5 小时（至少已过一轮）
                open_ts = getattr(self, '_position_open_times', {}).get(sym_full)
                if open_ts:
                    holding_hours = (time.time() - open_ts) / 3600.0

                # ── v4.0: 规则引擎优先 — 不依赖 AI 做出场决策 ──
                # v4.1: 即时亏损熔断 + 异常检测/市场评论已开启
                # v4.5→Phase2: reason 仅用于日志, close_reason 用于统一出口标记
                decision, reason, rule_exit_reason = "HOLD", "", ""
                if roi > 0.40:
                    decision, reason = "CLOSE", f"止盈规则-盈利{roi*100:.0f}%"
                    rule_exit_reason = "RULE_TP"
                elif roi < -0.50:
                    decision, reason = "CLOSE", f"紧急熔断-亏损{roi*100:.0f}%"
                    rule_exit_reason = "RULE_EMERGENCY"
                elif roi < -0.25 and holding_hours > 1.0:
                    decision, reason = "CLOSE", f"止损规则-亏损{roi*100:.0f}%超1h"
                    rule_exit_reason = "RULE_SL"
                elif holding_hours > 6.0 and abs(roi) < 0.03:
                    decision, reason = "CLOSE", f"僵尸仓规则-持仓{holding_hours:.0f}h"
                    rule_exit_reason = "RULE_STALE"

                # ── v4.5→Phase2: 规则引擎决策 (AI 出场审核已永久禁用) ──
                self.tlogger.log_rule_exit_review(
                    symbol=symbol, direction=side,
                    decision=decision, reason=reason,
                    entry_price=entry, current_price=mark,
                    roi=roi, holding_hours=holding_hours,
                )

                # ── 执行平仓 → v4.5→Phase2: 统一出口 ──
                if decision == "CLOSE":
                    logger.info(f"🔔 规则平仓 {symbol} {side}: {reason}")
                    try:
                        cts = abs(int(contracts))
                        close_side = "buy" if side == "SHORT" else "sell"
                        pos_side = "short" if side == "SHORT" else "long"
                        close_o = self.exchange.create_market_order_close(
                            sym_full, cts, close_side, pos_side
                        )
                        if close_o:
                            logger.info(f"✅ 规则平仓 {symbol} {side}: {close_o.get('id', '?')} | {reason}")
                            open_ts = self._position_open_times.get(sym_full, 0)
                            self._finalize_closed_position(
                                symbol=symbol, side=side,
                                entry=entry, contracts=cts, margin=margin,
                                upl=upl, close_reason=rule_exit_reason,
                                open_ts=open_ts,
                                strategy=getattr(self, '_position_strategies', {}).get(sym_full, "pullback"),
                                close_order_result=close_o,
                                mark_price=mark,
                            )
                        else:
                            logger.warning(f"⚠️  规则平仓 {symbol} 返回空 (可能已平仓)")
                    except Exception as e:
                        logger.error(f"❌ 规则平仓 {symbol} 失败: {e}")
                else:
                    logger.info(f"🤚 持仓 {symbol} {side}: HOLD | {reason or '规则未触发'}")

            except Exception as e:
                logger.warning(f"🔍 持仓检查 {symbol} 异常: {e}")
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

            # ── v4.0: 量化信号增强 (卡尔曼+Hurst+波动率锥) ──
            quant = compute_quant_signals(df)

            # v4.5 fix: 统一使用已闭合K线 (iloc[-2])，避免未完成K线驱动信号
            # 与高周期 _analyze_tf_context 保持一致的闭合K线策略
            cidx = -2 if len(df) >= 2 else -1
            latest = df.iloc[cidx]
            close = float(latest["close"])
            ema   = float(latest["ema"])
            rsi   = float(latest["rsi"])
            atr   = float(latest["atr"])
            adx   = float(latest["adx"])

            if pd.isna(ema) or pd.isna(rsi) or pd.isna(atr) or pd.isna(adx):
                logger.debug(f"{symbol} 指标未就绪 (NaN)，跳过")
                return None

            # v4.5: ADX 不再作为全局硬过滤器 → 趋势强度由 Regime 统一管理
            # ADX 信息通过 Regime.trend_strength 传递到评分系统，避免三重冗余：
            #  (1) 信号层硬过滤 (已移除) (2) Regime 分类 (保留) (3) Confidence 加分 (已移除)
            # 各策略如需 ADX 阈值，在自身逻辑中检查（momentum 已有 momentum_adx_threshold）
            session = SessionManager.get_session(
                self.config.adx_threshold, self.config.vol_ratio_threshold)

            # ── ② 成交量确认 (P3-B: wrapper → filter_pipeline.filter_volume) ──
            vol_result = filter_volume(df, cidx, session, self.config.is_sandbox)
            vol_ratio = vol_result["vol_ratio"]
            if not vol_result["passed"]:
                logger.debug(
                    f"{symbol} 量比={vol_ratio:.2f} < {session['vol_ratio']} "
                    f"({session['label']})，跳过"
                )
                self.tlogger.log_vol_skip(symbol, vol_ratio)
                self.tlogger.log_filter_reject(symbol, "VOL_TOO_LOW",
                    f"VolRatio={vol_ratio:.2f}<{session['vol_ratio']}")
                return None

            # ── v3.6: 趋势方向校验 (EMA50 vs EMA200) ──
            # v4.5: 使用已闭合K线，与信号生成保持一致
            ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[cidx])
            ema200 = float(df["close"].ewm(span=200, adjust=False).mean().iloc[cidx])
            ema200_prev = float(df["close"].ewm(span=200, adjust=False).mean().iloc[cidx - 5])
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

            # ── v4.3: EMA+Kalman 共识/冲突处理 ──
            # 核心改变: 冲突时不再硬拒绝，改为:
            #   1. 统计冲突样本 (EMA方向 vs Kalman方向 vs 1h后实际走势)
            #   2. 置信度扣分 (score -= 15)
            #   3. 降级为 counter_trend (半仓 + 高置信度门槛)
            kalman_dir = quant.get("kalman_direction", "flat")
            kalman_score = quant.get("kalman_score", 0)
            ema_kalman_conflict = (
                (is_bearish_trend and kalman_dir == "up")
                or (is_bullish_trend and kalman_dir == "down")
            )
            kalman_ema_disagree = False  # v4.3: 用于后续降分+降级
            if ema_kalman_conflict and abs(kalman_score) > 0.5:
                ema_label = "SHORT" if is_bearish_trend else "LONG"
                kalman_label = "LONG" if kalman_dir == "up" else "SHORT"
                # v4.3: 冲突样本统计 — 用于后续离线分析谁更准
                # 格式: EMA方向 | Kalman方向 | Kalman强度 | 当前价格
                self.tlogger.log_filter_reject(symbol, "KALMAN_VS_EMA",
                    f"EMA={ema_label} Kalman={kalman_label} "
                    f"score={kalman_score:.2f} close={close:.4f}",
                    direction="BOTH")
                logger.info(
                    f"📊 {symbol} EMA={ema_label} vs Kalman={kalman_label} "
                    f"(score={kalman_score:.2f}) — 冲突样本已记录"
                )
                kalman_ema_disagree = True  # v4.3: 标记冲突, 后续降分+降级

            # v4.3: 大趋势偏向 (P3-B: wrapper → filter_pipeline.adjust_direction_bias)
            bias = adjust_direction_bias(
                is_bearish_trend, is_bullish_trend, kalman_ema_disagree,
                effective_oversold, effective_overbought)
            block_long = bias["block_long"]
            block_short = bias["block_short"]
            effective_oversold = bias["effective_oversold"]
            effective_overbought = bias["effective_overbought"]

            # ── ③ 信号初筛 ──
            direction = None
            strategy = "pullback"  # default
            # v4.5: 记录原始策略，后续降级为counter_trend时可区分"主动逆势"和"被动降级"
            # 被动降级不应受counter_trend的极端RSI门槛限制
            _original_strategy = strategy

            # ── ③ 回调策略 (P3: extracted → evaluate_pullback) ──
            pullback_signal = evaluate_pullback(
                close, ema, rsi, effective_oversold, effective_overbought)
            if pullback_signal:
                direction = pullback_signal["direction"]
                strategy = pullback_signal["strategy"]

            # ── ④ 动量突破策略 (P3: extracted → evaluate_momentum) ──
            if direction is None:
                momentum_signal = evaluate_momentum(
                    close, ema, rsi, adx, df, cidx,
                    self.config.momentum_enabled,
                    self.config.momentum_rsi_min,
                    self.config.momentum_rsi_max,
                    self.config.momentum_adx_threshold,
                    self.config.momentum_ema_period,
                    self.config.momentum_breakout_bars,
                )
                if momentum_signal:
                    direction = momentum_signal["direction"]
                    strategy = momentum_signal["strategy"]

            # ── ⑤ EMA 交叉策略 (P3: extracted → evaluate_ema_cross) ──
            if direction is None:
                ema_cross_signal = evaluate_ema_cross(
                    df, cidx, close, ema200, rsi, self.config.momentum_enabled)
                if ema_cross_signal:
                    direction = ema_cross_signal["direction"]
                    strategy = ema_cross_signal["strategy"]

            # ── ⑥ 布林带均值回归 (P3: extracted → evaluate_bollinger) ──
            if direction is None:
                bollinger_signal = evaluate_bollinger(df, cidx, close, rsi)
                if bollinger_signal:
                    direction = bollinger_signal["direction"]
                    strategy = bollinger_signal["strategy"]

            if direction is None:
                logger.debug(
                    f"{symbol} 无信号 | price={close:.4f} "
                    f"EMA={ema:.4f} RSI={rsi:.2f} ADX={adx:.2f} ATR={atr:.4f}"
                )
                return None

            # ── v4.5: Kalman 统一决策 (替代分散的四重干预) ──
            # 旧逻辑: Kalman 同时做 ①改策略 ②扣confidence ③提required_score ④改方向门槛
            # 新逻辑: _resolve_kalman() 返回单一 action，下游只消费一次
            kalman_result = self._resolve_kalman(
                direction=direction, strategy=strategy,
                kalman_dir=kalman_dir, kalman_score=kalman_score,
                ema_kalman_conflict=ema_kalman_conflict,
                is_bearish_trend=is_bearish_trend, is_bullish_trend=is_bullish_trend)
            _kalman_action = kalman_result["action"]
            if _kalman_action == "downgrade":
                strategy = kalman_result["new_strategy"] or "counter_trend"
                _kalman_penalty = kalman_result["confidence_penalty"]
                logger.info(
                    f"🔓 {symbol} {direction} Kalman→{_kalman_action}: "
                    f"策略={strategy} 扣{_kalman_penalty}分 "
                    f"(Kalman={kalman_dir} score={kalman_score:.2f})"
                )
            elif _kalman_action == "penalize":
                _kalman_penalty = kalman_result["confidence_penalty"]
                logger.info(
                    f"🔓 {symbol} {direction} Kalman→{_kalman_action}: "
                    f"扣{_kalman_penalty}分 (Kalman={kalman_dir} score={kalman_score:.2f})"
                )
            else:  # "allow"
                _kalman_penalty = 0
            _kalman_conflict_for_scorer = kalman_result["kalman_conflict_for_scorer"]

            # ── v4.1: 币种止损冷却 —— 同币种止损后 30 分钟内禁止重新开仓 ──
            sym_cool = getattr(self, '_symbol_cooldowns', {})
            sym_key = symbol
            if sym_key in sym_cool:
                remaining = (sym_cool[sym_key] - time.time()) / 60
                if remaining > 0:
                    self.tlogger.log_filter_reject(symbol, "COOLDOWN",
                        f"止损冷却剩余{remaining:.0f}min", direction=direction)
                    logger.info(
                        f"⏳ {symbol} 止损冷却中 (剩余 {remaining:.0f}min)，跳过"
                    )
                    return None
                else:
                    del sym_cool[sym_key]  # 冷却过期, 清除

            # ── v4.1: 趋势方向校验先于方向阻止 — counter_trend 允许逆势通过 ──
            # v4.5: Kalman 冲突已由 _resolve_kalman() 统一处理 (改策略/扣分/提门槛)
            # Phase C 不再包含 Kalman 分支 — 死代码已移除
            if direction == "LONG" and strategy == "pullback":
                if is_bearish_trend:
                    if rsi > effective_oversold * 0.7:
                        self.tlogger.log_filter_reject(symbol, "DIRECTION_BLOCK",
                            f"熊市禁LONG RSI={rsi:.1f}>{effective_oversold*0.7:.0f}",
                            direction="LONG", strategy="pullback")
                        logger.debug(
                            f"⛔ {symbol} LONG-pullback 被拦截: 下跌趋势(EMA50<EMA200) "
                            f"且RSI={rsi:.1f}不够超卖(需<{effective_oversold:.0f})"
                        )
                        self.stats["direction_skips"] += 1  # v4.5: DIRECTION_BLOCK, 非ADX
                        return None
                    else:
                        logger.info(f"⚠️  {symbol} LONG-counter_trend 逆势通过 (RSI={rsi:.1f}深度超卖)")
                        strategy = "counter_trend"
            elif direction == "SHORT" and strategy == "pullback":
                if is_bullish_trend:
                    if rsi < effective_overbought * 1.2:
                        self.tlogger.log_filter_reject(symbol, "DIRECTION_BLOCK",
                            f"牛市禁SHORT RSI={rsi:.1f}<{effective_overbought*1.2:.0f}",
                            direction="SHORT", strategy="pullback")
                        logger.debug(
                            f"⛔ {symbol} SHORT-pullback 被拦截: 上涨趋势(EMA50>EMA200) "
                            f"且RSI={rsi:.1f}不够超买(需>{effective_overbought:.0f})"
                        )
                        self.stats["direction_skips"] += 1  # v4.5: DIRECTION_BLOCK, 非ADX
                        return None
                    else:
                        logger.info(f"⚠️  {symbol} SHORT-counter_trend 逆势通过 (RSI={rsi:.1f}深度超买)")
                        strategy = "counter_trend"

            # v4.0: 大趋势方向偏向 — counter_trend 除外
            # v4.1 fix: counter_trend 已在上面通过趋势校验，此处放行
            if direction == "LONG" and block_long and strategy != "counter_trend":
                self.tlogger.log_filter_reject(symbol, "DIRECTION_BLOCK",
                    "熊市禁LONG", direction="LONG", strategy=strategy)
                logger.debug(f"{symbol} LONG信号在熊市被阻止(必被多TF过滤)")
                return None
            if direction == "SHORT" and block_short and strategy != "counter_trend":
                self.tlogger.log_filter_reject(symbol, "DIRECTION_BLOCK",
                    "牛市禁SHORT", direction="SHORT", strategy=strategy)
                logger.debug(f"{symbol} SHORT信号在牛市被阻止(必被多TF过滤)")
                return None

            # ── v3.7: 方向自动开关 —— 滚动胜率过低时降仓位而非完全禁用 (v4.2) ──
            # v4.2: 胜率<30%直接封杀导致市场切换后完全错过机会
            # 改为降级 counter_trend (半仓+高置信度)，让信号有机会证明自己
            # v4.1 fix: counter_trend 是死锁逃生信号，豁免方向胜率检查
            # 它已通过趋势校验(Kalman冲突降级)且后续需置信度>=65+半仓才能执行
            if self.learner and strategy != "counter_trend":
                force = getattr(self, '_force_allow_direction', False)
                viable, skip_reason = self.learner.is_direction_viable(
                    direction, force_allow=force, symbol=symbol)
                if not viable:
                    logger.warning(
                        f"⚠️  {symbol} {direction} 方向低胜率: {skip_reason} "
                        f"→ 降级 counter_trend 半仓"
                    )
                    self.tlogger.log_direction_skip(symbol, direction, skip_reason)
                    strategy = "counter_trend"
                    # 不return — 降仓继续，让市场证明方向是否恢复

            # ── v3.2: 高胜率加成 ──
            confidence = 50  # 基础分
            bonuses = []

            # v4.5: EMA-Kalman 冲突处理已由 _resolve_kalman() 统一处理
            # 不再在此处单独扣分+改策略 (消除 Kalman 四重干预)

            # 1. MACD 背离检测 (值 20 分)
            # v4.5: 统一使用已闭合K线 cidx 及其相对偏移
            macd_hist = float(df["macd_hist"].iloc[cidx])
            macd_prev = float(df["macd_hist"].iloc[cidx - 2])
            price_change = close - float(df["close"].iloc[cidx - 5])
            macd_change = macd_hist - macd_prev
            if direction == "LONG" and price_change < 0 and macd_change > 0:
                confidence += 20
                bonuses.append("MACD底背离")
            elif direction == "SHORT" and price_change > 0 and macd_change < 0:
                confidence += 20
                bonuses.append("MACD顶背离")

            # 2. 布林带极端位 (值 15 分)
            bb_lower = float(df["bb_lower"].iloc[cidx])
            bb_upper = float(df["bb_upper"].iloc[cidx])
            if direction == "LONG" and close <= bb_lower * 1.01:
                confidence += 15
                bonuses.append("布林下轨")
            elif direction == "SHORT" and close >= bb_upper * 0.99:
                confidence += 15
                bonuses.append("布林上轨")

            # 3. 量价确认 (值 10 分)
            prev_vol = float(df["volume"].iloc[cidx - 2])
            curr_vol = float(df["volume"].iloc[cidx])
            vol_up = curr_vol > prev_vol * 1.2
            if direction == "LONG" and close > float(df["close"].iloc[cidx - 1]) and vol_up:
                confidence += 10
                bonuses.append("放量上涨")
            elif direction == "SHORT" and close < float(df["close"].iloc[cidx - 1]) and vol_up:
                confidence += 10
                bonuses.append("放量下跌")

            # 4. RSI 极端位 (值 10 分)
            # v4.5: pullback 已由 RSI 触发入场 → 不重复加分 (拆分RSI角色: 入场vs评分)
            # RSI深度对 pullback 只是入场条件的程度差异, 非独立新信息
            # 继续对 pullback 加分等于"RSI越极端越加分", 容易在趋势延续中接飞刀/摸顶
            # 其他策略 (momentum/ema_cross/bollinger) 不依赖 RSI 入场 → 保留完整加分
            if direction == "LONG" and rsi < 30:
                if strategy != "pullback":
                    confidence += 10
                    bonuses.append("RSI深度超卖")
            elif direction == "SHORT" and rsi > 70:
                if strategy != "pullback":
                    confidence += 10
                    bonuses.append("RSI深度超买")

            # 5. MACD 金叉/死叉 (值 5 分)
            macd_line = float(df["macd"].iloc[cidx])
            macd_sig = float(df["macd_signal"].iloc[cidx])
            if direction == "LONG" and macd_line > macd_sig and macd_hist > 0:
                confidence += 5
                bonuses.append("MACD多头")
            elif direction == "SHORT" and macd_line < macd_sig and macd_hist < 0:
                confidence += 5
                bonuses.append("MACD空头")

            # 6. ATR 扩张确认 (值 10 分) — ATR放大说明是真突破不是假晃
            atr_prev5 = float(df["atr"].iloc[cidx - 5:cidx].mean())
            atr_now = float(df["atr"].iloc[cidx])
            if atr_now > atr_prev5 * 1.15:
                confidence += 10
                bonuses.append("ATR扩张")

            # 7. K线实体比例 (值 10 分) — 阳线实体>影线=买方决心强
            open_p = float(df["open"].iloc[cidx])
            high_p = float(df["high"].iloc[cidx])
            low_p = float(df["low"].iloc[cidx])
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
            prev1_close = float(df["close"].iloc[cidx - 1])
            prev2_close = float(df["close"].iloc[cidx - 2])
            if direction == "LONG" and prev1_close > prev2_close and close > prev1_close:
                confidence += 10
                bonuses.append("3连阳")
            elif direction == "SHORT" and prev1_close < prev2_close and close < prev1_close:
                confidence += 10
                bonuses.append("3连阴")

            # 9. RSI 背离 (值 15 分) — 价格与RSI方向不一致=反转前兆
            rsi_now = float(df["rsi"].iloc[cidx])
            rsi_3ago = float(df["rsi"].iloc[cidx - 3])
            price_3ago = float(df["close"].iloc[cidx - 3])
            if direction == "LONG" and close < price_3ago and rsi_now > rsi_3ago:
                confidence += 15
                bonuses.append("RSI底背离")
            elif direction == "SHORT" and close > price_3ago and rsi_now < rsi_3ago:
                confidence += 15
                bonuses.append("RSI顶背离")

            bonus_str = " | ".join(bonuses) if bonuses else "无加成"
            # cap at 100
            confidence = min(100, confidence)

            # ── v4.1: Kalman 弱冲突延迟扣分 ──
            if _kalman_penalty > 0:
                old_conf = confidence
                confidence = max(30, confidence - _kalman_penalty)
                bonuses.append(f"Kalman-{_kalman_penalty}({old_conf}→{confidence})")

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
                        logger.debug("⚠️  静默异常", exc_info=True)
                markov_result = None
                if self.markov_regime:
                    try:
                        markov_result = self.markov_regime.detect(
                            df["close"].tail(100).tolist(), symbol)
                    except Exception:
                        logger.debug("⚠️  静默异常", exc_info=True)

                # v4.5: 扫描期不再构建单TF regime_info — 单TF分类不可靠
                # 执行期多TF Regime 已统一处理方向+策略路由
                # 单TF regime 的 recommended 会在 scorer 里提前惩罚策略，
                # 随后执行期又用多TF Regime 再路由一次 → 同类信息双重消费
                # 现在扫描期 scorer 收到 regime_info=None → regime 维度返回中性 50
                regime_info = None

                score_result = self.scorer.score(
                    sig={"direction": direction, "strategy": strategy,
                         "confidence": confidence, "bonuses": bonuses},
                    regime_info=regime_info,
                    sentiment=sentiment,
                    flow_signal=flow_signal,
                    markov_result=markov_result,
                    learner=self.learner,
                    strategy=strategy,  # v4.5: 策略感知门槛
                    kalman_conflict=_kalman_conflict_for_scorer,  # v4.5: Kalman统一决策
                )
                confidence = score_result["score"]
                tier = score_result["tier"]
                required_score = score_result.get("required_score", 50)  # v4.5: 统一门槛

                logger.info(
                    f"💡 {symbol} {direction} [{strategy}] "
                    f"→ {score_result['summary']}"
                )

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
                required_score = 50

            # ── v4.0: CVD 成交量背离检测 (仅实盘, 沙箱 fetch_trades 不可用) ──
            if not self.config.is_sandbox:
                try:
                    trades = self.exchange.fetch_recent_trades(symbol, limit=100)
                    if trades:
                        from quant_math import compute_cvd
                        cvd_signal = compute_cvd(trades, df["close"].values.astype(float))
                        if cvd_signal.get("divergence"):
                            bias = cvd_signal["bias"]
                            logger.info(
                                f"🔍 {symbol} CVD背离: {bias} "
                                f"(CVD={cvd_signal.get('cvd',0):.0f}, "
                                f"ratio={cvd_signal.get('cvd_ratio',0):.3f})"
                            )
                            if (bias == "bearish" and direction == "SHORT") or \
                               (bias == "bullish" and direction == "LONG"):
                                confidence = min(95, confidence + 10)
                                bonuses.append("CVD确认")
                            else:
                                confidence = max(35, confidence - 15)
                                bonuses.append("CVD背离警告")
                except Exception:
                    pass  # 获取不到成交数据不影响决策

            # ── v4.5: 统一门槛 (替代旧 tier=="REJECT" + min_conf 双重检查) ──
            # SignalScorer 按策略返回 required_score，执行层只检查一次，不再单独维护 min_conf
            if confidence < required_score:
                logger.info(
                    f"⛔ {symbol} {direction} {strategy} 评分{confidence}<门槛{required_score}"
                    f" → 拒绝 (加成: {bonus_str})"
                )
                return None

            # ── v4.4: counter_trend RSI 门槛 (非置信度阈值, 保留硬性RSI检查) ──
            # 真正的 counter_trend 信号必须处于超卖/超买极端区域
            _is_degraded = (_original_strategy == "pullback" and strategy == "counter_trend")
            if strategy == "counter_trend" and not _is_degraded:
                if direction == "LONG" and rsi > self.config.ct_rsi_long_max:
                    logger.info(
                        f"🔇 {symbol} counter_trend LONG RSI={rsi:.1f}>"
                        f"{self.config.ct_rsi_long_max} → 不是真正超卖，拒绝"
                    )
                    return None
                if direction == "SHORT" and rsi < self.config.ct_rsi_short_min:
                    logger.info(
                        f"🔇 {symbol} counter_trend SHORT RSI={rsi:.1f}<"
                        f"{self.config.ct_rsi_short_min} → 不是真正超买，拒绝"
                    )
                    return None
            elif strategy == "counter_trend" and _is_degraded:
                logger.info(
                    f"🔓 {symbol} {direction} 被动降级counter_trend (原pullback) → "
                    f"跳过极端RSI门槛 (RSI={rsi:.1f}, pullback门槛已满足)"
                )

            return {
                "symbol": symbol,
                "direction": direction,
                "strategy": strategy,
                # v4.5: 标记是否为被动降级 (pullback被Kalman/方向学习器降级为counter_trend)
                "_degraded_from_pullback": (_original_strategy == "pullback" and strategy == "counter_trend"),
                "price": close,
                "ema": ema,
                "rsi": rsi,
                "atr": atr,
                "adx": adx,
                "recent_closes": df["close"].tail(10).tolist(),
                "vol_ratio": round(vol_ratio, 2),
                "confidence": confidence,
                "tier": tier,
                "required_score": required_score,  # v4.5: 后置检查用 (VaR/方向集中扣分后重验)
                "bonuses": bonuses,
                # v4.0: 量化信号
                "_hurst": quant.get("hurst", 0.5),
                "_hurst_regime": quant.get("hurst_regime", "random_walk"),
                "_kalman_score": quant.get("kalman_score", 0),
                "_kalman_dir": kalman_dir,  # v4.5: ENTRY_SNAPSHOT 需要
                "_vol_cone_sl_mult": quant.get("vol_cone_sl_mult", 1.0),
                "_vol_cone_percentile": quant.get("vol_cone_percentile", 50),
                "_markov": markov_result,  # v4.0: 传给 _detect_market_regime
                # v4.5: EMA趋势上下文 (ENTRY_SNAPSHOT ema_trend)
                "_ema50": round(ema50, 2),
                "_ema200": round(ema200, 2),
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
    # Phase 2A: 阶段函数
    # ==================================================================

    def _phase_post_cycle(self, candidates, acct, cycle, trades_this_cycle):
        """后处理: 异常检测+市场评论+平仓检测+自学习+遗传进化+健康检查+日志+审计"""
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
        # v4.5→Phase2: 去重改用 _finalize_closed_position 幂等缓存, 不再维护独立 set
        if self.learner and prev_positions:
            current_syms = {p["symbol"] + "/USDT:USDT" for p in acct["positions_detail"]}
            for sym, old_pos in prev_positions.items():
                if sym in current_syms:
                    continue
                entry = float(old_pos.get("entryPrice", 0) or 0)
                contracts = abs(float(old_pos.get("contracts", 0) or 0))
                mark = float(old_pos.get("markPrice", 0) or 0)
                side = "LONG" if str(old_pos.get("side") or old_pos.get("info", {}).get("holdSide", "")).lower() == "long" else "SHORT"
                margin = float(old_pos.get("margin", 0) or 0)
                upl = float(old_pos.get("unrealizedPnl", 0) or 0)

                close_strategy = getattr(self, '_position_strategies', {}).get(sym, "pullback")
                open_ts = self._position_open_times.get(sym, 0)

                # v4.5→Phase2: 统一出口 — _closed_trades_cache 幂等保护防止重复
                self._finalize_closed_position(
                    symbol=sym, side=side,
                    entry=entry, contracts=contracts, margin=margin,
                    upl=upl, close_reason="MARKET_CLOSE",
                    open_ts=open_ts, strategy=close_strategy,
                    close_order_result=None, mark_price=mark,
                )
        # v4.5→Phase2: 每日清理 _closed_trades_cache (防止内存泄漏)
        if getattr(self, '_closed_date', '') != today_str():
            self._closed_trades_cache.clear()
            self._closed_date = today_str()
        # v4.1 fix: 包含本周期新开仓 (acct 是周期初快照, 不含本周期执行的交易)
        self._prev_positions = {
            f"{p['symbol']}/USDT:USDT": {
                "entryPrice": p["entry_price"],
                "markPrice": p["mark_price"],
                "contracts": p["contracts"],
                "unrealizedPnl": p.get("unrealized_pnl", 0),
                "margin": p.get("margin", 0),
                "info": {"holdSide": "long" if p["side"] == "LONG" else "short"},
            }
            for p in acct["positions_detail"]
        }
        # 合并本周期开仓记录
        if hasattr(self, '_this_cycle_trades') and self._this_cycle_trades:
            for sym_full, trade_info in self._this_cycle_trades.items():
                if sym_full not in self._prev_positions:  # 防止覆盖交易所数据
                    self._prev_positions[sym_full] = trade_info
        # ── v3.6: 清理已平仓的开仓时间记录 ──
        # v4.1 fix: 同周期新开仓位不能删 (acct是周期初快照,不含本周期新开仓)
        current_syms = {f"{p['symbol']}/USDT:USDT" for p in acct["positions_detail"]}
        this_cycle_opened = getattr(self, '_this_cycle_opened', set())
        for sym in list(self._position_open_times.keys()):
            if sym not in current_syms and sym not in this_cycle_opened:
                del self._position_open_times[sym]
        # 清理本周期记录
        if hasattr(self, '_this_cycle_opened'):
            self._this_cycle_opened.clear()
        if hasattr(self, '_this_cycle_trades'):
            self._this_cycle_trades.clear()
        # v3.4: 持久化仓位快照 + v4.1: 审计状态，重启后可恢复
        try:
            tmp = "positions_state.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "updated": now_iso(),
                    "positions": self._prev_positions,
                    "audit": {  # v4.1: 跨重启 PnL 追踪
                        "bot_closed_pnl_total": round(self._bot_closed_pnl_total, 4),
                        "bot_closed_trade_count": self._bot_closed_trade_count,
                        "cumulative_fees": round(self.riskmon.cumulative_fees, 4),
                        "drought_cycles": getattr(self, '_drought_cycles', 0),
                    },
                }, f, ensure_ascii=False)
            os.replace(tmp, "data/positions_state.json")
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

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
                            logger.debug("⚠️  静默异常", exc_info=True)
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
                logger.debug("⚠️  静默异常", exc_info=True)

        # ── v3.6: ② 退出质量优化 (每 REVIEW_INTERVAL_HOURS 触发) ──
        if self.learner and self.learner.should_review():
            try:
                exit_opt = self.learner.periodic_exit_optimization()
                if exit_opt:
                    self.tlogger.log_risk("EXIT_OPTIMIZE", exit_opt)
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)

        # ── v3.6: ③ Kelly 仓位更新 ──
        if self.learner:
            try:
                # 用当前最活跃策略的 Kelly 系数更新 config
                # 默认用 pullback + 最近方向
                dominant_dir = "SHORT"  # 当前市场主方向
                kelly = self.learner.get_kelly_multiplier("pullback", dominant_dir)
                self.config.kelly_multiplier = kelly
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)

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
                logger.debug("⚠️  静默异常", exc_info=True)

        # ── v3.2: 健康检查 ──
        if self.health:
            try:
                self.health.check(
                    cycle, len(candidates), trades_this_cycle,
                    acct["positions_detail"],
                    errors=(1 if self.analyst.circuit_breaker_open() else 0)
                )
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)

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

        # ── v4.1: 独立资金审计 ──
        self._audit_snapshot(acct)

        # ── v2.7: 更新状态文件，复用预取 acct ──
        self._update_status_file(acct)

    def _phase_execute(self, candidates, acct, btc_change, funding_rates, cycle):
        """执行交易: 逐信号过滤→下单。返回 trades_this_cycle"""
        trades_this_cycle = 0

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
            atr       = sig["atr"]
            adx       = sig["adx"]
            strategy  = sig.get("strategy", "pullback")

            # ── v2: 多时间周期分析 + v3.7 增强市场状态识别 ──
            tf_context = self._analyze_tf_context(symbol)
            vol_ratio_val = sig.get("vol_ratio", 1.0)
            markov_sig = sig.get("_markov")
            regime_info = self._detect_market_regime(
                tf_context, atr=atr, close=price, adx=adx, vol_ratio=vol_ratio_val,
                markov_result=markov_sig)
            market_regime = regime_info.get("regime", "unknown")
            # v4.5: Regime 切换检测 + 日志
            if market_regime != self._regime_current:
                duration_h = (time.time() - self._regime_start_time) / 3600.0
                self.tlogger.log_regime_change(
                    from_regime=self._regime_current,
                    to_regime=market_regime,
                    duration_hours=duration_h,
                    cycle=self.cycle_count if hasattr(self, 'cycle_count') else 0,
                )
                logger.info(
                    f"📊 Regime切换: {self._regime_current} → {market_regime} "
                    f"(持续{duration_h:.1f}h)"
                )
                self._regime_current = market_regime
                self._regime_start_time = time.time()
                self._regime_start_cycle = self.cycle_count if hasattr(self, 'cycle_count') else 0
            # v4.0: 记录当前市场状态，供平仓归因使用
            self._current_regime = market_regime
            # v4.5: _check_trend_alignment 已吸收到 Regime.allowed_directions
            # 趋势对齐不再单独检查，统一由下方 Regime 统一过滤处理
            allowed_dirs = regime_info.get("allowed_directions", ["LONG", "SHORT"])
            recommended = regime_info.get("recommended", [])

            logger.info(
                f"📊 多TF分析 [{symbol}]: 允许方向={allowed_dirs} | "
                f"市场: {market_regime} ({regime_info.get('detail','')}) "
                f"波动:{regime_info.get('volatility','?')} "
                f"趋势:{regime_info.get('trend_strength','?')} "
                f"推荐策略:{regime_info.get('recommended',[])}"
            )

            # ── v4.5: Hurst 策略路由 —— 数量化决定趋势 vs 回归 ──
            # pullback 在 signal_scorer 中归为趋势策略，统一受 Hurst 约束
            sig_hurst = sig.get("_hurst", 0.5)
            if sig_hurst > 0.55 and strategy in ("bollinger", "grid"):
                self.tlogger.log_filter_reject(symbol, "HURST",
                    f"Hurst={sig_hurst:.3f}>0.55趋-跳过{strategy}", direction=direction, strategy=strategy)
                logger.info(f"🔧 {symbol} Hurst={sig_hurst:.3f} 趋势市→跳过{strategy}回归策略")
                continue
            elif sig_hurst < 0.45 and strategy in ("momentum", "ema_cross", "pullback"):
                self.tlogger.log_filter_reject(symbol, "HURST",
                    f"Hurst={sig_hurst:.3f}<0.45回-跳过{strategy}", direction=direction, strategy=strategy)
                logger.info(f"🔧 {symbol} Hurst={sig_hurst:.3f} 回归市→跳过{strategy}趋势策略")
                continue

            # ── v4.5: Regime 统一过滤 (替代原 STRATEGY_ROUTE + TF_MISMATCH 双重检查) ──
            # 两个旧过滤器来源相同 (tf_context→Regime)，容易让同一趋势证据连续筛掉信号
            # 现在 Regime 输出 allowed_directions + recommended，执行层只检查一次
            # 均值回归策略 (bollinger, counter_trend) 豁免方向/趋势约束
            if strategy not in ("bollinger", "counter_trend"):
                reject_reason = None
                if direction not in allowed_dirs:
                    reject_reason = f"方向{direction}不在Regime允许{allowed_dirs}"
                    self.stats["tf_skips"] += 1
                elif recommended and strategy not in recommended:
                    reject_reason = f"{strategy}不在推荐{recommended}({market_regime})"
                if reject_reason:
                    reject_type = "TF_MISMATCH" if "方向" in reject_reason else "STRATEGY_ROUTE"
                    self.tlogger.log_filter_reject(symbol, reject_type,
                        reject_reason, direction=direction, strategy=strategy)
                    logger.warning(f"⛔ {symbol} Regime统一过滤: {reject_reason}")
                    if "方向" in reject_reason:
                        self.tlogger.log_tf_skip(symbol, direction,
                            f"Regime禁止:{allowed_dirs}", tf_context)
                    continue
            elif strategy in ("bollinger", "counter_trend"):
                logger.info(
                    f"🔄 {symbol} {strategy} 均值回归策略不受Regime方向约束 "
                    f"(市场: {market_regime})，放行"
                )

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

            # ── v4.0: 震荡市(rage) 网格策略 ──
            if market_regime == "range" and self.grid_manager:
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

            # ── v4.4: 禁止同币种对锁 ──
            if not self.config.allow_hedge:
                base = symbol.replace("/USDT:USDT", "")
                pos_list = acct.get("positions_detail", [])
                for existing in pos_list:
                    if existing.get("symbol") == base and existing.get("side") != direction:
                        logger.warning(
                            f"⛔ {symbol} 已有{existing.get('side')}持仓，"
                            f"allow_hedge=false → 拒绝开{direction}"
                        )
                        self.tlogger.log_filter_reject(symbol, "OPPOSITE_HELD",
                            f"已有{existing.get('side')}反向仓, allow_hedge=false",
                            direction=direction, strategy=strategy)
                        sig["_blocked"] = True
                        break
            if sig.get("_blocked"):
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
                    # v4.5: 不再单独检查 → 统一由下方后置校验处理

            # ── v4.5: 统一后置校验 — VaR/方向集中扣分后重新检查 required_score ──
            # 扫描阶段已通过门槛，但后续 VaR/方向集中可能再次扣分
            # 确保信号在最终下单前仍满足策略感知的 required_score
            _req = sig.get("required_score", 50)
            if sig.get("confidence", 50) < _req:
                logger.info(f"⛔ {symbol} 后置扣分后{sig['confidence']}<门槛{_req} → 跳过")
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
                # v4.0: AI 降级为研究层 — 信号评分达标直接开仓, AI 不拦路
                decision, reason = "CONFIRM", f"量化信号{tier}-直通"
                logger.info(f"⚡ {symbol} 量化直通: 评分{sig.get('confidence',50)} — 跳过AI审核")

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
                # v4.4: 同质化限制 — counter_trend 最多同时持有 N 个仓位
                if strategy == "counter_trend":
                    ct_count = sum(
                        1 for s in getattr(self, '_position_strategies', {}).values()
                        if s == "counter_trend"
                    )
                    if ct_count >= self.config.max_counter_trend_positions:
                        logger.info(
                            f"⛔ {symbol} counter_trend 同质化限制: "
                            f"已有{ct_count}个counter_trend仓位 → 拒绝"
                        )
                        self.tlogger.log_filter_reject(symbol, "CT_CLUSTER_LIMIT",
                            f"已有{ct_count}个counter_trend仓位, MAX={self.config.max_counter_trend_positions}",
                            direction=direction, strategy=strategy)
                        continue
                # v4.1: EMA-Kalman死锁降级的counter_trend → 半仓
                if strategy == "counter_trend":
                    pos_mult *= 0.5
                    logger.info(f"  🔒 counter_trend半仓: 仓位系数 {pos_mult:.2f}x")
                # ── v4.1: 币种级微调 (保守) ──
                base = symbol.replace("/USDT:USDT", "")
                coin_cap = getattr(self, '_coin_position_cap', {}).get(base, 1.0)
                if coin_cap < 1.0:
                    pos_mult *= coin_cap
                    logger.info(f"  🔒 {base} 仓位系数 {pos_mult:.2f}x")
                result = self.executor.execute(symbol, direction, price, atr, adx, strategy,
                                               position_multiplier=pos_mult,
                                               market_regime=market_regime,
                                               vol_cone_sl_mult=sig.get("_vol_cone_sl_mult", 1.0))
                # P0修复: TPSL fatal close → finalize 已发生的 round-trip trade
                if result.get("tpsl_fatal_close"):
                    sym_full = f"{symbol}/USDT:USDT"
                    entry_price = result.get("entry_price", price)
                    contracts = result.get("amount", 0)
                    pos_side = result.get("pos_side", "long" if direction == "LONG" else "short")
                    # 估算保证金 (使用 ADX 公式近似)
                    est_margin = entry_price * abs(contracts) * self.exchange.get_contract_size(sym_full) / self.config.leverage
                    logger.error(
                        f"📋 {symbol} TPSL致命失败 → 记录 round-trip 到 finalize: "
                        f"entry={entry_price:.4f} contracts={contracts} margin≈{est_margin:.2f}"
                    )
                    self._finalize_closed_position(
                        symbol=sym_full,
                        side="LONG" if pos_side == "long" else "SHORT",
                        entry=entry_price,
                        contracts=contracts,
                        margin=max(est_margin, 1.0),
                        upl=0,
                        close_reason="TPSL_FAILURE_PROTECTIVE_CLOSE",
                        open_ts=time.time(),
                        strategy=strategy,
                        close_order_result=result.get("close_order"),
                        mark_price=entry_price,
                    )
                    self.stats["ai_rejects"] += 1
                    continue
                if result["success"]:
                    self.total_trades += 1
                    trades_this_cycle += 1
                    self.stats["trades"] += 1
                    self._last_trade_ts = time.time()  # v4.0: 开仓间隔
                    # v4.5→Phase2: 手续费不再在开仓时记录到 cumulative_fees
                    # 平仓时从 Bitget API 获取实际手续费统一记录，避免双重计入
                    # 估算费仅保存在 result["total_fee"] 中供日志参考
                    # ── v3.6: 记录仓位开仓时间 ──
                    sym_full = f"{symbol}/USDT:USDT"
                    if sym_full not in self._position_open_times:
                        self._position_open_times[sym_full] = time.time()
                    # v4.1 fix: 标记本周期新开仓, 防止清理时误删
                    if not hasattr(self, '_this_cycle_opened'):
                        self._this_cycle_opened = set()
                    self._this_cycle_opened.add(sym_full)
                    # ── v4.1: 记录策略名 + 开仓特征 (平仓日志用) ──
                    if not hasattr(self, '_position_strategies'):
                        self._position_strategies = {}
                    self._position_strategies[sym_full] = strategy
                    if not hasattr(self, '_open_trade_factors'):
                        self._open_trade_factors = {}
                    # v4.3: 完整开仓快照 — EMA趋势/Kalman方向/RSI/ADX/时段/市场状态
                    ema50 = sig.get("_ema50", 0)
                    ema200 = sig.get("_ema200", 0)
                    if ema50 > ema200:
                        ema_trend = "BULL"
                    elif ema50 < ema200:
                        ema_trend = "BEAR"
                    else:
                        ema_trend = "NEUTRAL"
                    sl_price = result.get("sl", 0)
                    tp_parts = result.get("tp_info", [])
                    tp_price = tp_parts[0].get("price", 0) if tp_parts else 0
                    self._open_trade_factors[sym_full] = {
                        "confidence": sig.get("confidence", 50),
                        "strategy": strategy, "direction": direction,
                        "entry_price": sig.get("price", 0),
                        "sl": sl_price,
                        "tp": tp_price,
                        "ema": sig.get("ema", 0),
                        "rsi": sig.get("rsi", 0),
                        "adx": sig.get("adx", 0),
                        "ema_trend": ema_trend,
                        "_kalman_score": sig.get("_kalman_score", 0),
                        "_kalman_dir": sig.get("_kalman_dir", "flat"),
                        "_hurst_regime": sig.get("_hurst_regime", "random_walk"),
                        "session": SessionManager.get_session_label() if hasattr(SessionManager, 'get_session_label') else "UNKNOWN",
                        "market_regime": getattr(self, '_current_regime', 'unknown'),
                        "bonuses": sig.get("bonuses", []),
                    }
                    # v4.3: 开仓快照写 JSONL — 含完整入场上下文
                    try:
                        factors = self._open_trade_factors[sym_full]
                        self.tlogger.log_entry_snapshot(
                            symbol=symbol, direction=direction,
                            strategy=factors["strategy"], score=factors["confidence"],
                            entry_price=price, sl=sl_price, tp=tp_price,
                            ema=factors["ema"], rsi=factors["rsi"], adx=factors["adx"],
                            ema_trend=factors["ema_trend"],
                            kalman_dir=factors["_kalman_dir"],
                            kalman_score=factors["_kalman_score"],
                            session=factors["session"],
                            market_regime=factors["market_regime"],
                            bonuses=factors["bonuses"],
                            amount=int(result.get("amount", 0)),
                            rr_ratio=result.get("rr_ratio", 0),
                        )
                    except Exception as e:
                        logger.error(f"❌ ENTRY_SNAPSHOT 写入失败: {e}", exc_info=True)
                    # v4.1: 存储本周期开仓快照 (供 _prev_positions 合并)
                    if not hasattr(self, '_this_cycle_trades'):
                        self._this_cycle_trades = {}
                    self._this_cycle_trades[sym_full] = {
                        "entryPrice": price,
                        "markPrice": price,
                        "contracts": result.get("amount", 0),
                        "unrealizedPnl": 0,
                        "margin": result.get("margin_ratio_used", 0),
                        "info": {"holdSide": "long" if direction == "LONG" else "short"},
                    }
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
                        if regime != "range":
                            self.grid_manager.cancel_grid(sym)
                            logger.info(f"📋 市场不再震荡，取消 {sym} 网格")

        return trades_this_cycle

    def _phase_scan_and_filter(self, acct, cycle):
        """扫描候选信号 + 6层过滤。返回 {"blocked": bool, "candidates": list, "btc_change": float|None, "funding_rates": dict}"""
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
                            close_o = self.exchange.create_market_order_close(
                                sym_full, w_cts, w_side, w_hold
                            )
                            logger.info(f"🔄 平仓 {worst_symbol}: {close_o.get('id', '?') if close_o else 'CANCELLED'}")
                            rotated += 1
                            # v4.5→Phase2: 统一出口
                            w_entry = worst.get("entry_price", 0)
                            w_mark = worst.get("mark_price", 0)
                            w_margin = worst.get("margin", 0)
                            w_side2 = worst.get("side", "LONG")
                            self._finalize_closed_position(
                                symbol=sym_full, side=w_side2,
                                entry=w_entry, contracts=w_cts, margin=w_margin,
                                upl=worst_upl, close_reason="ROTATION_CLOSE",
                                open_ts=self._position_open_times.get(sym_full, 0),
                                strategy=getattr(self, '_position_strategies', {}).get(sym_full, "pullback"),
                                close_order_result=close_o, mark_price=w_mark,
                            )
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
            return {"blocked": True, "candidates": [], "btc_change": None, "funding_rates": {}}

        # ── v4.5: 截断移至BTC过滤和排名之后 —— 避免前N个被BTC滤掉后后续候选无法补位 ──
        # 旧位置在BTC过滤之前，导致排在后面的有效 pullback SHORT 被永久截掉

        # ── v2.7: BTC 联动过滤 (有候选信号时才查，空信号跳过省API) ──
        btc_change = None
        if candidates:
            btc_change = self.exchange.fetch_btc_change(self.config.btc_filter_timeframe)
        if btc_change is not None:
            logger.info(f"📊 BTC {self.config.btc_filter_timeframe} 涨跌幅: {btc_change*100:+.2f}%")

            # P3-B: wrapper → filter_pipeline.filter_btc_linkage
            btc_result = filter_btc_linkage(
                candidates, btc_change,
                self.config.btc_drop_block_long, self.config.btc_pump_block_short)
            candidates = btc_result["passed"]
            for blocked in btc_result["blocked"]:
                logger.warning(
                    f"⏭️  {blocked['symbol']} {blocked['direction']} "
                    f"因 BTC {'暴跌' if 'drop' in blocked['reason'] else '暴涨'} "
                    f"{btc_change*100:+.1f}% 被拦截"
                )
                self.tlogger.log_btc_filter(
                    blocked["symbol"], blocked["direction"], btc_change, blocked["reason"])
                self.stats["btc_filters"] += 1
            if btc_result["blocked"]:
                logger.info(
                    f"📊 BTC 过滤: {len(candidates) + len(btc_result['blocked'])} "
                    f"→ {len(candidates)} 个候选"
                )
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
                    logger.debug("⚠️  静默异常", exc_info=True)
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

                # v2.0: 用组合排名重新评分 (仅算组合维度得分, 不改 confidence)
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
                        # v4.5: 组合排名不再改写 confidence — 只影响仓位系数 (get_position_multiplier)
                        # 低排名减分会导致刚好过线的信号跌穿 required_score 门槛

                # v2.0: 组合摘要 (供 AI context)
                sym_bases = [s["symbol"] for s in candidates[:5]]
                port_summary = self.portfolio.get_portfolio_summary(sym_bases)
                logger.info(f"📊 {port_summary}")
            except Exception as e:
                logger.warning(f"⚠️  投资组合排名失败: {e}")

        # ── v4.5: 截断 —— 在BTC过滤和排名之后执行，避免前N候选被滤后无法补位 ──
        # 此时 candidates 已经过 BTC 过滤 + 组合排名重排，截断取前 max_new 个是公平的
        if len(candidates) > max_new:
            logger.info(f"⏳ 候选信号 {len(candidates)} > 可开仓 {max_new}，取前 {max_new} 个 "
                        f"(已通过BTC过滤+排名)")
            candidates = candidates[:max_new]

        # ── v2.1: 资金费率获取 ──
        funding_rates: Dict[str, Optional[float]] = {}
        for sig in candidates:
            symbol = sig["symbol"]
            funding_rates[symbol] = self.exchange.fetch_funding_rate(symbol)

        return {"blocked": False, "candidates": candidates, "btc_change": btc_change, "funding_rates": funding_rates}

    def _phase_trade_guard(self, acct, cycle):
        """交易守卫: 日损锁+周末保护+方向死锁。返回 {"blocked": bool, "reason": str}"""
        # 如果日内亏损已触发锁定，本轮只扫描不交易
        if not self.riskmon.can_trade_with_data(acct):
            # v4.5: 每小时只报一次，减少日志噪声
            now_ts = time.time()
            last_log = getattr(self, '_last_daily_loss_logged', 0)
            if now_ts - last_log > 3600:
                logger.error(f"🚨 日内亏损 -{abs(self.riskmon.daily_pnl_pct)*100:.1f}% > "
                             f"{self.config.daily_loss_limit*100:.0f}%，硬止损！")
                self._last_daily_loss_logged = now_ts
            # v4.5: DAILY_LOSS_CLOSE_ALL=true → 强制清仓, 不只是锁新仓
            if self.config.daily_loss_close_all and acct.get("positions_detail"):
                positions = acct["positions_detail"]
                logger.error(
                    f"🔥 日损硬止损 + DAILY_LOSS_CLOSE_ALL → "
                    f"强制平仓 {len(positions)} 个持仓"
                )
                closed_count = 0
                for pos in positions:
                    try:
                        sym = f"{pos['symbol']}/USDT:USDT"
                        pside = "long" if pos.get("side") == "LONG" else "short"
                        contracts = pos.get("contracts", 0)
                        if contracts <= 0:
                            continue
                        close_o = self.exchange.create_market_order_close(
                            sym, contracts, "buy" if pside == "long" else "sell",
                            pos_side=pside
                        )
                        if close_o:
                            closed_count += 1
                            # v4.5→Phase2: 统一出口
                            dl_side = pos.get("side", "LONG")
                            dl_entry = pos.get("entry_price", 0)
                            dl_mark = pos.get("mark_price", 0)
                            dl_margin = pos.get("margin", 0)
                            dl_upl = pos.get("unrealized_pnl", 0)
                            self._finalize_closed_position(
                                symbol=sym, side=dl_side,
                                entry=dl_entry, contracts=contracts, margin=dl_margin,
                                upl=dl_upl, close_reason="DAILY_LOSS_CLOSE",
                                open_ts=self._position_open_times.get(sym, 0),
                                strategy=getattr(self, '_position_strategies', {}).get(sym, "pullback"),
                                close_order_result=close_o, mark_price=dl_mark,
                                is_emergency=True,  # 减少重试
                            )
                            self.tlogger.log_risk("DAILY_LOSS_CLOSE",
                                f"{pos['symbol']} {pos['side']} "
                                f"强平 @{pos.get('mark_price',0):.4f} "
                                f"浮盈={pos.get('unrealized_pnl',0):+.2f}")
                            logger.warning(
                                f"🛑 日损清仓: {pos['symbol']} {pos['side']} "
                                f"@{pos.get('mark_price',0):.4f}"
                            )
                    except Exception as e:
                        logger.error(f"💥 日损清仓 {pos['symbol']} 失败: {e}")
                logger.error(
                    f"🔥 日损清仓完成: {closed_count}/{len(positions)} 个仓位已平"
                )
                # v4.5: 全平后复核 — 仍有仓位则重试
                remaining_positions = self.exchange.get_open_positions()
                if remaining_positions:
                    logger.error(
                        f"🔥 日损清仓复核: 仍有 {len(remaining_positions)} 个残留 → 重试"
                    )
                    # v4.5→Phase2: 内联重试循环 (替代 emergency_close_all), 接入统一出口
                    for rsym, rpos in remaining_positions.items():
                        try:
                            rct = abs(float(rpos.get("contracts", 0) or 0))
                            if rct <= 0:
                                continue
                            rh = rpos.get("info", {}).get("holdSide", "").lower()
                            if not rh:
                                rh = "short" if str(rpos.get("side", "")).upper() == "SHORT" else "long"
                            rs = "buy" if rh == "short" else "sell"
                            close_or = self.exchange.create_market_order_close(rsym, rct, rs, rh)
                            if close_or:
                                rside = "LONG" if rh == "long" else "SHORT"
                                self._finalize_closed_position(
                                    symbol=rsym, side=rside,
                                    entry=float(rpos.get("entryPrice", 0) or 0),
                                    contracts=rct,
                                    margin=float(rpos.get("initialMargin", 0) or 0),
                                    upl=float(rpos.get("unrealizedPnl", 0) or 0),
                                    close_reason="DAILY_LOSS_CLOSE",
                                    open_ts=self._position_open_times.get(rsym, 0),
                                    strategy=getattr(self, '_position_strategies', {}).get(rsym, "pullback"),
                                    close_order_result=close_or,
                                    mark_price=float(rpos.get("markPrice", 0) or 0),
                                    is_emergency=True,
                                )
                        except Exception:
                            pass
                    final_check = self.exchange.get_open_positions()
                    if final_check:
                        for sym in list(final_check.keys()):
                            self.tlogger.log_risk("DAILY_LOSS_FAILED",
                                f"日损清仓失败残留: {sym}")
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
            self._audit_snapshot(acct)
            return {"blocked": True, "reason": "daily_loss"}

        # ── v4.1: 周末凌晨保护 —— 低流动性时段只平仓不开仓 ──
        session_live = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)
        if session_live.get("name") == "weekend_dead":
            logger.info(f"🛑 周末凌晨({session_live['label']})低流动性，本轮只平仓不开仓")
            # P0: 检测 TPSL 触发平仓
            self._detect_disappeared_positions(acct.get("positions_detail", []))
            # 仍然执行持仓检查 (规则引擎平仓，AI决策层已禁用)
            if self.config.rule_exit_review_enabled and acct.get("positions_detail"):
                if self.total_scans > 0 and self.total_scans % self.config.rule_exit_review_interval == 0:
                    self._review_open_positions(acct["positions_detail"])
            # 扫描生成信号但不执行
            candidates = self.scan_all()
            if candidates:
                logger.info(f"📊 周末凌晨信号: {len(candidates)} 个（不执行，仅记录）")
                for sig in candidates:
                    self.tlogger.log_signal(sig)
            all_time = self.riskmon.current_equity - self.riskmon.initial_equity
            self.tlogger.log_cycle(cycle, len(candidates), 0,
                                    self.riskmon.current_equity,
                                    self.riskmon.daily_pnl_pct,
                                    self.riskmon.cumulative_fees,
                                    "weekend_dead", 0, 0,
                                    self.riskmon.initial_equity, all_time)
            # 执行网格策略 (低波动适用)
            if self.grid_manager:
                self.grid_manager.poll_grids()
            self._audit_snapshot(acct)
            return {"blocked": True, "reason": "weekend"}

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

        return {"blocked": False, "reason": "ok"}

    def _phase_position_protect(self, acct, session):
        """持仓保护: 移动止损+锁仓+浮亏止损+僵尸仓退出+波动熔断+规则审核+订单清理+TPSL检查"""
        # ── v3.2: 移动止损检查 ──
        if self.trailing_sl and acct.get("positions_detail"):
            for pos_d in acct["positions_detail"]:
                sym_full = f"{pos_d['symbol']}/USDT:USDT"
                side = pos_d["side"]
                mark = pos_d["mark_price"]
                entry = pos_d["entry_price"]
                # 获取当前 SL (v4.5→Phase2: 修复 source-of-truth — 优先 position.info 而非 fetch_open_orders)
                try:
                    # Layer 1: 从 positions_detail 读取 (info.stopLoss, 已在 get_account_summary 提取)
                    current_sl = float(pos_d.get("sl_price", 0) or 0)
                    # Layer 1b: positions_detail 无数据 → 直接查缓存的原始持仓
                    if current_sl <= 0:
                        try:
                            raw_positions = self.exchange.get_open_positions()
                            raw_pos = raw_positions.get(sym_full, {})
                            raw_info = raw_pos.get("info", {})
                            sl_raw = raw_info.get("stopLoss", raw_info.get("stopLossPrice", ""))
                            if sl_raw and str(sl_raw) not in ("0", "", "None"):
                                current_sl = float(sl_raw)
                        except Exception:
                            pass
                    # Layer 2: 兜底 — 独立计划单 (非 pos-tpsl 场景, 如 _set_sl_tp_via_plan_orders 降级)
                    if current_sl <= 0:
                        stop_ords = self.exchange.exchange.fetch_open_orders(
                            sym_full, params={"stop": True}
                        )
                        for o in (stop_ords or []):
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
                                # v4.5→Phase2: 优先从持仓 info 读取 (pos-tpsl 已设)
                                existing_tp = float(pos_d.get("tp_price", 0) or 0)
                                # Layer 1b: positions_detail 无数据 → 直接查缓存的原始持仓
                                if existing_tp <= 0:
                                    try:
                                        raw_positions = self.exchange.get_open_positions()
                                        raw_pos = raw_positions.get(sym_full, {})
                                        raw_info = raw_pos.get("info", {})
                                        tp_raw = raw_info.get("takeProfit", raw_info.get("takeProfitPrice", ""))
                                        if tp_raw and str(tp_raw) not in ("0", "", "None"):
                                            existing_tp = float(tp_raw)
                                    except Exception:
                                        pass
                                # Layer 2: 兜底 — 独立计划单 (非 pos-tpsl 降级场景)
                                if existing_tp <= 0:
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
                                        logger.debug("⚠️  静默异常", exc_info=True)
                                # v4.5: TP仍为0 → 跳过更新, 避免传无效TP破坏现有保护
                                if existing_tp <= 0:
                                    logger.warning(
                                        f"⚠️  {pos_d['symbol']} 移动止损跳过: 无法获取现有TP"
                                    )
                                else:
                                    self.exchange.set_position_sl_tp(
                                        sym_full, side.lower(),
                                        new_sl, existing_tp
                                    )
                        except Exception:
                            logger.debug("⚠️  静默异常", exc_info=True)
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)

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
                        _needs_cleanup = len(reduce_orders) >= 8
                        if _needs_cleanup:
                            logger.warning(f"🔒 {pos_d['symbol']} SL/TP堆积({len(reduce_orders)}个)")
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
                            # v4.5: 先设新保护，成功后再清理旧单 — 防止"取消后设失败"裸仓
                            tpsl_ok = self.exchange.set_position_sl_tp(sym_full, side.lower(), lock_sl, lock_tp)
                            if tpsl_ok and _needs_cleanup:
                                for o in reduce_orders:
                                    try:
                                        self.exchange.cancel_order(str(o.get('id','')), sym_full)
                                    except Exception:
                                        pass
                                logger.info(f"🔒 {pos_d['symbol']} 旧SL/TP已清理 {len(reduce_orders)}个")
                            elif not tpsl_ok:
                                logger.error(f"🔒 {pos_d['symbol']} 锁仓TPSL设置失败 → 保留旧保护")
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
                logger.debug("⚠️  静默异常", exc_info=True)

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
                    # v4.5: 本地开仓时间缺失 (重启/外部开仓/状态恢复失败)
                    # 尝试从交易所持仓数据获取, 拿不到则保守处理: 视为已超 auto_sl_min_hours
                    try:
                        raw_positions = self.exchange.get_open_positions()
                        raw = raw_positions.get(sym_full, {})
                        pos_ts = raw.get("timestamp") or raw.get("datetime")
                        if pos_ts:
                            if isinstance(pos_ts, str):
                                from datetime import datetime as dt
                                pos_ts = dt.fromisoformat(pos_ts.replace("Z", "+00:00")).timestamp()
                            open_ts = float(pos_ts) / 1000.0 if float(pos_ts) > 1e12 else float(pos_ts)
                            # 补充到本地记录
                            self._position_open_times[sym_full] = open_ts
                            logger.info(f"📌 {pos_d['symbol']} 从交易所恢复开仓时间")
                    except Exception:
                        pass
                if not open_ts:
                    # 保守默认: 视为长持仓, 允许 AUTO_SL 生效
                    holding_hours = self.config.auto_sl_min_hours + 0.1
                    logger.warning(
                        f"⚠️  {pos_d['symbol']} 开仓时间未知 → 保守处理 "
                        f"(holding_hours={holding_hours:.1f}h, 允许AUTO_SL)"
                    )
                else:
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
                    close_side = "buy" if side == "SHORT" else "sell"
                    pos_side = "short" if side == "SHORT" else "long"
                    close_o = self.exchange.create_market_order_close(
                        sym_full, contracts, close_side, pos_side
                    )
                    if close_o:
                        logger.info(f"✅ 浮亏止损平仓 {sym}: {close_o.get('id', '?')}")
                        # v4.5→Phase2: 统一出口
                        margin = pos_d.get("margin", 0)
                        self._finalize_closed_position(
                            symbol=sym_full, side=side,
                            entry=entry, contracts=contracts, margin=margin,
                            upl=upl, close_reason="AUTO_SL",
                            open_ts=open_ts,
                            strategy=getattr(self, '_position_strategies', {}).get(sym_full, "pullback"),
                            close_order_result=close_o, mark_price=mark,
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
                        # v4.5→Phase2: 统一出口
                        entry = pos_d.get("entry_price", 0)
                        mark = pos_d.get("mark_price", 0)
                        self._finalize_closed_position(
                            symbol=sym_full, side=side,
                            entry=entry, contracts=contracts, margin=margin,
                            upl=upl, close_reason="STALE_EXIT",
                            open_ts=open_ts,
                            strategy=getattr(self, '_position_strategies', {}).get(sym_full, "pullback"),
                            close_order_result=close_o, mark_price=mark,
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
                                sym_full, side.lower(),
                                tight_sl, tp_price)
                            self.tlogger.log_risk(
                                "VOL_SPIKE", f"{sym} ATR {spike_ratio:.1f}x → SL收紧")
                        except Exception as e:
                            logger.warning(f"⚡ {sym} 波动熔断更新SL失败: {e}")
                except Exception:
                    logger.debug("⚠️  静默异常", exc_info=True)  # OI/ATR 获取失败不影响主循环

        # P0: 检测 TPSL 触发平仓
        self._detect_disappeared_positions(acct.get("positions_detail", []))
        # ── v4.5→Phase2: 规则引擎持仓审核 (AI决策层已永久禁用, 无需断路器) ──
        if self.config.rule_exit_review_enabled and acct.get("positions_detail"):
            # 按 interval 间隔执行 (默认每3轮=15分钟)
            if self.total_scans > 0 and self.total_scans % self.config.rule_exit_review_interval == 0:
                self._review_open_positions(acct["positions_detail"])

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
            logger.debug("⚠️  静默异常", exc_info=True)

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
                        self.exchange.set_position_sl_tp(sym_full, side.lower(), round(sl_p,4), round(tp_p,4))
                if bare == 0 and acct.get("positions_detail"):
                    logger.debug("🛡️  SL健康检查: 全部持仓已保护")
            except Exception as e:
                logger.warning(f"⚠️  SL健康检查异常: {e}")

    def _phase_emergency_guard(self, acct, cycle):
        """紧急停止检查 + 市场刷新 + 风控状态。返回 {"blocked": bool, "session": dict}"""
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
                _is_dry_run = (getattr(self.config, 'emergency_dry_run', False)
                               and self.exchange.is_sandbox())
                closed = 0
                # v4.5→Phase2: 紧急平仓逐个调用统一出口
                for sym, pos in positions.items():
                    try:
                        contracts = abs(float(pos.get("contracts", 0) or 0))
                        if contracts <= 0:
                            continue
                        hold_side = pos.get("info", {}).get("holdSide", "").lower()
                        if not hold_side:
                            hold_side = "short" if str(pos.get("side", "")).upper() == "SHORT" else "long"
                        w_side = "buy" if hold_side == "short" else "sell"
                        close_o = self.exchange.create_market_order_close(
                            sym, contracts, w_side, hold_side)
                        if close_o:
                            closed += 1
                            em_side = "LONG" if hold_side == "long" else "SHORT"
                            em_entry = float(pos.get("entryPrice", 0) or 0)
                            em_mark = float(pos.get("markPrice", 0) or 0)
                            em_margin = float(pos.get("initialMargin", 0) or 0)
                            em_upl = float(pos.get("unrealizedPnl", 0) or 0)
                            self._finalize_closed_position(
                                symbol=sym, side=em_side,
                                entry=em_entry, contracts=contracts, margin=em_margin,
                                upl=em_upl, close_reason="EMERGENCY_STOP",
                                open_ts=self._position_open_times.get(sym, 0),
                                strategy=getattr(self, '_position_strategies', {}).get(sym, "pullback"),
                                close_order_result=close_o, mark_price=em_mark,
                                is_emergency=True,
                            )
                    except Exception as e:
                        logger.error(f"❌ 紧急平仓失败 {sym}: {e}")
                self.tlogger.log_risk("EMERGENCY_STOP", f"已平仓 {closed} 个持仓: {reason}")
                # v4.5: 全平后复核 — 仍有仓位则重试
                if not _is_dry_run:
                    for retry in range(2):
                        remaining = self.exchange.get_open_positions()
                        if not remaining:
                            break
                        logger.error(
                            f"🔥 紧急平仓复核: 仍有 {len(remaining)} 个残留仓位 "
                            f"{list(remaining.keys())} → 第{retry+1}次重试"
                        )
                        for sym2 in list(remaining.keys()):
                            self.tlogger.log_risk("EMERGENCY_REMAINING",
                                f"残留仓位: {sym2} 第{retry+1}次重试")
                            try:
                                rp = remaining[sym2]
                                rc = abs(float(rp.get("contracts", 0) or 0))
                                rh = rp.get("info", {}).get("holdSide", "").lower()
                                if not rh:
                                    rh = "short" if str(rp.get("side", "")).upper() == "SHORT" else "long"
                                rs = "buy" if rh == "short" else "sell"
                                close_o2 = self.exchange.create_market_order_close(sym2, rc, rs, rh)
                                if close_o2:
                                    # v4.5→Phase2: 紧急重试也接入统一出口
                                    em_side2 = "LONG" if rh == "long" else "SHORT"
                                    em_entry2 = float(rp.get("entryPrice", 0) or 0)
                                    em_mark2 = float(rp.get("markPrice", 0) or 0)
                                    em_margin2 = float(rp.get("initialMargin", 0) or 0)
                                    em_upl2 = float(rp.get("unrealizedPnl", 0) or 0)
                                    self._finalize_closed_position(
                                        symbol=sym2, side=em_side2,
                                        entry=em_entry2, contracts=rc, margin=em_margin2,
                                        upl=em_upl2, close_reason="EMERGENCY_STOP",
                                        open_ts=self._position_open_times.get(sym2, 0),
                                        strategy=getattr(self, '_position_strategies', {}).get(sym2, "pullback"),
                                        close_order_result=close_o2, mark_price=em_mark2,
                                        is_emergency=True,
                                    )
                            except Exception:
                                pass
                    # 最终确认
                    final_remaining = self.exchange.get_open_positions()
                    if final_remaining:
                        logger.error(
                            f"💀 紧急平仓未完全清除: {list(final_remaining.keys())} "
                            f"— 3次尝试后仍残留!"
                        )
                        for sym3 in list(final_remaining.keys()):
                            self.tlogger.log_risk("EMERGENCY_FAILED",
                                f"3次平仓失败: {sym3}")
                self.riskmon.cooldown_until = time.time() + 86400  # 紧急停止: 24h 冷却
                self.riskmon.cooldown_reason = "紧急停止"
                return {"blocked": True, "session": {}}

        logger.info(f"\n{'─' * 50}")
        session = SessionManager.get_session(
            self.config.adx_threshold, self.config.vol_ratio_threshold)

        # ── v2.3: 每轮刷新市场情绪 ──
        try:
            self.market_ctx.ensure_fresh()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

        # ── v2.7: 用预取数据刷新风控，避免重复 API 调用 ──
        self.riskmon._refresh_with_data(acct)
        risk_status = self.riskmon.get_status()
        logger.info(f"🔄 第 {cycle} 轮扫描 — {now_str('%H:%M:%S')} | {session['label']}")
        logger.info(f"📊 日内风控: PnL={risk_status['pnl_pct']:+.2f}% | "
                     f"权益={risk_status['current_equity']:.2f} | "
                     f"累计手续费={risk_status['cumulative_fees']:.4f} | "
                     f"{'🔒锁定' if risk_status['blocked'] else '🟢正常'}")
        logger.info(f"{'─' * 50}")

        return {"blocked": False, "session": session}

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

        # 1. 紧急停止 + 市场刷新
        guard = self._phase_emergency_guard(acct, cycle)
        if guard["blocked"]:
            return
        session = guard["session"]

        # 2. 持仓保护
        self._phase_position_protect(acct, session)

        # 3. 交易守卫 (日损锁/周末/死锁)
        trade_guard = self._phase_trade_guard(acct, cycle)
        if trade_guard["blocked"]:
            return

        # 4. 扫描 + 过滤
        scan = self._phase_scan_and_filter(acct, cycle)
        if scan["blocked"]:
            return
        candidates = scan["candidates"]
        btc_change = scan["btc_change"]
        funding_rates = scan["funding_rates"]

        # 5. 执行交易
        trades_this_cycle = self._phase_execute(candidates, acct, btc_change, funding_rates, cycle)

        # 6. 后处理
        self._phase_post_cycle(candidates, acct, cycle, trades_this_cycle)

        # v4.3: 每周期过滤器统计
        filter_counts = self.tlogger.get_filter_counts()
        if filter_counts:
            self.tlogger.log_filter_stats(cycle, filter_counts,
                current_regime=self._regime_current)

    def shutdown(self):
        """优雅退出: 写最终状态、关闭连接、记录停止事件"""
        logger.info("🛑 正在关闭 …")
        # ── v4.1: 持久化审计记录 ──
        try:
            self.equity_auditor.save()
        except Exception:
            pass
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
                logger.debug("⚠️  静默异常", exc_info=True)
        # ── v3.1: 关闭学习引擎 ──
        if self.learner:
            try:
                self.learner.close()
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)
        try:
            self._update_status_file()
            self.tlogger.log_risk("SHUTDOWN", "bot stopped gracefully")
            logger.info("✅ 最终状态已保存")
        except Exception as e:
            logger.error(f"⚠️  关闭时保存状态失败: {e}")
        try:
            self.analyst.close()
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)
        logger.info("👋 DeepSeekQuantBot v4.5 已停止")

    def _detect_startup_closes(self):
        """v3.4: 启动时对比持久化仓位快照，检测停机期间的平仓"""
        state_file = "data/positions_state.json"
        if not os.path.exists(state_file):
            return
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                prev = json.load(f)
            # v4.5: 仅恢复干旱计数器, 不再跨重启累计 PnL
            # PnL 累计跨重启导致审计偏差 (bot预期 vs 实际不一致)
            audit_state = prev.get("audit", {})
            if audit_state:
                # v4.1: 恢复干旱计数器，避免重启后 ADX 阈值回弹
                saved_drought = audit_state.get("drought_cycles", 0)
                if saved_drought > 0:
                    self._drought_cycles = saved_drought
                    logger.info(f"🌵 干旱计数器恢复: {saved_drought} 轮")
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
                    pv = entry * contracts * self.exchange.get_contract_size(sym)
                    margin = float(old_pos.get("margin", pv / self.config.leverage))

                    # v4.5→Phase2: 统一出口 (启动同步 — 无 API 数据，依赖回退)
                    self._finalize_closed_position(
                        symbol=sym, side=side,
                        entry=entry, contracts=contracts, margin=margin,
                        upl=float(old_pos.get("unrealizedPnl", 0) or 0),
                        close_reason="STARTUP_SYNC",
                        open_ts=0,  # 启动时无准确开仓时间
                        strategy=getattr(self, '_position_strategies', {}).get(sym, "pullback"),
                        close_order_result=None, mark_price=mark,
                    )
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
                side = "long" if str(pos_side_raw).lower() in ("long", "buy") else "short"
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
                sl_price = entry + 1.5 * atr if side == "short" else entry - 1.5 * atr
                tp_price = entry - 2.0 * atr if side == "short" else entry + 2.0 * atr
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

        logger.info("\n🎯 DeepSeekQuantBot v4.5 进入主循环 (每 5 分钟)")
        logger.info("🧠 AI决策层: 已禁用 (v4.1+ 永久禁用, 出场使用规则引擎)")
        logger.info("按 Ctrl+C 停止\n")

        # ── v3.4: 检测停机期间的平仓 ──
        # v4.5→Phase2: 先恢复去重键防止崩溃后重复处理
        self._restore_closed_keys()
        self._detect_startup_closes()

        # ── v3.3: 启动时为所有存量仓位补挂 SL/TP ──
        self._ensure_all_positions_protected()

        # 注册 SIGTERM 信号处理 (容器优雅关闭)
        def _handle_signal(signum, _frame):
            logger.info(f"\n📡 收到信号 {signum}，优雅退出 …")
            self.shutdown()
            sys.exit(0)
        try:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)  # 某些平台不支持 signal

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
                    logger.debug("⚠️  静默异常", exc_info=True)
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
