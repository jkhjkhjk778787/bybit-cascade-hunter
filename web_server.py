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
from collections import deque
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

    def close_all_positions(self) -> List[Dict[str, Any]]:
        positions = self.get_positions()
        results = []
        for p in positions:
            sym = p["symbol"]
            side = "Sell" if p["side"] == "Buy" else "Buy"
            res = self._request("POST", "/v5/order/create", {
                "category": "linear",
                "symbol": sym,
                "side": side,
                "orderType": "Market",
                "qty": str(p["size"]),
                "reduceOnly": True,
                "timeInForce": "IOC"
            })
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

        self.latest_prices: Dict[str, float] = {}
        self.price_history_5m: Dict[str, deque] = {}
        self.armed_status: Dict[str, Dict[str, Any]] = {}
        self.absorption_alerts: deque = deque(maxlen=50)
        self.recent_liquidations: deque = deque(maxlen=100)

        self.setup_routes()

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
        active_symbols_data = {}
        if os.path.exists(ACTIVE_SYMBOLS_PATH):
            try:
                with open(ACTIVE_SYMBOLS_PATH, "r") as f:
                    active_symbols_data = json.load(f)
            except Exception:
                pass

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
        balance = self.trader.get_wallet_balance()
        positions = self.trader.get_positions()
        return web.json_response({
            "balance": balance,
            "positions": positions
        })

    async def handle_api_symbols(self, request: web.Request) -> web.Response:
        sql = """
            SELECT symbol, count(*) as ticks, max(price) as high, min(price) as low, argmax(price, exec_time) as last_p
            FROM trades
            WHERE exec_time >= (SELECT max(exec_time) - INTERVAL 3 HOUR FROM trades)
            GROUP BY symbol
            ORDER BY ticks DESC
            LIMIT 30
        """
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, query_duckdb_snapshot, sql)
        sym_stats = df.to_dict(orient="records") if not df.empty else []
        return web.json_response({"symbols": sym_stats})

    async def handle_api_history(self, request: web.Request) -> web.Response:
        symbol = request.query.get("symbol", "VELVETUSDT")
        limit = int(request.query.get("limit", 300))

        sql = f"""
            SELECT epoch_ms(exec_time) as t, price as p, side as s, size as v
            FROM trades
            WHERE symbol = '{symbol}'
            ORDER BY exec_time DESC
            LIMIT {limit}
        """
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, query_duckdb_snapshot, sql)
        data = []
        if not df.empty:
            data = df.to_dict(orient="records")
            data.reverse()

        return web.json_response({"symbol": symbol, "trades": data})

    async def handle_api_market_order(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            symbol = body.get("symbol", "VELVETUSDT").upper()
            side = body.get("side", "Buy")
            order_usd = float(body.get("order_usd", 1.5))
            leverage = float(body.get("leverage", 15.0))
            tp_pct = float(body.get("tp_pct", 2.0))
            sl_pct = float(body.get("sl_pct", 0.6))

            result = self.trader.place_market_order(
                symbol=symbol,
                side=side,
                order_value_usdt=order_usd,
                leverage=leverage,
                tp_pct=tp_pct,
                sl_pct=sl_pct
            )
            return web.json_response({"success": result.get("retCode") == 0, "response": result})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_api_close_all(self, request: web.Request) -> web.Response:
        res = self.trader.close_all_positions()
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
            balance = self.trader.get_wallet_balance()
            positions = self.trader.get_positions()
            await ws.send_str(orjson.dumps({
                "type": "SNAPSHOT",
                "balance": balance,
                "positions": positions,
                "prices": self.latest_prices,
                "armed": self.armed_status,
                "recent_liqs": list(self.recent_liquidations)[-30:],
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
        dead_clients = set()
        for ws in self.ws_clients:
            try:
                await ws.send_str(msg_str)
            except Exception:
                dead_clients.add(ws)
        for d in dead_clients:
            self.ws_clients.discard(d)

    async def run_liquidation_stream(self):
        """사용자님의 crypto-liquidation-stream을 통한 실시간 멀티 거래소 청산 브로드캐스트 & 연쇄 감지"""
        binance_recent_liqs: Dict[str, Dict[str, Any]] = {}

        while self.is_running:
            try:
                async with LiquidationStream(exchanges=["binance", "bybit", "okx"], min_notional_usd=0.0) as stream:
                    logger.info("⚡ [crypto_liquidation] 3대 거래소 실시간 청산 스트림 & 연쇄 감지기 가동!")
                    async for event in stream:
                        if not self.is_running: break

                        now = time.time()
                        event_dict = event.to_dict()
                        self.recent_liquidations.append(event_dict)

                        sym = event.symbol
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
                                is_cascade = True
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

                                logger.info(f"💥 [연쇄 청산 포착!] {sym} (Binance ${bin_liq['usd']:,.0f} ➔ Bybit ${event.notional_usd:,.0f} | {lag_sec}s 전이)")
                                await self.broadcast({
                                    "type": "CASCADE_BURST",
                                    "cascade": cascade_data
                                })

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
                        topics = ["tickers.VELVETUSDT", "tickers.BTCUSDT", "tickers.ETHUSDT", "tickers.ACEUSDT",
                                  "tickers.AKEUSDT", "tickers.APRUSDT", "tickers.BEATUSDT", "tickers.BTWUSDT",
                                  "tickers.COWUSDT", "tickers.CYSUSDT", "tickers.HEMIUSDT", "tickers.TUTUSDT",
                                  "tickers.SPORTFUNUSDT", "tickers.HYPEUSDT", "tickers.BICOUSDT", "tickers.WALUSDT"]
                        
                        await ws.send_str(json.dumps({"op": "subscribe", "args": topics}))
                        logger.info(f"📡 [Bybit Ticker Stream] {len(topics)}개 심볼 가격 스트림 연결 성공!")

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
                                        if sym not in self.price_history_5m:
                                            self.price_history_5m[sym] = deque(maxlen=600)
                                        self.price_history_5m[sym].append((now, p_float))

                                        await self.broadcast({
                                            "type": "TICKER",
                                            "symbol": sym,
                                            "price": p_float,
                                            "time": now
                                        })
            except Exception as e:
                logger.error(f"Bybit Ticker 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_account_sync_loop(self):
        """계좌 잔고 및 포지션 2초 주기 실시간 동기화"""
        while self.is_running:
            try:
                if self.ws_clients:
                    balance = self.trader.get_wallet_balance()
                    positions = self.trader.get_positions()
                    await self.broadcast({
                        "type": "ACCOUNT_UPDATE",
                        "balance": balance,
                        "positions": positions
                    })
            except Exception as e:
                logger.error(f"계좌 동기화 에러: {e}")
            await asyncio.sleep(2.0)

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"🚀 [Cascade Pro Trading Suite] 웹 서버 가동 완료: http://localhost:{self.port}")

        await asyncio.gather(
            self.run_liquidation_stream(),
            self.run_ticker_stream(),
            self.run_account_sync_loop()
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
        sys.exit(0)


if __name__ == "__main__":
    main()
