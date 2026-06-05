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
                    logger.debug("⚠️  静默异常", exc_info=True)  # 沙箱环境可能不支持

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
            logger.debug("⚠️  静默异常", exc_info=True)
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
                    logger.debug("⚠️  静默异常", exc_info=True)
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
                logger.debug("⚠️  静默异常", exc_info=True)

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

