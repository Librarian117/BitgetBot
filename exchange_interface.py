#!/usr/bin/env python3
"""exchange_interface.py — Bitget 交易所封装层"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import pandas as pd

from config_manager import ConfigManager

logger = logging.getLogger("QuantBot")

class ExchangeInterface:
    """Bitget 交易所封装层 (Classic + UTA 双路径)"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.exchange: ccxt.Exchange = self._build_exchange()
        self._trading_fees: Dict[str, float] = {}  # unified symbol -> taker fee
        self._tpsl_cache: Dict[Tuple[str, str, int], Tuple[float, float, float]] = {}  # v4.5: {(symbol, side, entry_int): (sl, tp, ts)} 含方向+entry+TTL
        self._is_uta = not self.config.is_sandbox  # UTA V3 path for live, Classic for sandbox
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
        # ★ v4.5→Phase2: 沙箱模式由 config 控制（BITGET_SANDBOX=true/false）
        ex.set_sandbox_mode(self.config.is_sandbox)
        return ex

    # ── UTA V3 写路径辅助方法 ──────────────────────────────────────
    def _uta_v3_create_market_order(self, symbol: str, side: str, amount: float,
                                     pos_side: str, trade_side: str = "open") -> Optional[Dict]:
        """UTA V3: POST /api/v3/trade/place-order → 市价单

        side: "buy"|"sell"
        pos_side: "long"|"short"
        trade_side: "open"|"close"
        amount: 合约张数（由 trade_executor 计算，已对齐最小下单量）
        """
        # ── 最小下单量保护 ──
        min_qty = self.get_min_amount(symbol)
        if amount < min_qty:
            logger.warning(
                f"⛔ SKIP_BELOW_MIN_ORDER {symbol}: qty={amount} < min={min_qty} → 跳过，不发送 POST"
            )
            return None

        raw_symbol = symbol.split(":")[0].replace("/", "")
        params = {
            "symbol": raw_symbol,
            "category": "USDT-FUTURES",
            "marginCoin": "USDT",
            "side": side,
            "posSide": pos_side,
            "orderType": "market",
            "qty": str(amount),
            "tradeSide": trade_side,
        }
        try:
            resp = self.exchange.private_uta_post_v3_trade_place_order(params)
            if resp.get("code") == "00000":
                data = resp.get("data", {})
                return {
                    "id": data.get("orderId", ""),
                    "price": float(data.get("fillPrice", 0) or 0),
                    "average": float(data.get("fillPrice", 0) or 0),
                    "amount": amount,
                    "side": side,
                    "symbol": symbol,
                    "info": data,
                }
            logger.error(f"❌ UTA V3 市价单失败: code={resp.get('code')} msg={resp.get('msg')}")
            return None
        except Exception as e:
            logger.error(f"❌ UTA V3 市价单异常: {e}")
            return None

    def _uta_v3_place_strategy_order(self, symbol: str, plan_type: str,
                                      trigger_price: float, hold_side: str,
                                      execute_price: float = None) -> bool:
        """UTA V3: POST /api/v3/trade/place-strategy-order → 止损/止盈计划单

        plan_type: "pos_loss"|"pos_profit"
        hold_side: "long"|"short"
        """
        raw_symbol = symbol.split(":")[0].replace("/", "")
        if execute_price is None:
            execute_price = trigger_price

        try:
            tp_str = self.exchange.price_to_precision(symbol, trigger_price)
            ep_str = self.exchange.price_to_precision(symbol, execute_price)
        except Exception:
            try:
                market = self.exchange.market(symbol)
                precision = market.get("precision", {}).get("price", 0.01)
                if precision < 1:
                    import math
                    decimals = max(0, int(-math.log10(precision)))
                else:
                    decimals = 2
            except Exception:
                decimals = 2
            tp_str = str(round(trigger_price, decimals))
            ep_str = str(round(execute_price, decimals))

        params = {
            "symbol": raw_symbol,
            "category": "USDT-FUTURES",
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": tp_str,
            "triggerType": "mark_price",
            "executePrice": ep_str,
            "holdSide": hold_side,
        }
        try:
            resp = self.exchange.private_uta_post_v3_trade_place_strategy_order(params)
            code = resp.get("code", "")
            if code == "00000":
                label = "SL" if plan_type == "pos_loss" else "TP"
                logger.info(f"🛡️  UTA V3 {label} 计划单 OK: {symbol} @ {tp_str}")
                return True
            logger.warning(f"⚠️ UTA V3 {plan_type} 计划单失败: code={code} msg={resp.get('msg')}")
            return False
        except Exception as e:
            logger.error(f"❌ UTA V3 {plan_type} 计划单异常: {e}")
            return False

    def _uta_v3_close_positions(self, symbol: str, pos_side: str) -> Optional[Dict]:
        """UTA V3: POST /api/v3/trade/close-positions → 一键平仓

        pos_side: "long"|"short"
        """
        raw_symbol = symbol.split(":")[0].replace("/", "")
        params = {
            "category": "USDT-FUTURES",
            "symbol": raw_symbol,
            "posSide": pos_side,
            "marginCoin": "USDT",
        }
        try:
            resp = self.exchange.private_uta_post_v3_trade_close_positions(params)
            if resp.get("code") == "00000":
                logger.info(f"📦 UTA V3 平仓 OK: {symbol} posSide={pos_side}")
                return {"id": resp.get("data", {}).get("orderId", ""), "info": resp.get("data", {}),
                        "note": "UTA V3 close positions"}
            # Already closed
            if "40757" in str(resp.get("msg", "")) or "Not enough" in str(resp.get("msg", "")):
                logger.info(f"📦 {symbol} 仓位已不存在")
                return {"id": "already_closed", "note": "position already closed"}
            logger.error(f"❌ UTA V3 平仓失败: code={resp.get('code')} msg={resp.get('msg')}")
            return None
        except Exception as e:
            err_str = str(e)
            if "40757" in err_str or "Not enough position" in err_str or "position is not available" in err_str:
                logger.info(f"📦 {symbol} 仓位已不存在")
                return {"id": "already_closed", "note": "position already closed by pos-tpsl"}
            logger.error(f"❌ UTA V3 平仓异常: {e}")
            return None

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
            if self._is_uta:
                # UTA V3: /api/v3/account/assets
                resp = self.exchange.private_uta_get_v3_account_assets({})
                if resp.get("code") == "00000":
                    data = resp.get("data", {})
                    total = float(data.get("usdtEquity", 0) or 0)
                    assets = data.get("assets", [])
                    free = total
                    used = 0.0
                    for a in assets:
                        if a.get("coin") == "USDT":
                            free = float(a.get("available", 0) or 0)
                            locked = float(a.get("locked", 0) or 0)
                            used = locked
                            break
                    result = {"free": free, "total": total, "used": used}
                else:
                    raise RuntimeError(f"UTA balance failed: code={resp.get('code')} msg={resp.get('msg')}")
            else:
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
            # v4.5: 沙箱允许模拟余额，实盘必须失败
            if self.config.is_sandbox:
                return {"free": 10000.0, "total": 10000.0, "used": 0.0}
            raise

    def fetch_usdt_balance(self) -> float:
        """查询 USDT 可用余额（带缓存，沙箱降级：失败时返回模拟余额）"""
        try:
            bal = self._fetch_balance_dict()
            free = bal["free"]
            logger.info(f"💰 USDT 可用余额: {free:.2f}")
            return free
        except Exception as e:
            if self.config.is_sandbox:
                logger.warning(f"⚠️  余额查询不可用 ({str(e)[:120]})，使用模拟余额 10000 USDT")
                return 10000.0
            raise

    def fetch_total_balance(self) -> float:
        """查询 USDT 总余额 (带缓存，free + used)"""
        try:
            return self._fetch_balance_dict()["total"]
        except Exception:
            if self.config.is_sandbox:
                return self.fetch_usdt_balance()
            raise

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

            # v4.5→Phase2: 提取 SL/TP 从持仓 info (place-pos-tpsl 设置后 API 可见)
            # 热路径不再依赖 fetch_open_orders(stop=True) 判断 TPSL 是否存在
            raw_info = pos.get("info", {})
            sl_raw = raw_info.get("stopLoss", raw_info.get("stopLossPrice", ""))
            tp_raw = raw_info.get("takeProfit", raw_info.get("takeProfitPrice", ""))
            try:
                sl_price = float(sl_raw) if sl_raw and str(sl_raw) not in ("0", "", "None") else 0.0
            except (ValueError, TypeError):
                sl_price = 0.0
            try:
                tp_price = float(tp_raw) if tp_raw and str(tp_raw) not in ("0", "", "None") else 0.0
            except (ValueError, TypeError):
                tp_price = 0.0

            pos_list.append({
                "symbol": sym.replace("/USDT:USDT", ""),
                "side": side,
                "contracts": abs(contracts),
                "entry_price": round(entry, 4),
                "mark_price": round(mark, 4),
                "unrealized_pnl": round(upnl, 4),
                "margin": round(margin, 4),
                "sl_price": round(sl_price, 4) if sl_price > 0 else 0.0,
                "tp_price": round(tp_price, 4) if tp_price > 0 else 0.0,
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
        """返回当前持仓字典 (v2.7: 批量查询 + UTA V3)"""
        cache_key = "positions"
        if self._cache_fresh(cache_key, 30):
            cached = self._cache_get(cache_key)
            if isinstance(cached, dict):
                return cached

        positions: Dict[str, Any] = {}
        api_ok = False

        if self._is_uta:
            # UTA V3: /api/v3/position/current-position
            try:
                resp = self.exchange.private_uta_get_v3_position_current_position(
                    {"category": "USDT-FUTURES"})
                if resp.get("code") == "00000":
                    api_ok = True
                    raw_data = resp.get("data", []) or []
                    # UTA returns dict (0 pos) or list (1+ pos)
                    pos_list = raw_data if isinstance(raw_data, list) else []
                    for p in pos_list:
                        # Normalize UTA V3 fields to Classic format
                        contracts = float(p.get("available", 0) or 0)
                        if contracts == 0:
                            continue
                        sym_raw = p.get("symbol", "")
                        sym = sym_raw  # UTA returns e.g. "BTCUSDT"
                        # Convert "BTCUSDT" → "BTC/USDT:USDT" if needed
                        if "/" not in sym:
                            for s in self.config.SYMBOLS:
                                if s.replace("/USDT:USDT", "").replace("/", "") == sym_raw:
                                    sym = s
                                    break
                        pos_side = p.get("posSide", p.get("holdSide", "long"))
                        positions[sym] = {
                            "symbol": sym,
                            "contracts": contracts,
                            "unrealizedPnl": float(p.get("unrealizedPL", p.get("unrealizedPnl", 0)) or 0),
                            "initialMargin": float(p.get("margin", p.get("imr", 0)) or 0),
                            "entryPrice": float(p.get("openPrice", p.get("avgOpenPrice", p.get("entryPrice", 0))) or 0),
                            "markPrice": float(p.get("markPrice", 0) or 0),
                            "side": pos_side,
                            "info": {
                                "holdSide": pos_side,
                                "stopLoss": p.get("stopLossPrice", "0"),
                                "takeProfit": p.get("takeProfitPrice", "0"),
                            },
                        }
            except Exception:
                logger.warning("UTA positions failed", exc_info=True)
        else:
            # Classic V2 path
            try:
                all_positions = self.exchange.fetch_positions(self.config.SYMBOLS)
                api_ok = True
                if all_positions:
                    for p in all_positions:
                        sym = p.get("symbol", "")
                        if sym and float(p.get("contracts", 0) or 0) != 0:
                            positions[sym] = p
            except Exception:
                for sym in self.config.SYMBOLS:
                    try:
                        pos = self.exchange.fetch_position(sym)
                        api_ok = True
                        if pos and float(pos.get("contracts", 0) or 0) != 0:
                            positions[sym] = pos
                    except Exception:
                        logger.warning("single position fetch failed", exc_info=True)

        if api_ok:
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
            logger.debug("⚠️  静默异常", exc_info=True)
        return 0.0006  # 默认 0.06%

    def create_market_order_with_partial_tp(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl_price: float,
        tp_parts: List[Tuple[float, float]],
        pos_side: str = "",  # v4.5: 显式持仓方向 "long"/"short", 空字符串则从side推导
    ) -> Optional[Dict]:
        """
        下单市价单 + 止损 + 部分止盈 (v2)

        tp_parts: [(tp_price, amount_ratio), ...]
          - 第一批 TP 附带在主单上（原子化）
          - 后续 TP 作为独立 reduceOnly 限价单

        v4.5: side 用于 ccxt (buy/sell), pos_side 用于 TPSL 方向 (long/short)
        """
        # v4.5: 清除上一轮的 TPSL 致命标志 (防止跨周期残留，在所有return之前)
        self._tpsl_fatal = None

        if not tp_parts:
            logger.error(f"❌ tp_parts 为空 {symbol}")
            return None

        try:
            # v4.5: 推导持仓方向，不再把 buy/sell 传给 TPSL 方法
            _pos_side = pos_side if pos_side else ("short" if side in ("sell", "SHORT") else "long")
            logger.info(
                f"🔔 下单(分步SL/TP): {symbol} {side.upper()} | "
                f"持仓方向={_pos_side} | 数量={amount}张 | SL={sl_price:.4f}"
            )

            # ── v3.6 重构: 市价开仓 + place-pos-tpsl 一次设 SL/TP ──

            # 1. 下市价单（纯开仓）
            if self._is_uta:
                # UTA V3: POST /api/v3/trade/place-order
                order = self._uta_v3_create_market_order(
                    symbol=symbol, side=side, amount=amount,
                    pos_side=_pos_side, trade_side="open")
            else:
                # Classic V2: ccxt unified create_order
                order = self.exchange.create_order(
                    symbol=symbol, type="market", side=side, amount=amount,
                    params={'tradeSide': 'open', 'marginMode': 'crossed'},
                )
            if not order:
                logger.error(f"❌ {symbol} 市价单失败")
                return None
            logger.info(f"✅ 主单成交: {symbol} | ID={order.get('id', 'N/A')}")

            # 2. 用 place-pos-tpsl API 一次设 SL+TP (自动替换旧单，不累积)
            # 从成交单获取实际入场价
            fill_price = float(order.get("price") or order.get("average") or sl_price)
            if fill_price <= 0:
                fill_price = sl_price
            # v4.0: 使用 tp_parts 中的第一个 TP 价格 (而非硬编码 2%)
            if tp_parts and len(tp_parts) > 0:
                tp_price = tp_parts[0][0]
                # v4.5: TP 方向校验基于持仓方向 (不再是 side=="SHORT" 这种永不命中的分支)
                if _pos_side == "short" and tp_price >= fill_price:
                    tp_price = fill_price * 0.98  # 做空TP必须 < 入场价
                    logger.warning(f"⚠️  TP方向修正: SHORT TP>=入场 → {tp_price:.4f}")
                elif _pos_side == "long" and tp_price <= fill_price:
                    tp_price = fill_price * 1.02  # 做多TP必须 > 入场价
                    logger.warning(f"⚠️  TP方向修正: LONG TP<=入场 → {tp_price:.4f}")
            else:
                # 无 tp_parts 时的回退
                tp_price = fill_price * 0.98 if _pos_side == "short" else fill_price * 1.02
            # v4.5: 显式传 "long"/"short"，不再传 "buy"/"sell"
            tpsl_ok = self.set_position_sl_tp(symbol, _pos_side, sl_price, tp_price)

            # ── v4.5 CRITICAL: TPSL 失败不允许进入成功持仓状态 ──
            # 主单已成交但保护单挂载失败 = 裸仓，必须立即处理
            if not tpsl_ok:
                logger.error(
                    f"🚨 {symbol} TPSL挂载失败! pos-tpsl+plan双重失败，复核保护状态..."
                )
                # 同步复核 — 3 层检测 (缓存/fetch_positions/fetch_open_orders)
                has_sl, has_tp = self._has_position_tpsl(symbol, fill_price, _pos_side)
                if not has_sl:
                    logger.error(
                        f"🔥 {symbol} 复核确认无SL保护 → 裸仓风险 → 立即市价平仓!"
                    )
                    # v4.5: TPSL 附着失败 → 紧急平仓，不允许裸仓存在
                    close_order = None
                    try:
                        close_order = self.create_market_order_close(
                            symbol, amount, side, pos_side=_pos_side
                        )
                        if close_order:
                            logger.warning(
                                f"🛑 {symbol} 紧急平仓成功 (TPSL失败保护) "
                                f"ID={close_order.get('id','N/A')}"
                            )
                    except Exception as close_e:
                        logger.error(f"💥 {symbol} 紧急平仓也失败: {close_e}")
                    # v4.5: 设致命标志位 → 通知调用方"市价单已成但TPSL致命失败，禁止重试"
                    # 防止 safety_manager.place_with_limit_fallback 降级到限价单重开仓
                    self._tpsl_fatal = symbol
                    # P0修复: 返回 close context 供 bot 层 finalize, 避免静默财务黑洞
                    # 无论平仓成功与否，都标记为失败，不进入成功持仓状态
                    return {
                        "tpsl_fatal_close": True,
                        "entry_order": order,
                        "close_order": close_order,
                        "entry_price": fill_price,
                        "amount": amount,
                        "side": side,
                        "pos_side": _pos_side,
                        "sl_price": sl_price,
                    }
                else:
                    # SL 已由交易所端自动挂载（pos-tpsl code=00000 但返回False的边界情况）
                    logger.info(
                        f"🛡️  {symbol} 复核确认SL已存在 (可能交易所异步挂载) → 放行"
                    )

            # v4.0: 多级 TP (第2级+) 用独立计划单补充
            if tp_parts and len(tp_parts) > 1:
                hold_side = _pos_side  # v4.5: 直接用持仓方向
                for tp_p, ratio in tp_parts[1:]:
                    try:
                        if self._is_uta:
                            # UTA V3: POST /api/v3/trade/place-strategy-order (pos_profit)
                            ok = self._uta_v3_place_strategy_order(
                                symbol=symbol, plan_type="pos_profit",
                                trigger_price=tp_p, hold_side=hold_side)
                            if ok:
                                logger.info(f"  📏 附加TP(U) @ {tp_p:.4f} ({ratio*100:.0f}%)")
                            else:
                                logger.warning(f"  ⚠️ 附加TP(U)失败")
                        else:
                            # Classic V2: place_tpsl_order
                            raw_symbol = symbol.split(":")[0].replace("/", "")
                            tp_str = self.exchange.price_to_precision(symbol, tp_p)
                            tp_params = {
                                "symbol": raw_symbol,
                                "productType": "usdt-futures",
                                "marginCoin": "USDT",
                                "holdSide": hold_side,
                                "planType": "pos_profit",
                                "triggerPrice": tp_str,
                                "triggerType": "mark_price",
                                "executePrice": tp_str,
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
            logger.warning(f"⚠️ 获取 {symbol} contract_size 失败，回退到 1.0 — 仓位计算可能严重错误!")
            return 1.0

    def get_min_amount(self, symbol: str) -> float:
        """获取最小下单数量"""
        try:
            market = self.exchange.market(symbol)
            return float(market["limits"]["amount"]["min"])
        except Exception:
            logger.warning(f"⚠️ 获取 {symbol} min_amount 失败，回退到 1.0 — 可能下单被拒")
            return 1.0

    def get_min_notional(self, symbol: str) -> float:
        """获取交易所最小下单额 (USDT)，用于实盘小额资金保护"""
        try:
            market = self.exchange.market(symbol)
            # Bitget USDT-M 最小下单额，通常 5 USDT
            min_cost = market.get("limits", {}).get("cost", {}).get("min")
            if min_cost is not None:
                return float(min_cost)
        except Exception:
            pass
        return 5.0  # 保守默认 5 USDT

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
        """获取最近已平仓的真实 PnL (Classic V2 or UTA V3)"""
        try:
            raw_symbol = symbol.replace("/USDT:USDT", "USDT")
            if self._is_uta:
                # UTA V3: /api/v3/position/history-position
                params = {"category": "USDT-FUTURES", "symbol": raw_symbol, "limit": "3"}
                if since:
                    params["startTime"] = str(int(since * 1000))
                resp = self.exchange.private_uta_get_v3_position_history_position(params)
            else:
                # Classic V2
                params = {"symbol": raw_symbol, "productType": "usdt-futures", "limit": "3"}
                if since:
                    params["startTime"] = str(int(since * 1000))
                resp = self.exchange.privateMixGetV2MixPositionHistoryPosition(params)

            if isinstance(resp, dict) and resp.get("code") == "00000":
                records = resp.get("data", {}).get("list", resp.get("data", []))
                if not records:
                    return None
                latest = records[0]
                return {
                    "pnl": float(latest.get("pnl", 0) or 0),
                    "net_profit": float(latest.get("netProfit", 0) or 0),
                    "funding_fee": float(latest.get("totalFunding", 0) or 0),
                    "exit_price": float(latest.get("closeAvgPrice", 0) or 0),
                    "fee": float(latest.get("closeFee", 0) or 0),
                    "open_fee": float(latest.get("openFee", 0) or 0),
                    "position_id": latest.get("positionId", ""),
                    "holding_ms": int(latest.get("holdTime", 0) or 0),
                }
            logger.debug("history-position: code=" + str(resp.get("code")))
            return None
        except Exception as e:
            logger.debug(f"📊 获取 {symbol} 平仓历史失败: {e}")
            return None

    def fetch_position_funding_fee(self, symbol: str) -> float:
        """
        v4.5→Phase2: 从持仓 API 获取当前累计资金费率 (totalFee 字段)
        Bitget 持仓 API 返回 totalFee = 持仓期间累计资金费率 (USDT)
        Returns: 累计资金费率 (正数=收取, 负数=支付)
        """
        try:
            pos = self.exchange.fetch_position(symbol)
            if pos and pos.get("info"):
                total_fee = float(pos["info"].get("totalFee", 0) or 0)
                return total_fee
        except Exception:
            logger.debug(f"⚠️ 获取 {symbol} 资金费率历史失败")
        return 0.0

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

    def _has_position_tpsl(self, symbol: str, entry_price: float, side: str) -> Tuple[bool, bool]:
        """
        检测持仓是否有 SL/TP 保护。

        方法:
          0. (v4.5) 内存缓存 — 沙箱 pos-tpsl 成功但 API 不可见时信任缓存
          1. (主力) fetch_positions → info.stopLoss/takeProfit — 实盘返回
          2. (兜底) fetch_open_orders(stop=True) — 独立计划单

        side: "buy"/"LONG" 或 "sell"/"SHORT"
        返回 (has_sl, has_tp)
        """
        has_sl, has_tp = False, False

        # 方法0: v4.5 内存缓存 — 沙箱 set_position_sl_tp 的 code=00000 信任
        # 缓存键: (symbol, side, entry_int) — 含entry防同方向不同价位的缓存穿越
        side_norm = "long" if str(side).upper() in ("BUY", "LONG") else "short"
        _entry_int = int(round(entry_price, 2) * 100) if entry_price > 0 else 0
        # v4.5: 尝试精确匹配 + entry模糊匹配 (允许±1%的entry偏差)
        CACHE_TTL = 600  # 10分钟TTL
        for _offset in (0, -1, 1, -2, 2):  # entry_key 允许轻微偏差
            _try_key = (symbol, side_norm, _entry_int + _offset)
            if _try_key in self._tpsl_cache:
                _, _, cache_ts = self._tpsl_cache[_try_key]
                if time.time() - cache_ts > CACHE_TTL:
                    del self._tpsl_cache[_try_key]
                    continue  # TTL过期, 尝试下一个偏移
                # 验证持仓仍存在
                try:
                    pos_list = self.exchange.fetch_positions([symbol])
                    has_matching_pos = any(
                        abs(float(p.get("contracts", 0) or 0)) > 0
                        for p in pos_list
                    )
                    if not has_matching_pos:
                        del self._tpsl_cache[_try_key]
                        logger.debug(f"🧹 {symbol} TPSL缓存失效: 持仓已关闭")
                    else:
                        return (True, True)
                except Exception:
                    return (True, True)
                break  # 找到了key, 无论是否命中都停止偏移搜索

        # 方法1: fetch_positions — 实盘返回 stopLoss/takeProfit
        # v4.5: 增加方向校验 — 只检查非0值不够, 必须确认 SL/TP 在正确方向
        try:
            pos_list = self.exchange.fetch_positions([symbol])
            side_is_long = str(side).upper() in ("BUY", "LONG")
            for p in pos_list:
                if abs(float(p.get("contracts", 0) or 0)) <= 0:
                    continue
                info = p.get("info", {})
                sl_raw = info.get("stopLoss", info.get("stopLossPrice", ""))
                tp_raw = info.get("takeProfit", info.get("takeProfitPrice", ""))
                # 用持仓 mark 价做方向参照 (比 entry_price 更实时)
                mark_p = float(p.get("markPrice", 0) or 0)
                if mark_p <= 0:
                    mark_p = entry_price
                # SL 方向校验
                if sl_raw and str(sl_raw) not in ("0", "", "None"):
                    sl_f = float(sl_raw)
                    if side_is_long and sl_f < mark_p:
                        has_sl = True
                    elif not side_is_long and sl_f > mark_p:
                        has_sl = True
                    else:
                        logger.warning(
                            f"⚠️  {symbol} SL方向异常: SL={sl_f} mark={mark_p} "
                            f"side={'LONG' if side_is_long else 'SHORT'} → 视为无效保护"
                        )
                # TP 方向校验
                if tp_raw and str(tp_raw) not in ("0", "", "None"):
                    tp_f = float(tp_raw)
                    if side_is_long and tp_f > mark_p:
                        has_tp = True
                    elif not side_is_long and tp_f < mark_p:
                        has_tp = True
                    else:
                        logger.warning(
                            f"⚠️  {symbol} TP方向异常: TP={tp_f} mark={mark_p} "
                            f"side={'LONG' if side_is_long else 'SHORT'} → 视为无效保护"
                        )
        except Exception:
            logger.debug("⚠️  静默异常", exc_info=True)

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
                logger.debug("⚠️  静默异常", exc_info=True)

        return (has_sl, has_tp)

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

        Classic V2: Hedge模式 close_position / create_order(tradeSide=close)
          - 平多: side=buy,  tradeSide=close
          - 平空: side=sell, tradeSide=close
          - 双向模式禁止传 reduceOnly (会导致 40774)

        UTA V3: POST /api/v3/trade/close-positions
          - category=USDT-FUTURES, symbol, posSide

        pos_side: "long" or "short"
        """
        # UTA V3: 一键平仓 API (不依赖 reduceOnly, 不依赖方向推导)
        if self._is_uta:
            _ps = pos_side if pos_side else ("short" if str(side).upper() in ("SELL", "SHORT") else "long")
            return self._uta_v3_close_positions(symbol, _ps)

        # Classic V2 path
        try:
            # 优先使用 ccxt 的 close_position (内部处理了参数转换)
            if pos_side and hasattr(self.exchange, 'close_position'):
                return self.exchange.close_position(symbol, pos_side)

            # 回退: v2 API 双向模式参数 (P0修复: 对齐 Bitget 官方 Place Order 文档)
            # Bitget hedge mode tradeSide=close 规则:
            #   - Close long:  side=buy,  tradeSide=close
            #   - Close short: side=sell, tradeSide=close
            # 参考: https://www.bitget.com/api-doc/contract/trade/Place-Order
            close_side_map = {"long": "buy", "short": "sell"}
            close_side = close_side_map.get(pos_side, side)
            return self.exchange.create_order(
                symbol=symbol, type="market", side=close_side,
                amount=amount, params={"tradeSide": "close", "marginMode": "crossed"},
            )
        except Exception as e:
            err_str = str(e)
            if "40757" in err_str or "Not enough position" in err_str or "position is not available" in err_str:
                logger.info(f"📦 {symbol} 仓位已不存在 (pos-tpsl 已平仓)")
                return {"id": "already_closed", "note": "position already closed by pos-tpsl"}
            logger.error(f"市价平仓失败 {symbol}: {e}")
            return None

    def is_sandbox(self) -> bool:
        """是否在沙箱环境中"""
        return getattr(self.exchange, "sandbox_mode", True)

    def set_position_sl_tp(
        self, symbol: str, side: str, sl_price: float, tp_price: float
    ) -> bool:
        """
        TPSL 设置 (Classic V2 + UTA V3 双路径)

        Classic V2: POST /api/v2/mix/order/place-pos-tpsl — 一次设 SL+TP
        UTA V3:    POST /api/v3/trade/place-strategy-order × 2 — pos_loss + pos_profit

        彻底解决重复计划单累积问题。
        """
        try:
            # side 统一为持仓方向: "long"/"short"
            side_lower = side.lower() if isinstance(side, str) else ""
            if side_lower in ("long", "short"):
                hold_side = side_lower
            elif side_lower in ("buy",):      # v4.5 fix: buy=开多仓 → hold long (不是平空仓!)
                hold_side = "long"
            elif side_lower in ("sell",):     # v4.5 fix: sell=开空仓 → hold short (不是平多仓!)
                hold_side = "short"
            else:
                hold_side = "long"  # fallback

            # 获取价格精度 (使用 ccxt 的价格格式化)
            try:
                sl_str = self.exchange.price_to_precision(symbol, sl_price)
                tp_str = self.exchange.price_to_precision(symbol, tp_price)
                sl_val = float(sl_str)
            except Exception:
                # v4.5→Phase2: 回退时从 market 查询精度而非盲目 round(price,2)
                try:
                    market = self.exchange.market(symbol)
                    precision = market.get("precision", {}).get("price", 0.01)
                    if precision < 1:
                        # 小数值精度（如 0.001），计算小数位数
                        import math
                        decimals = max(0, int(-math.log10(precision)))
                    else:
                        decimals = 2  # 保守默认
                except Exception:
                    decimals = 2
                sl_str = str(round(sl_price, decimals))
                tp_str = str(round(tp_price, decimals))
                sl_val = float(sl_str)
                logger.warning(
                    f"⚠️ {symbol} price_to_precision 失败，回退到 {decimals}位小数 "
                    f"(SL={sl_str}, TP={tp_str}) — 请验证交易所是否接受"
                )

            # 验证 SL/TP 方向并修正 (v4.0: 加 TP 校验)
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                mark = float(ticker.get("mark", ticker.get("last", 0)))
            except Exception:
                mark = 0.0
            if mark > 0:
                # SL 校验
                if hold_side == "long" and sl_val >= mark:
                    sl_str = self.exchange.price_to_precision(symbol, mark * 0.995)
                    sl_val = float(sl_str)
                elif hold_side == "short" and sl_val <= mark:
                    sl_str = self.exchange.price_to_precision(symbol, mark * 1.005)
                    sl_val = float(sl_str)
                # TP 校验 (v4.0: 新增)
                tp_val = float(tp_str)
                if hold_side == "long" and tp_val <= mark:
                    tp_str = self.exchange.price_to_precision(symbol, mark * 1.02)
                    logger.warning(f"⚠️  TP方向修正: LONG TP必须>mark({mark}), 设为{mark*1.02:.4f}")
                elif hold_side == "short" and tp_val >= mark:
                    tp_str = self.exchange.price_to_precision(symbol, mark * 0.98)
                    logger.warning(f"⚠️  TP方向修正: SHORT TP必须<mark({mark}), 设为{mark*0.98:.4f}")

            # ── UTA V3 路径: 两次策略单 (pos_loss + pos_profit) ──
            if self._is_uta:
                sl_ok = self._uta_v3_place_strategy_order(
                    symbol=symbol, plan_type="pos_loss",
                    trigger_price=float(sl_str), hold_side=hold_side)
                tp_ok = self._uta_v3_place_strategy_order(
                    symbol=symbol, plan_type="pos_profit",
                    trigger_price=float(tp_str), hold_side=hold_side)
                if sl_ok and tp_ok:
                    logger.info(f"🛡️🎯 {symbol} SL={sl_str} TP={tp_str} → UTA V3 OK")
                    _entry_key = int(mark * 100) if mark > 0 else 0
                    self._tpsl_cache[(symbol, hold_side, _entry_key)] = (
                        float(sl_str), float(tp_str), time.time()
                    )
                    return True
                # V3 不支持 place-pos-tpsl 降级, 部分失败即返回
                logger.warning(f"⚠️ UTA V3 TPSL 部分失败: SL={sl_ok} TP={tp_ok}")
                return sl_ok or tp_ok

            # ── Classic V2 路径: place-pos-tpsl ──
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
                # v4.5: 沙箱信任缓存 — _has_position_tpsl 可查此缓存
                # v4.5: 缓存键 (symbol, side, entry_int) + TTL — 同方向不同entry可区分
                _entry_key = int(float(mark) * 100) if mark > 0 else 0  # 精度0.01
                self._tpsl_cache[(symbol, hold_side, _entry_key)] = (
                    float(sl_str), float(tp_str), time.time()
                )
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
        """v4.0: 用独立 plan order 设 SL/TP (place-pos-tpsl 的降级方案)

        UTA V3:    POST /api/v3/trade/place-strategy-order × 2
        Classic V2: POST /api/v2/mix/order/place-tpsl-order × 2
        """
        # UTA V3 path
        if self._is_uta:
            sl_ok = self._uta_v3_place_strategy_order(
                symbol=symbol, plan_type="pos_loss",
                trigger_price=float(sl_str), hold_side=hold_side)
            tp_ok = self._uta_v3_place_strategy_order(
                symbol=symbol, plan_type="pos_profit",
                trigger_price=float(tp_str), hold_side=hold_side)
            return sl_ok and tp_ok

        # Classic V2 path
        try:
            ok = True
            # 撤旧止损单
            try:
                old = self.exchange.fetch_open_orders(symbol, params={"stop": True}) or []
                for o in old:
                    if self._is_reduce_only(o):
                        self.exchange.cancel_order(str(o.get('id', '')), symbol)
            except Exception:
                logger.debug("⚠️  静默异常", exc_info=True)

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

    def fetch_recent_trades(self, symbol: str, limit: int = 100) -> list:
        """拉取最近成交记录 (用于 CVD 计算)"""
        try:
            return self.exchange.fetch_trades(symbol, limit=limit)
        except Exception:
            return []

