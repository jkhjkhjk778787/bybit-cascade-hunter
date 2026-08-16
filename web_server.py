#!/usr/bin/env python3
"""
========================================================================================
🚀 [CASCADE PRO TRADING SUITE - BACKEND SERVER]
aiohttp 비동기 고성능 웹 서버 & 실시간 멀티 거래소 WebSocket 브로드캐스터
========================================================================================
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import deque, OrderedDict
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from aiohttp import web, WSMsgType, ClientSession
import duckdb
import orjson
from dotenv import load_dotenv

# 사용자님의 crypto_liquidation 라이브러리 연동
from crypto_liquidation import LiquidationStream, LiquidationEvent, OrderSide, PositionSide

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [WebServer] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("WebServer")

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
DB_PATH = str(BASE_DIR / "bybit_trades.duckdb")
ACTIVE_SYMBOLS_PATH = str(BASE_DIR / "active_symbols.json")
CRED_PATH = "/home/jph/.bybit/oauth_token.json"
BYBIT_REST_URL = "https://api.bybit.com"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val == "" or val is None:
            return default
        return float(val)
    except Exception:
        return default


class BybitTradingService:
    def __init__(self, cred_path: str):
        self.api_key = ""
        self.api_secret = ""
        self.load_credentials(cred_path)
        self.symbol_specs: Dict[str, Dict[str, Any]] = {}
        self.load_symbol_specs()

    def load_credentials(self, path: str):
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    cred = json.load(f)
                ai_cred = cred.get("ai-account", cred)
                self.api_key = ai_cred.get("api_key", "")
                self.api_secret = ai_cred.get("api_secret", "")
            except Exception as e:
                logger.error(f"자격증명 로드 실패: {e}")

    def _sign(self, params_str: str, timestamp: int, recv_window: int = 5000) -> str:
        param_str = f"{timestamp}{self.api_key}{recv_window}{params_str}"
        return hmac.new(self.api_secret.encode('utf-8'), param_str.encode('utf-8'), hashlib.sha256).hexdigest()

    def _request(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            return {"retCode": -1, "retMsg": "API credentials missing"}

        ts = int(time.time() * 1000)
        url = f"{BYBIT_REST_URL}{endpoint}"
        body_str = ""

        if method == "GET" and params:
            qs = urllib.parse.urlencode(params)
            url += f"?{qs}"
            body_str = qs
        elif method == "POST" and params:
            body_str = json.dumps(params)

        sign = self._sign(body_str, ts)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": str(ts),
            "X-BAPI-RECV-WINDOW": "5000",
            "Content-Type": "application/json"
        }

        req = urllib.request.Request(url, data=body_str.encode('utf-8') if method == "POST" else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}

    def load_symbol_specs(self):
        url = f"{BYBIT_REST_URL}/v5/market/instruments-info?category=linear"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                for item in data.get("result", {}).get("list", []):
                    sym = item.get("symbol")
                    lsf = item.get("lotSizeFilter", {})
                    pf = item.get("priceFilter", {})
                    self.symbol_specs[sym] = {
                        "min_qty": _safe_float(lsf.get("minOrderQty"), 0.001),
                        "qty_step": _safe_float(lsf.get("qtyStep"), 0.001),
                        "tick_size": _safe_float(pf.get("tickSize"), 0.0001),
                    }
        except Exception as e:
            logger.error(f"심볼 메타데이터 로드 실패: {e}")

    def get_wallet_balance(self) -> Dict[str, Any]:
        res = self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if res.get("retCode") == 0:
            coins = res.get("result", {}).get("list", [{}])[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    wb = _safe_float(c.get("walletBalance"), 0.0)
                    avail = _safe_float(c.get("availableToWithdraw"), wb)
                    if avail == 0.0 and wb > 0.0:
                        avail = wb
                    return {
                        "equity": _safe_float(c.get("equity"), wb),
                        "walletBalance": wb,
                        "availableBalance": avail,
                        "unrealisedPnl": _safe_float(c.get("unrealisedPnl"), 0.0)
                    }
        return {"equity": 0.0, "walletBalance": 0.0, "availableBalance": 0.0, "unrealisedPnl": 0.0}

    def get_positions(self) -> List[Dict[str, Any]]:
        res = self._request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        positions = []
        if res.get("retCode") == 0:
            for p in res.get("result", {}).get("list", []):
                size = _safe_float(p.get("size"), 0.0)
                if size > 0:
                    positions.append({
                        "symbol": p.get("symbol"),
                        "side": p.get("side"),
                        "size": size,
                        "entryPrice": _safe_float(p.get("avgPrice"), 0.0),
                        "markPrice": _safe_float(p.get("markPrice"), 0.0),
                        "unrealisedPnl": _safe_float(p.get("unrealisedPnl"), 0.0),
                        "leverage": _safe_float(p.get("leverage"), 15.0),
                        "stopLoss": _safe_float(p.get("stopLoss"), 0.0),
                        "takeProfit": _safe_float(p.get("takeProfit"), 0.0),
                        "updatedTime": int(_safe_float(p.get("updatedTime"), 0))
                    })
        return positions

    def place_market_order(self, symbol: str, side: str, order_value_usdt: float, leverage: float = 15.0, tp_pct: float = 2.0, sl_pct: float = 0.6) -> Dict[str, Any]:
        self._request("POST", "/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)
        })

        spec = self.symbol_specs.get(symbol, {"min_qty": 0.001, "qty_step": 0.001, "tick_size": 0.0001})
        
        t_res = self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        last_price = 0.0
        if t_res.get("retCode") == 0:
            last_price = float(t_res.get("result", {}).get("list", [{}])[0].get("lastPrice", 0.0))
        
        if last_price <= 0:
            return {"retCode": -1, "retMsg": "Invalid price"}

        notional = order_value_usdt * leverage
        raw_qty = notional / last_price
        
        step_dec = max(0, -Decimal(str(spec["qty_step"])).as_tuple().exponent)
        qty = float(Decimal(str(raw_qty)).quantize(Decimal(str(spec["qty_step"])), rounding=ROUND_DOWN))
        if qty < spec["min_qty"]:
            qty = spec["min_qty"]

        tick_dec = max(0, -Decimal(str(spec["tick_size"])).as_tuple().exponent)
        if side == "Buy":
            tp_p = last_price * (1 + tp_pct / 100.0)
            sl_p = last_price * (1 - sl_pct / 100.0)
        else:
            tp_p = last_price * (1 - tp_pct / 100.0)
            sl_p = last_price * (1 + sl_pct / 100.0)

        tp_str = f"{tp_p:.{tick_dec}f}"
        sl_str = f"{sl_p:.{tick_dec}f}"

        order_params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": f"{qty:.{step_dec}f}",
            "timeInForce": "IOC",
            "takeProfit": tp_str,
            "stopLoss": sl_str,
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market"
        }

        return self._request("POST", "/v5/order/create", order_params)

    def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        close_side = "Sell" if side == "Buy" else "Buy"
        return self._request("POST", "/v5/order/create", {
            "category": "linear",
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(size),
            "reduceOnly": True,
            "timeInForce": "IOC"
        })

    def close_all_positions(self) -> List[Dict[str, Any]]:
        positions = self.get_positions()
        results = []
        for p in positions:
            sym = p["symbol"]
            res = self.close_position(sym, p["side"], float(p["size"]))
            results.append({"symbol": sym, "result": res})
        return results


import tempfile
import shutil
import pandas as pd


def query_duckdb_snapshot(query_sql: str) -> pd.DataFrame:
    """DuckDB 라이터 락 충돌 방지를 위한 초고속 무간섭 스냅샷 조회"""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    td = tempfile.mkdtemp()
    try:
        tmp_db = os.path.join(td, 'snap.duckdb')
        shutil.copy2(DB_PATH, tmp_db)
        conn = duckdb.connect(tmp_db, read_only=True)
        df = conn.execute(query_sql).df()
        conn.close()
        return df
    except Exception as e:
        logger.error(f"스냅샷 쿼리 에러: {e}")
        return pd.DataFrame()
    finally:
        shutil.rmtree(td, ignore_errors=True)


class CascadeTradingServer:
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application()
        self.trader = BybitTradingService(CRED_PATH)
        self.ws_clients: Set[web.WebSocketResponse] = set()
        self.is_running = True
        self.auto_trade_enabled = False

        self._http_session: Optional[ClientSession] = None
        self._chart_cache = OrderedDict()
        self._active_symbols_cache = {}
        self._active_symbols_mtime = 0
        self._last_ticker_broadcast: Dict[str, float] = {}
        self.latest_prices: Dict[str, float] = {}
        self.armed_status: Dict[str, Dict[str, Any]] = {}
        self.recent_liquidations_by_sym: Dict[str, deque] = {}

        self.ticker_ws = None
        self._cvd_deltas: Dict[str, Dict[str, float]] = {}
        self.position_entry_times: Dict[str, float] = {}
        self.cascade_cooldowns: Dict[str, float] = {}
        # 초기 기본 20개 동시 상장 심볼
        self.top20_symbols: List[str] = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "COWUSDT", "CYSUSDT",
            "ACEUSDT", "AKEUSDT", "HYPEUSDT", "HEMIUSDT", "XRPUSDT",
            "BEATUSDT", "HUSDT", "LINKUSDT", "VELVETUSDT", "APRUSDT",
            "DOGEUSDT", "ZECUSDT", "ADAUSDT", "WALUSDT", "WLDUSDT"
        ]
        self.subscribed_ticker_symbols: Set[str] = set(self.top20_symbols)

        self.setup_routes()

    async def update_top_20_subscriptions(self, new_top20: List[str]):
        """상위 20개 심볼 유지: 탈락 심볼 구독 해제, 진입 심볼 신규 구독"""
        old_set = set(self.top20_symbols)
        new_set = set(new_top20)

        dropped = old_set - new_set
        added = new_set - old_set

        self.top20_symbols = list(new_top20)
        self.subscribed_ticker_symbols = set(new_top20)

        # 탈락 심볼 메모리 컬렉션 정리 (Memory Leak 방지)
        for s in dropped:
            self._cvd_deltas.pop(s, None)
            self.recent_liquidations_by_sym.pop(s, None)
            self.latest_prices.pop(s, None)
            self.cascade_cooldowns.pop(s, None)

        if self.ticker_ws and not self.ticker_ws.closed:
            if dropped:
                try:
                    drop_topics = [f"tickers.{s}" for s in dropped]
                    await self.ticker_ws.send_str(orjson.dumps({"op": "unsubscribe", "args": drop_topics}).decode('utf-8'))
                    logger.info(f"🛑 [Bybit Ticker Stream] TOP 20 제외 심볼 구독 해제: {', '.join(dropped)}")
                except Exception as e:
                    logger.error(f"티커 구독 해제 실패: {e}")
            if added:
                try:
                    add_topics = [f"tickers.{s}" for s in added]
                    await self.ticker_ws.send_str(orjson.dumps({"op": "subscribe", "args": add_topics}).decode('utf-8'))
                    logger.info(f"✨ [Bybit Ticker Stream] TOP 20 신규 진입 심볼 구독 등록: {', '.join(added)}")
                except Exception as e:
                    logger.error(f"티커 신규 구독 실패: {e}")

    async def subscribe_ticker_symbol(self, symbol: str):
        """특정 심볼 조회 시 임시 티커 스트림 활성화"""
        if not symbol or symbol in self.subscribed_ticker_symbols:
            return
        self.subscribed_ticker_symbols.add(symbol)
        if self.ticker_ws and not self.ticker_ws.closed:
            try:
                await self.ticker_ws.send_str(orjson.dumps({"op": "subscribe", "args": [f"tickers.{symbol}"]}).decode('utf-8'))
                logger.info(f"📡 [Bybit Ticker Stream] 심볼 실시간 구독 추가: tickers.{symbol}")
            except Exception as e:
                logger.error(f"티커 동적 구독 실패: {e}")

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_api_status)
        self.app.router.add_get("/api/account", self.handle_api_account)
        self.app.router.add_get("/api/symbols", self.handle_api_symbols)
        self.app.router.add_get("/api/history", self.handle_api_history)
        self.app.router.add_post("/api/order/market", self.handle_api_market_order)
        self.app.router.add_post("/api/order/close_all", self.handle_api_close_all)
        self.app.router.add_post("/api/autotrade/toggle", self.handle_api_toggle_autotrade)
        self.app.router.add_get("/ws/live", self.handle_ws)
        self.app.router.add_static("/css/", path=str(WEB_DIR / "css"), name="css")
        self.app.router.add_static("/js/", path=str(WEB_DIR / "js"), name="js")

    async def handle_index(self, request: web.Request) -> web.Response:
        index_file = WEB_DIR / "index.html"
        if index_file.exists():
            return web.FileResponse(str(index_file))
        return web.Response(text="Trading Suite Web UI is loading...", content_type="text/html")

    async def handle_api_status(self, request: web.Request) -> web.Response:
        try:
            mt = os.path.getmtime(ACTIVE_SYMBOLS_PATH)
            if mt != self._active_symbols_mtime:
                with open(ACTIVE_SYMBOLS_PATH, "r") as f:
                    self._active_symbols_cache = orjson.loads(f.read())
                self._active_symbols_mtime = mt
        except Exception:
            pass
        active_symbols_data = self._active_symbols_cache

        now = time.time()
        active_armed = {k: v for k, v in self.armed_status.items() if now <= v.get("expires", 0)}

        return web.json_response({
            "status": "online",
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "auto_trade_enabled": self.auto_trade_enabled,
            "active_symbols": active_symbols_data,
            "armed_symbols": active_armed,
            "connected_ws_clients": len(self.ws_clients),
            "latest_prices": self.latest_prices
        })

    async def handle_api_account(self, request: web.Request) -> web.Response:
        balance = await asyncio.to_thread(self.trader.get_wallet_balance)
        positions = await asyncio.to_thread(self.trader.get_positions)
        return web.json_response({
            "balance": balance,
            "positions": positions
        })

    async def fetch_bybit_chart_data(self, symbol: str) -> Dict[str, Any]:
        """Bybit REST API를 통한 0ms 무지연 1분봉 캔들 및 최근 틱 조회 (디스크 I/O 완전 배제)"""
        now = time.time()
        cached = getattr(self, "_chart_cache", {}).get(symbol)
        if cached and (now - cached["cached_at"] < 3.0):
            return cached["data"]

        candles = []
        trades = []
        try:
            kline_url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=1&limit=60"
            trade_url = f"https://api.bybit.com/v5/market/recent-trade?category=linear&symbol={symbol}&limit=100"

            async with self._http_session.get(kline_url, timeout=2.5) as r1, self._http_session.get(trade_url, timeout=2.5) as r2:
                kdata = await r1.json()
                tdata = await r2.json()

                for item in kdata.get("result", {}).get("list", []):
                    candles.append({
                        "t": int(item[0]),
                        "o": float(item[1]),
                        "h": float(item[2]),
                        "l": float(item[3]),
                        "c": float(item[4]),
                        "v": float(item[5])
                    })
                candles.reverse()

                for tr in tdata.get("result", {}).get("list", []):
                    trades.append({
                        "t": int(tr.get("time", 0)),
                        "p": float(tr.get("price", 0)),
                        "s": tr.get("side", "Buy"),
                        "v": float(tr.get("size", 1))
                    })
                trades.reverse()

        except Exception as e:
            logger.error(f"차트 데이터 조회 에러: {e}")

        result = {"candles": candles, "trades": trades}
        self._chart_cache[symbol] = {"cached_at": now, "data": result}
        self._chart_cache.move_to_end(symbol)
        while len(self._chart_cache) > 20:
            self._chart_cache.popitem(last=False)
        return result

    async def handle_api_symbols(self, request: web.Request) -> web.Response:
        """현재 활성 20개 동시 상장 심볼 목록 반환"""
        return web.json_response({"symbols": self.top20_symbols})

    async def handle_api_history(self, request: web.Request) -> web.Response:
        """0ms 초고속 차트 데이터 및 청산 내역 반환"""
        symbol = request.query.get("symbol", "VELVETUSDT").upper()
        asyncio.create_task(self.subscribe_ticker_symbol(symbol))
        chart_data = await self.fetch_bybit_chart_data(symbol)

        # In-memory recent liquidations for this symbol
        mem_liqs = list(self.recent_liquidations_by_sym.get(symbol, []))
        liq_data = []
        for ml in mem_liqs:
            ts = ml.get("timestamp", int(time.time() * 1000))
            usd = ml.get("notional_usd", 0.0)
            p = ml.get("price", 0.0)
            pos_side = ml.get("pos_side", "long" if ml.get("side") == "sell" else "short")
            exch = ml.get("exchange", "binance")
            liq_data.append({
                "exch": exch,
                "symbol": symbol,
                "t": ts,
                "pos_side": pos_side,
                "p": p,
                "v": ml.get("amount", 0.0),
                "usd": usd
            })

        return web.json_response({
            "symbol": symbol,
            "candles": chart_data["candles"],
            "trades": chart_data["trades"],
            "liquidations": liq_data
        })

    async def handle_api_market_order(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            symbol = body.get("symbol", "VELVETUSDT").upper()
            side = body.get("side", "Buy")
            order_usd = float(body.get("order_usd", 1.5))
            leverage = float(body.get("leverage", 15.0))
            tp_pct = float(body.get("tp_pct", 2.0))
            sl_pct = float(body.get("sl_pct", 0.6))

            result = await asyncio.to_thread(
                self.trader.place_market_order,
                symbol,
                side,
                order_usd,
                leverage,
                tp_pct,
                sl_pct
            )
            if result.get("retCode") == 0:
                self.position_entry_times[symbol] = time.time()
                logger.info(f"⚡ [{symbol}] 주문 성공 ➔ 45초 안전 타임아웃 타이머 가동!")

            return web.json_response({"success": result.get("retCode") == 0, "response": result})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_api_close_all(self, request: web.Request) -> web.Response:
        res = await asyncio.to_thread(self.trader.close_all_positions)
        return web.json_response({"success": True, "results": res})

    async def handle_api_toggle_autotrade(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.auto_trade_enabled = bool(body.get("enabled", not self.auto_trade_enabled))
        return web.json_response({"success": True, "auto_trade_enabled": self.auto_trade_enabled})

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        self.ws_clients.add(ws)
        logger.info(f"🌐 [Web UI 접속] 클라이언트 연결됨 (총 {len(self.ws_clients)}명)")

        try:
            balance = await asyncio.to_thread(self.trader.get_wallet_balance)
            positions = await asyncio.to_thread(self.trader.get_positions)
            await ws.send_str(orjson.dumps({
                "type": "SNAPSHOT",
                "balance": balance,
                "positions": positions,
                "prices": self.latest_prices,
                "armed": self.armed_status,
                "recent_liqs": sorted([x for dq in self.recent_liquidations_by_sym.values() for x in dq], key=lambda x: x.get('timestamp', 0))[-30:],
                "auto_trade": self.auto_trade_enabled
            }).decode('utf-8'))
        except Exception:
            pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = orjson.loads(msg.data)
                    action = data.get("action")
                    if action == "PING":
                        await ws.send_str('{"type":"PONG"}')
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WS 에러: {ws.exception()}")
        finally:
            self.ws_clients.remove(ws)
            logger.info(f"🌐 [Web UI 단절] 클라이언트 퇴장 (남은 접속자: {len(self.ws_clients)}명)")

        return ws

    async def broadcast(self, payload: Dict[str, Any]):
        if not self.ws_clients:
            return
        msg_str = orjson.dumps(payload).decode('utf-8')
        results = await asyncio.gather(
            *[ws.send_str(msg_str) for ws in self.ws_clients],
            return_exceptions=True
        )
        dead_clients = set()
        for ws, r in zip(list(self.ws_clients), results):
            if isinstance(r, Exception):
                dead_clients.add(ws)
        for d in dead_clients:
            self.ws_clients.discard(d)

    async def run_liquidation_stream(self):
        """바이낸스 & 바이비트 2대 거래소 실시간 청산 스트림 (TOP 20 심볼 타겟 필터링)"""
        binance_recent_liqs: Dict[str, Dict[str, Any]] = {}

        while self.is_running:
            try:
                # 바이낸스 + 바이비트 2개 거래소 청산 감시 ($50 이상)
                async with LiquidationStream(exchanges=["binance", "bybit"], min_notional_usd=50.0) as stream:
                    logger.info("⚡ [crypto_liquidation] 바이낸스 & 바이비트 청산 스트림 가동 (TOP 20 전용)!")
                    async for event in stream:
                        if not self.is_running: break

                        sym = event.symbol
                        # 20개 선정 심볼만 엄격 필터링
                        if self.top20_symbols and sym not in self.top20_symbols:
                            continue

                        now = time.time()
                        event_dict = event.to_dict()
                        if sym not in self.recent_liquidations_by_sym:
                            self.recent_liquidations_by_sym[sym] = deque(maxlen=50)
                        self.recent_liquidations_by_sym[sym].append(event_dict)

                        is_cascade = False
                        cascade_data = None

                        if event.exchange == "binance":
                            binance_recent_liqs[sym] = {
                                "ts": now,
                                "is_long": event.is_long_liquidation,
                                "usd": event.notional_usd,
                                "price": event.price
                            }

                            if event.notional_usd >= 200.0:
                                if len(self.armed_status) > 100:
                                    self.armed_status = {k: v for k, v in self.armed_status.items() if now <= v.get('expires', 0)}
                                target_side = "Sell" if event.is_long_liquidation else "Buy"
                                self.armed_status[sym] = {
                                    "target_side": target_side,
                                    "expires": now + 8.0,
                                    "duration": 8.0,
                                    "notional_usd": event.notional_usd,
                                    "exchange": "binance",
                                    "side_kr": "숏" if target_side == "Sell" else "롱"
                                }

                        elif event.exchange == "bybit":
                            bin_liq = binance_recent_liqs.get(sym)
                            if bin_liq and (now - bin_liq["ts"] <= 8.0) and (bin_liq["is_long"] == event.is_long_liquidation):
                                # 25초 쿨타임 검사: 동일 심볼 다수 청산 시 중복 트리거 스팸 차단
                                if now >= self.cascade_cooldowns.get(sym, 0.0):
                                    is_cascade = True
                                    self.cascade_cooldowns[sym] = now + 25.0  # 25초 쿨타임 부여
                                    lag_sec = round(now - bin_liq["ts"], 2)
                                    target_side = "Sell" if event.is_long_liquidation else "Buy"
                                    cascade_data = {
                                        "symbol": sym,
                                        "is_long_liq": event.is_long_liquidation,
                                        "target_side": target_side,
                                        "binance_usd": bin_liq["usd"],
                                        "bybit_usd": event.notional_usd,
                                        "lag_sec": lag_sec,
                                        "timestamp": int(now * 1000)
                                    }

                                    # 도화선 1회성 소비 (One-shot)
                                    binance_recent_liqs.pop(sym, None)
                                    self.armed_status.pop(sym, None)

                                    logger.info(f"💥 [연쇄 청산 격발!] {sym} (Binance ${bin_liq['usd']:,.0f} ➔ Bybit ${event.notional_usd:,.0f} | {lag_sec}s 전이) [25s 쿨타임 적용]")
                                    await self.broadcast({
                                        "type": "CASCADE_BURST",
                                        "cascade": cascade_data
                                    })

                        if len(binance_recent_liqs) > 200:
                            binance_recent_liqs = {k: v for k, v in binance_recent_liqs.items() if now - v['ts'] <= 10.0}

                        event_dict["is_cascade"] = is_cascade
                        await self.broadcast({
                            "type": "LIQUIDATION",
                            "event": event_dict,
                            "armed": self.armed_status.get(sym)
                        })

            except Exception as e:
                logger.error(f"청산 스트림 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_ticker_stream(self):
        """Bybit Public Linear Ticker 실시간 스트림"""
        while self.is_running:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(BYBIT_WS_URL) as ws:
                        self.ticker_ws = ws
                        sym_list = list(self.subscribed_ticker_symbols)
                        chunk_size = 10
                        for i in range(0, len(sym_list), chunk_size):
                            chunk = [f"tickers.{s}" for s in sym_list[i:i + chunk_size]]
                            await ws.send_str(orjson.dumps({"op": "subscribe", "args": chunk}).decode('utf-8'))
                            if i + chunk_size < len(sym_list):
                                await asyncio.sleep(0.05)
                        logger.info(f"📡 [Bybit Ticker Stream] {len(sym_list)}개 심볼 가격 스트림 연결 성공!")

                        async for msg in ws:
                            if not self.is_running: break
                            if msg.type == WSMsgType.TEXT:
                                data = orjson.loads(msg.data)
                                topic = data.get("topic", "")
                                if topic.startswith("tickers."):
                                    sym = topic.split(".")[1]
                                    t_data = data.get("data", {})
                                    lp = t_data.get("lastPrice")
                                    if lp:
                                        p_float = float(lp)
                                        self.latest_prices[sym] = p_float
                                        now = time.time()
                                        if now - self._last_ticker_broadcast.get(sym, 0) < 0.1:
                                            continue
                                        self._last_ticker_broadcast[sym] = now

                                        await self.broadcast({
                                            "type": "TICKER",
                                            "symbol": sym,
                                            "price": p_float,
                                            "time": now
                                        })
            except Exception as e:
                logger.error(f"Bybit Ticker 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_top_symbol_scanner(self):
        """바이낸스 & 바이비트 선물 동시 상장 심볼 중 거래대금 TOP 20종 15분 주기 자동 갱신 및 구독 유지"""
        while self.is_running:
            try:
                # 1. Binance USDT-M 선물 상장 심볼 조회
                bin_syms = set()
                try:
                    async with self._http_session.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5) as r_bin:
                        bdata = await r_bin.json()
                        bin_syms = {
                            s['symbol'] for s in bdata.get('symbols', [])
                            if s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'
                        }
                except Exception as e:
                    logger.warning(f"Binance exchangeInfo 조회 오류: {e}")

                # 2. Bybit 선물 티커 조회
                async with self._http_session.get(f"{BYBIT_REST_URL}/v5/market/tickers?category=linear", timeout=5) as r_bybit:
                    bydata = await r_bybit.json()
                    by_list = bydata.get("result", {}).get("list", [])

                dual_candidates = []
                for item in by_list:
                    sym = item.get("symbol", "")
                    if not sym.endswith("USDT") or "USDC" in sym:
                        continue
                    if bin_syms and sym not in bin_syms:
                        continue
                    turnover24h = float(item.get("turnover24h", 0.0))
                    dual_candidates.append((sym, turnover24h))

                # 거래대금 상위 정렬 ➔ 정확히 20개 심볼 선정
                dual_candidates.sort(key=lambda x: x[1], reverse=True)
                top_symbols = [s[0] for s in dual_candidates[:20]]

                if len(top_symbols) >= 10:
                    await self.update_top_20_subscriptions(top_symbols)
                    logger.info(f"🔄 [15M TOP 20 갱신 완료] 동시 상장 20종: {', '.join(top_symbols)}")

            except Exception as e:
                logger.error(f"동시상장 TOP 20 스캐너 에러: {e}")

            # 15분(900초) 주기 갱신
            await asyncio.sleep(900.0)

    async def run_binance_trade_stream(self):
        """바이낸스 USDT-M 선물 20종 실시간 체결(trade) 스트림 ➔ CVD 델타 집계"""
        while self.is_running:
            try:
                symbols = [s.lower() for s in self.top20_symbols]
                stream_param = "/".join([f"{s}@trade" for s in symbols])
                url = f"wss://fstream.binance.com/stream?streams={stream_param}"
                async with self._http_session.ws_connect(url) as ws:
                    logger.info(f"🟡 [Binance Trade Stream] {len(symbols)}개 심볼 실시간 체결(CVD) 연결 완료")
                    async for msg in ws:
                        if not self.is_running: break
                        if msg.type == WSMsgType.TEXT:
                            data = orjson.loads(msg.data)
                            t = data.get("data", {})
                            sym = t.get("s")
                            if sym:
                                p = float(t.get("p", 0.0))
                                q = float(t.get("q", 0.0))
                                m = t.get("m", False)  # True means market sell
                                usd = p * q
                                delta = -usd if m else usd

                                if sym not in self._cvd_deltas:
                                    self._cvd_deltas[sym] = {"bin_delta": 0.0, "byb_delta": 0.0, "price": p}
                                self._cvd_deltas[sym]["bin_delta"] += delta
                                self._cvd_deltas[sym]["price"] = p
            except Exception as e:
                logger.error(f"Binance Trade Stream 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_bybit_trade_stream(self):
        """바이비트 Linear 선물 20종 실시간 체결(publicTrade) 스트림 ➔ CVD 델타 집계"""
        while self.is_running:
            try:
                async with self._http_session.ws_connect(BYBIT_WS_URL) as ws:
                    sym_list = list(self.top20_symbols)
                    chunk_size = 10
                    for i in range(0, len(sym_list), chunk_size):
                        chunk = [f"publicTrade.{s}" for s in sym_list[i:i + chunk_size]]
                        await ws.send_str(orjson.dumps({"op": "subscribe", "args": chunk}).decode('utf-8'))
                        if i + chunk_size < len(sym_list):
                            await asyncio.sleep(0.05)
                    logger.info(f"🟠 [Bybit Trade Stream] {len(sym_list)}개 심볼 실시간 체결(CVD) 연결 완료")

                    async for msg in ws:
                        if not self.is_running: break
                        if msg.type == WSMsgType.TEXT:
                            data = orjson.loads(msg.data)
                            topic = data.get("topic", "")
                            if topic.startswith("publicTrade."):
                                sym = topic.replace("publicTrade.", "")
                                for t in data.get("data", []):
                                    p = float(t.get("p", 0.0))
                                    v = float(t.get("v", 0.0))
                                    side = t.get("S", "Buy")
                                    usd = p * v
                                    delta = usd if side == "Buy" else -usd

                                    if sym not in self._cvd_deltas:
                                        self._cvd_deltas[sym] = {"bin_delta": 0.0, "byb_delta": 0.0, "price": p}
                                    self._cvd_deltas[sym]["byb_delta"] += delta
                                    self._cvd_deltas[sym]["price"] = p
            except Exception as e:
                logger.error(f"Bybit Trade Stream 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_cvd_broadcast_loop(self):
        """100ms 주기로 누적된 Binance & Bybit CVD 델타 일괄(Batch) 브로드캐스트"""
        while self.is_running:
            try:
                if self.ws_clients and self._cvd_deltas:
                    batch = []
                    for sym, d in list(self._cvd_deltas.items()):
                        bin_d = d["bin_delta"]
                        byb_d = d["byb_delta"]
                        if bin_d != 0.0 or byb_d != 0.0:
                            d["bin_delta"] = 0.0
                            d["byb_delta"] = 0.0
                            batch.append({
                                "s": sym,
                                "b": round(bin_d, 2),
                                "y": round(byb_d, 2),
                                "p": d.get("price", 0.0)
                            })
                    if batch:
                        await self.broadcast({
                            "type": "CVD_BATCH",
                            "items": batch,
                            "time": time.time()
                        })
            except Exception as e:
                logger.error(f"CVD 브로드캐스트 에러: {e}")
            await asyncio.sleep(0.1)

    async def run_position_guard_loop(self):
        """실시간 포지션 감시 및 45초 안전 타임아웃 자동 종료 엔진"""
        while self.is_running:
            try:
                positions = await asyncio.to_thread(self.trader.get_positions)
                active_symbols_in_pos = set()
                now = time.time()

                for p in positions:
                    sym = p.get("symbol")
                    size = float(p.get("size", 0.0))
                    if not sym or size <= 0:
                        continue

                    active_symbols_in_pos.add(sym)

                    # 진입 시각 등록 (최초 감지 또는 updatedTime 기준)
                    if sym not in self.position_entry_times:
                        up_time = p.get("updatedTime", 0)
                        if up_time > 0 and (now - (up_time / 1000.0) < 300.0):
                            self.position_entry_times[sym] = up_time / 1000.0
                        else:
                            self.position_entry_times[sym] = now

                    entry_t = self.position_entry_times[sym]
                    elapsed = now - entry_t

                    # 45초 초과 시 자동 시장가 종료
                    if elapsed >= 45.0:
                        logger.warning(f"⏱️ [{sym}] 45초 안전 타임아웃 도달 ({elapsed:.1f}초 경과) ➔ 시장가 자동 종료 실행!")
                        close_res = await asyncio.to_thread(self.trader.close_position, sym, p.get("side"), size)
                        self.position_entry_times.pop(sym, None)
                        await self.broadcast({
                            "type": "POSITION_TIMEOUT",
                            "symbol": sym,
                            "side": p.get("side"),
                            "elapsed": round(elapsed, 1),
                            "result": close_res
                        })

                # 종료된 포지션의 엔트리 타임 정리
                for sym in list(self.position_entry_times.keys()):
                    if sym not in active_symbols_in_pos:
                        self.position_entry_times.pop(sym, None)

            except Exception as e:
                logger.error(f"포지션 가드 루프 에러: {e}")

            await asyncio.sleep(0.5)

    async def run_account_sync_loop(self):
        """계좌 잔고 및 포지션 1초 주기 실시간 동기화 (남은 타임아웃 시간 계산 포함)"""
        while self.is_running:
            try:
                if self.ws_clients:
                    balance = await asyncio.to_thread(self.trader.get_wallet_balance)
                    positions = await asyncio.to_thread(self.trader.get_positions)
                    now = time.time()
                    for p in positions:
                        sym = p.get("symbol")
                        if sym in self.position_entry_times:
                            p["entryTime"] = self.position_entry_times[sym]
                            p["elapsedSec"] = round(now - self.position_entry_times[sym], 1)
                            p["timeoutSec"] = 45.0

                    await self.broadcast({
                        "type": "ACCOUNT_UPDATE",
                        "balance": balance,
                        "positions": positions
                    })
            except Exception as e:
                logger.error(f"계좌 동기화 에러: {e}")
            await asyncio.sleep(1.0)

    async def _warmup_connections(self):
        try:
            async with self._http_session.get('https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=1', timeout=3) as r:
                await r.read()
            logger.info('🔥 HTTP 커넥션 풀 워밍업 완료')
        except Exception:
            pass

    async def start(self):
        self._http_session = ClientSession()
        await self._warmup_connections()

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"🚀 [Cascade Pro Trading Suite] 웹 서버 가동 완료: http://localhost:{self.port}")

        await asyncio.gather(
            self.run_liquidation_stream(),
            self.run_ticker_stream(),
            self.run_binance_trade_stream(),
            self.run_bybit_trade_stream(),
            self.run_cvd_broadcast_loop(),
            self.run_top_symbol_scanner(),
            self.run_account_sync_loop(),
            self.run_position_guard_loop()
        )


def main():
    try:
        os.nice(5)
    except Exception:
        pass

    server = CascadeTradingServer(port=8080)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        server.is_running = False
        if getattr(server, '_http_session', None):
            asyncio.run(server._http_session.close())
        sys.exit(0)


if __name__ == "__main__":
    main()
