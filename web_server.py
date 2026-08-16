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
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
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
        cursor = ""
        count = 0
        try:
            while True:
                url = f"{BYBIT_REST_URL}/v5/market/instruments-info?category=linear&limit=1000" + (f"&cursor={cursor}" if cursor else "")
                with urllib.request.urlopen(url, timeout=5) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    items = data.get("result", {}).get("list", [])
                    for item in items:
                        sym = item.get("symbol")
                        lsf = item.get("lotSizeFilter", {})
                        pf = item.get("priceFilter", {})
                        lf = item.get("leverageFilter", {})
                        step_str = lsf.get("qtyStep", "0.001")
                        min_q_str = lsf.get("minOrderQty", "0.001")
                        tick_str = pf.get("tickSize", "0.0001")
                        min_notional = _safe_float(lsf.get("minNotionalValue"), 5.0)
                        max_lev = _safe_float(lf.get("maxLeverage"), 20.0)

                        self.symbol_specs[sym] = {
                            "min_qty": _safe_float(min_q_str, 0.001),
                            "qty_step": _safe_float(step_str, 0.001),
                            "min_qty_str": min_q_str,
                            "qty_step_str": step_str,
                            "tick_size_str": tick_str,
                            "min_notional": min_notional,
                            "tick_size": _safe_float(tick_str, 0.0001),
                            "max_leverage": max_lev,
                            "max_leverage_str": str(int(max_lev) if max_lev.is_integer() else max_lev),
                        }
                        count += 1
                    cursor = data.get("result", {}).get("nextPageCursor", "")
                    if not cursor or not items:
                        break
            logger.info(f"📐 [Bybit 메타데이터 동기화] 총 {len(self.symbol_specs)}개 전 종목 규격 및 최대 레버리지 캐싱 완료")
        except Exception as e:
            logger.error(f"심볼 메타데이터 로드 실패: {e}")

    def get_symbol_spec(self, symbol: str) -> Dict[str, Any]:
        if symbol in self.symbol_specs:
            return self.symbol_specs[symbol]
        # 온디맨드 단일 심볼 조회
        try:
            url = f"{BYBIT_REST_URL}/v5/market/instruments-info?category=linear&symbol={symbol}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                items = data.get("result", {}).get("list", [])
                if items:
                    it = items[0]
                    lsf = it.get("lotSizeFilter", {})
                    pf = it.get("priceFilter", {})
                    lf = it.get("leverageFilter", {})
                    step_str = lsf.get("qtyStep", "0.001")
                    min_q_str = lsf.get("minOrderQty", "0.001")
                    tick_str = pf.get("tickSize", "0.0001")
                    min_notional = _safe_float(lsf.get("minNotionalValue"), 5.0)
                    max_lev = _safe_float(lf.get("maxLeverage"), 20.0)
                    spec = {
                        "min_qty": _safe_float(min_q_str, 0.001),
                        "qty_step": _safe_float(step_str, 0.001),
                        "min_qty_str": min_q_str,
                        "qty_step_str": step_str,
                        "tick_size_str": tick_str,
                        "min_notional": min_notional,
                        "tick_size": _safe_float(tick_str, 0.0001),
                        "max_leverage": max_lev,
                        "max_leverage_str": str(int(max_lev) if max_lev.is_integer() else max_lev),
                    }
                    self.symbol_specs[symbol] = spec
                    return spec
        except Exception:
            pass
        return {
            "min_qty": 0.001, "qty_step": 0.001, "min_qty_str": "0.001",
            "qty_step_str": "0.001", "tick_size_str": "0.0001", "min_notional": 5.0, "tick_size": 0.0001,
            "max_leverage": 20.0, "max_leverage_str": "20"
        }

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

    def place_market_order(self, symbol: str, side: str, order_value_usdt: float, leverage: Optional[float] = None, tp_pct: float = 2.0, sl_pct: float = 0.6) -> Dict[str, Any]:
        spec = self.get_symbol_spec(symbol)
        max_lev = float(spec.get("max_leverage", 20.0))

        # 사용자 요청: 해당 심볼의 바이비트 최대 레버리지로 투자 (기본/미지정 시 최대치 적용)
        if leverage is None or leverage <= 0 or leverage == 15.0:
            effective_leverage = max_lev
        else:
            effective_leverage = min(float(leverage), max_lev)

        lev_str = str(int(effective_leverage) if effective_leverage.is_integer() else effective_leverage)
        self._request("POST", "/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol, "buyLeverage": lev_str, "sellLeverage": lev_str
        })

        # 1. 가용 잔고(Available Balance) 자동 보정 (초과 주문 시 가용 잔고 내로 자동 스케일링)
        try:
            wb = self.get_wallet_balance()
            avail = float(wb.get("availableBalance", 0.0))
            if avail > 0.1 and order_value_usdt > avail:
                order_value_usdt = max(0.5, round(avail * 0.95, 2))
                logger.info(f"💡 [{symbol}] 가용 잔고(${avail:.2f}) 초과 주문 감지 ➔ 주문 증거금 ${order_value_usdt:.2f}로 자동 보정")
        except Exception:
            pass

        t_res = self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        last_price = 0.0
        if t_res.get("retCode") == 0:
            last_price = float(t_res.get("result", {}).get("list", [{}])[0].get("lastPrice", 0.0))
        
        if last_price <= 0:
            return {"retCode": -1, "retMsg": "Invalid price"}

        step_str = str(spec.get("qty_step_str", "0.001"))
        min_q_str = str(spec.get("min_qty_str", "0.001"))
        tick_str = str(spec.get("tick_size_str", "0.0001"))
        min_notional = float(spec.get("min_notional", 5.0))
        min_qty = float(spec.get("min_qty", 0.001))

        # 가격 및 레버리지에 맞춘 동적 $6.0 거래대금(Notional) 및 최소 수량 보장 연산
        min_safe_notional = max(6.0, min_notional, (min_qty * last_price))
        target_notional = max(order_value_usdt * effective_leverage, min_safe_notional)

        raw_qty = target_notional / last_price

        # Decimal 정밀 스텝 연산
        step_d = Decimal(step_str)
        min_q_d = Decimal(min_q_str)
        raw_d = Decimal(str(raw_qty))

        steps = (raw_d / step_d).quantize(Decimal('1'), rounding=ROUND_UP)
        qty_d = steps * step_d
        if qty_d < min_q_d:
            qty_d = min_q_d

        # 최소 $6.0 거래대금(Notional) 하드 보장
        while (float(qty_d) * last_price) < min_safe_notional:
            qty_d += step_d

        # 정수(e.g. 1)면 "3111", 소수(e.g. 0.001)면 "0.001"로 완벽 포맷
        if '.' in step_str:
            dec_places = len(step_str.split('.')[1])
            qty_str = f"{qty_d:.{dec_places}f}"
        else:
            qty_str = str(int(qty_d))

        # TP / SL 가격 포맷팅
        tick_d = Decimal(tick_str)
        last_px_d = Decimal(str(last_price))
        if side == "Buy":
            tp_px = last_px_d * (Decimal('1') + Decimal(str(tp_pct / 100.0)))
            sl_px = last_px_d * (Decimal('1') - Decimal(str(sl_pct / 100.0)))
        else:
            tp_px = last_px_d * (Decimal('1') - Decimal(str(tp_pct / 100.0)))
            sl_px = last_px_d * (Decimal('1') + Decimal(str(sl_pct / 100.0)))

        tp_px = (tp_px / tick_d).quantize(Decimal('1'), rounding=ROUND_UP) * tick_d
        sl_px = (sl_px / tick_d).quantize(Decimal('1'), rounding=ROUND_UP) * tick_d
        
        if '.' in tick_str:
            tick_places = len(tick_str.split('.')[1])
            tp_str = f"{tp_px:.{tick_places}f}"
            sl_str = f"{sl_px:.{tick_places}f}"
        else:
            tp_str = str(int(tp_px))
            sl_str = str(int(sl_px))

        logger.info(f"🛒 [주문 생성] {symbol} {side} 수량: {qty_str} (약 ${float(qty_d)*last_price:,.2f} Notional / {lev_str}x 최대 레버리지) | 진입: ${last_price} | TP: {tp_str} | SL: {sl_str}")

        order_params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "timeInForce": "IOC",
            "takeProfit": tp_str,
            "stopLoss": sl_str,
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market"
        }

        logger.info(f"🛒 [주문 생성] {symbol} {side} 수량: {qty_str} (약 ${float(qty_d)*last_price:,.2f} Notional) | 진입: ${last_price} | TP: {tp_str} | SL: {sl_str}")
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


class InMemoryLiquidationManager:
    """초고속 무간섭 In-Memory DuckDB 기반 청산 데이터 분석, 적정 타임프레임 산출 및 퀀트 인사이트 엔진"""
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.inmem_conn = duckdb.connect(':memory:')
        self.is_initialized = False
        self.last_sync_time = 0.0
        self.last_max_ts = None
        self._init_memory_schema()

    def _init_memory_schema(self):
        self.inmem_conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidations (
                exchange VARCHAR(20),
                symbol VARCHAR(20),
                exec_time TIMESTAMP_MS,
                side TINYINT,
                pos_side VARCHAR(10),
                price DOUBLE,
                size DOUBLE,
                notional_usd DOUBLE
            );
        """)
        self.inmem_conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_sym_time ON liquidations (symbol, exec_time);")
        self.inmem_conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_ex_sym ON liquidations (exchange, symbol);")

    def sync_from_disk_snapshot(self):
        """디스크 DuckDB 스냅샷을 인메모리 테이블로 초고속 동기화 (Zero CPU Lock)"""
        if not os.path.exists(self.db_path):
            return
        td = tempfile.mkdtemp()
        try:
            snap_db = os.path.join(td, 'snap.duckdb')
            shutil.copy2(self.db_path, snap_db)
            conn = duckdb.connect(snap_db, read_only=True)
            
            if self.last_max_ts:
                query = f"SELECT * FROM liquidations WHERE exec_time > '{self.last_max_ts}';"
                df_new = conn.execute(query).df()
            else:
                df_new = conn.execute("SELECT * FROM liquidations;").df()
            conn.close()

            if not df_new.empty:
                max_ts = df_new['exec_time'].max()
                if max_ts:
                    self.last_max_ts = str(max_ts)
                self.inmem_conn.register('temp_new_liqs', df_new)
                self.inmem_conn.execute("INSERT INTO liquidations SELECT * FROM temp_new_liqs;")
                self.inmem_conn.unregister('temp_new_liqs')
                total_cnt = self.inmem_conn.execute('SELECT count(*) FROM liquidations').fetchone()[0]
                logger.info(f"⚡ [In-Memory Liq Engine] {len(df_new):,}건 청산 데이터 RAM 적재 완료 (총 {total_cnt:,}건 보존)")
            self.is_initialized = True
            self.last_sync_time = time.time()
        except Exception as e:
            logger.error(f"In-Memory 청산 동기화 에러: {e}")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def add_live_event(self, event_dict: Dict[str, Any]):
        """실시간 유입 청산 건을 0.02ms 이내로 인메모리 테이블에 즉시 삽입"""
        try:
            ex = event_dict.get("exchange", "bybit")
            sym = event_dict.get("symbol", "BTCUSDT")
            ts = event_dict.get("timestamp", time.time() * 1000)
            if ts < 10000000000:
                ts = ts * 1000.0
            exec_dt = datetime.fromtimestamp(ts / 1000.0)
            is_long = event_dict.get("pos_side") == "long" or event_dict.get("is_long_liquidation") or event_dict.get("side") == 2
            side_val = 2 if is_long else 1
            pos_side = "long" if is_long else "short"
            price = float(event_dict.get("price", 0.0))
            size = float(event_dict.get("size", 0.0))
            usd = float(event_dict.get("notional_usd", 0.0))

            self.inmem_conn.execute("""
                INSERT INTO liquidations VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, [ex, sym, exec_dt, side_val, pos_side, price, size, usd])
        except Exception as e:
            logger.error(f"In-Memory 라이브 청산 삽입 에러: {e}")

    def query_analytics_with_insight(self, timeframe: str = "auto", exchange: str = "all", symbol: Optional[str] = None) -> Dict[str, Any]:
        """RAM 기반 초고속(<2ms) 청산 분석, 최적 타임프레임 자동 산출 및 피크 클러스터 퀀트 인사이트 반환"""
        where_clauses = []
        if exchange and exchange != "all":
            where_clauses.append(f"exchange = '{exchange}'")
        if symbol:
            where_clauses.append(f"symbol = '{symbol.upper()}'")
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # 1. Summary & Timespan query
        meta_sql = f"""
            SELECT 
                count(*) as total_count,
                COALESCE(sum(notional_usd), 0.0) as total_usd,
                COALESCE(sum(CASE WHEN side = 2 THEN notional_usd ELSE 0 END), 0.0) as long_usd,
                COALESCE(sum(CASE WHEN side = 1 THEN notional_usd ELSE 0 END), 0.0) as short_usd,
                COALESCE(sum(CASE WHEN exchange = 'binance' THEN notional_usd ELSE 0 END), 0.0) as binance_usd,
                COALESCE(sum(CASE WHEN exchange = 'bybit' THEN notional_usd ELSE 0 END), 0.0) as bybit_usd,
                COALESCE(sum(CASE WHEN exchange = 'okx' THEN notional_usd ELSE 0 END), 0.0) as okx_usd,
                epoch(min(exec_time)) as min_epoch,
                epoch(max(exec_time)) as max_epoch,
                count(DISTINCT symbol) as symbol_count
            FROM liquidations {where_sql};
        """
        meta_df = self.inmem_conn.execute(meta_sql).df()
        summary = meta_df.to_dict(orient='records')[0] if not meta_df.empty else {}

        tot_usd = float(summary.get("total_usd", 0.0) or 0.0)
        tot_count = int(summary.get("total_count", 0) or 0)
        long_usd = float(summary.get("long_usd", 0.0) or 0.0)
        short_usd = float(summary.get("short_usd", 0.0) or 0.0)
        min_epoch = summary.get("min_epoch")
        max_epoch = summary.get("max_epoch")

        # 2. Optimal Timeframe calculation
        effective_tf = timeframe
        opt_reason = ""
        span_hours = 0.0

        if min_epoch and max_epoch and not pd.isna(min_epoch) and not pd.isna(max_epoch):
            span_hours = max(0.1, (float(max_epoch) - float(min_epoch)) / 3600.0)

            if timeframe == "auto" or not timeframe:
                if span_hours <= 0.6:
                    effective_tf = "1m"
                elif span_hours <= 4.0:
                    effective_tf = "5m"
                elif span_hours <= 16.0:
                    effective_tf = "15m"
                else:
                    effective_tf = "1h"
                opt_reason = f"최근 {span_hours:.1f}시간 동안 {tot_count:,}건 발생 분포에 최적화된 {effective_tf.upper()} 버킷 자동 채택"
            else:
                effective_tf = timeframe
                opt_reason = f"수동 선택된 {effective_tf.upper()} 타임프레임 ({span_hours:.1f}시간 범위)"
        else:
            if timeframe == "auto" or not timeframe:
                effective_tf = "5m"
            opt_reason = "기본 5분 버킷 (실시간 감시 모드)"

        interval_map = {
            "1m": "1 minute",
            "3m": "3 minutes",
            "5m": "5 minutes",
            "15m": "15 minutes",
            "30m": "30 minutes",
            "1h": "1 hour",
            "4h": "4 hours",
            "24h": "1 day"
        }
        interval_str = interval_map.get(effective_tf, "5 minutes")

        # 3. Time-Rate Distribution Series
        # 3. Time-Rate Distribution & Price OHLC Series
        time_sql = f"""
            SELECT 
                strftime(time_bucket(INTERVAL '{interval_str}', exec_time), '%H:%M') as time_str,
                epoch(time_bucket(INTERVAL '{interval_str}', exec_time)) * 1000 as timestamp,
                COALESCE(sum(CASE WHEN side = 2 THEN notional_usd ELSE 0 END), 0.0) as long_usd,
                COALESCE(sum(CASE WHEN side = 1 THEN notional_usd ELSE 0 END), 0.0) as short_usd,
                COALESCE(sum(CASE WHEN exchange = 'binance' THEN notional_usd ELSE 0 END), 0.0) as bin_usd,
                COALESCE(sum(CASE WHEN exchange = 'bybit' THEN notional_usd ELSE 0 END), 0.0) as byb_usd,
                COALESCE(sum(CASE WHEN exchange = 'okx' THEN notional_usd ELSE 0 END), 0.0) as okx_usd,
                COALESCE(sum(notional_usd), 0.0) as total_usd,
                count(*) as count,
                avg(price) as avg_price,
                min(price) as low_price,
                max(price) as high_price,
                arg_min(price, exec_time) as open_price,
                arg_max(price, exec_time) as close_price
            FROM liquidations {where_sql}
            GROUP BY 1, 2
            ORDER BY timestamp ASC;
        """
        time_series = self.inmem_conn.execute(time_sql).df().to_dict(orient='records')

        # 4. Peak Cluster & Insight Engine
        peak_cluster = {}
        quant_insight = {}
        if time_series and tot_usd > 0:
            peak_row = max(time_series, key=lambda x: x.get("total_usd", 0.0))
            peak_usd = peak_row.get("total_usd", 0.0)
            peak_pct = round((peak_usd / tot_usd * 100.0), 1) if tot_usd > 0 else 0.0
            peak_is_long = peak_row.get("long_usd", 0.0) >= peak_row.get("short_usd", 0.0)
            peak_side_kr = "🔴 롱 청산 집중" if peak_is_long else "🟢 숏 청산 집중"

            long_ratio = round((long_usd / tot_usd * 100.0), 1)
            bias_kr = "🔴 롱 청산 우세 (하방 압력 급증)" if long_ratio >= 65.0 else ("🟢 숏 청산 우세 (상방 압력 급증)" if long_ratio <= 35.0 else "⚖️ 롱/숏 균형 공방")

            peak_cluster = {
                "time_str": peak_row.get("time_str", "--:--"),
                "timestamp": peak_row.get("timestamp", 0),
                "peak_usd": peak_usd,
                "peak_pct": peak_pct,
                "peak_side": "long" if peak_is_long else "short",
                "peak_side_kr": peak_side_kr,
                "count": peak_row.get("count", 0)
            }

            target_name = symbol.upper() if symbol else "전체 시장"
            quant_insight = {
                "headline": f"[{target_name}] {peak_row.get('time_str')}에 최대 청산 폭발 (${peak_usd:,.0f}, {peak_pct}% 집중) — {bias_kr}",
                "bias": bias_kr,
                "long_ratio": long_ratio,
                "short_ratio": round(100.0 - long_ratio, 1),
                "optimal_tf": effective_tf,
                "optimal_reason": opt_reason,
                "action_strategy": "피크 청산 구간 후 CVD 양전환 확인 시 기술적 반등 스캘핑 타점, 또는 지지선 붕괴 지속 시 청산 숏 추종 유효"
            }
        else:
            quant_insight = {
                "headline": f"[{symbol or '전체'}] 최근 청산 데이터 집계 중 (실시간 감시 활성화)",
                "bias": "⚖️ 대기 중",
                "long_ratio": 50.0,
                "short_ratio": 50.0,
                "optimal_tf": effective_tf,
                "optimal_reason": opt_reason,
                "action_strategy": "신규 청산 도화선 점화 대기 중"
            }

        # 5. Top Symbols (if querying whole market)
        top_syms = []
        if not symbol:
            sym_sql = f"""
                SELECT 
                    symbol,
                    count(*) as count,
                    COALESCE(sum(notional_usd), 0.0) as total_usd,
                    COALESCE(sum(CASE WHEN side = 2 THEN notional_usd ELSE 0 END), 0.0) as long_usd,
                    COALESCE(sum(CASE WHEN side = 1 THEN notional_usd ELSE 0 END), 0.0) as short_usd,
                    COALESCE(sum(CASE WHEN exchange = 'binance' THEN notional_usd ELSE 0 END), 0.0) as bin_usd,
                    COALESCE(sum(CASE WHEN exchange = 'bybit' THEN notional_usd ELSE 0 END), 0.0) as byb_usd,
                    COALESCE(sum(CASE WHEN exchange = 'okx' THEN notional_usd ELSE 0 END), 0.0) as okx_usd
                FROM liquidations {where_sql}
                GROUP BY symbol
                ORDER BY total_usd DESC
                LIMIT 50;
            """
            top_syms = self.inmem_conn.execute(sym_sql).df().to_dict(orient='records')

        # 6. Recent records
        rec_sql = f"""
            SELECT 
                exchange,
                symbol,
                epoch(exec_time) * 1000 as timestamp,
                strftime(exec_time, '%H:%M:%S') as time_str,
                CASE WHEN side = 2 THEN 'long' ELSE 'short' END as pos_side,
                price,
                size,
                notional_usd
            FROM liquidations {where_sql}
            ORDER BY exec_time DESC
            LIMIT 200;
        """
        recent_records = self.inmem_conn.execute(rec_sql).df().to_dict(orient='records')

        # 7. Exchange Shares
        exchange_shares = {
            "binance": {
                "usd": summary.get("binance_usd", 0.0),
                "pct": round((summary.get("binance_usd", 0.0) / tot_usd * 100.0), 1) if tot_usd > 0 else 0.0
            },
            "bybit": {
                "usd": summary.get("bybit_usd", 0.0),
                "pct": round((summary.get("bybit_usd", 0.0) / tot_usd * 100.0), 1) if tot_usd > 0 else 0.0
            },
            "okx": {
                "usd": summary.get("okx_usd", 0.0),
                "pct": round((summary.get("okx_usd", 0.0) / tot_usd * 100.0), 1) if tot_usd > 0 else 0.0
            }
        }

        return {
            "timeframe": effective_tf,
            "interval_str": interval_str,
            "exchange": exchange,
            "symbol": symbol,
            "summary": summary,
            "exchange_shares": exchange_shares,
            "symbol_rankings": top_syms,
            "time_series": time_series,
            "recent_records": recent_records,
            "peak_cluster": peak_cluster,
            "quant_insight": quant_insight,
            "server_time": time.time()
        }


class CVDSlopeTracker:
    """
    실시간 멀티 심볼 듀얼 거래소 CVD 기울기 및 가속도 피크 감지 엔진 (백테스트 4대 특징 검증 적용)
    - 1. 직전 60초 극단적 변동성 압축(Volatility Squeeze, Range <= 1.25%) 감지
    - 2. 발발 CVD 가속도 폭발 배율(2.8배+ Acceleration Ratio) 정밀 측정
    - 3. 가격-거래량 '가성비 붕괴' 교차 검증 (아이스버그 숏 트랩 / 지지선 흡수 롱 스퀴즈)
    - 4. 바이낸스 0.9초 선행 도화선 ➔ 바이비트 전이 연계 트래킹
    """
    def __init__(self):
        self.history: Dict[tuple, deque] = {}
        self.cum_cvd: Dict[tuple, float] = {}
        self.last_alert_time: Dict[tuple, float] = {}
        self.binance_lead_sparks: Dict[str, tuple] = {}  # symbol -> (slope, timestamp, insight)
        self.cooldown_sec = 3.0  # 동일 심볼 3.0초 쿨다운

    def push_delta(self, exchange: str, symbol: str, delta_usd: float, price: float, now: float) -> Optional[Dict[str, Any]]:
        key = (exchange, symbol)
        if key not in self.cum_cvd:
            self.cum_cvd[key] = 0.0
            self.history[key] = deque(maxlen=60)

        self.cum_cvd[key] += delta_usd
        self.history[key].append((now, self.cum_cvd[key], price))

        hist = self.history[key]
        if len(hist) < 5:
            return None

        # 3초 전 데이터 탐색 (초단기 기울기)
        t_3s_ago = now - 3.0
        idx_3s = 0
        for i, (t, c, p) in enumerate(hist):
            if t >= t_3s_ago:
                idx_3s = i
                break

        dt_3s = max(0.5, now - hist[idx_3s][0])
        slope_3s = (self.cum_cvd[key] - hist[idx_3s][1]) / dt_3s  # USD per second

        # 15초 전 데이터 탐색 (가격 변동률 및 가성비 대조)
        t_15s_ago = now - 15.0
        idx_15s = 0
        for i, (t, c, p) in enumerate(hist):
            if t >= t_15s_ago:
                idx_15s = i
                break
        start_p = hist[idx_15s][2]
        dp_pct = ((price - start_p) / start_p * 100.0) if start_p > 0 else 0.0

        # [특징 1] 직전 60초 변동성 압축률 (Volatility Squeeze)
        prices_60s = [p for _, _, p in hist]
        min_p_60s = min(prices_60s)
        max_p_60s = max(prices_60s)
        range_pct_60s = ((max_p_60s - min_p_60s) / min_p_60s * 100.0) if min_p_60s > 0 else 0.0
        is_squeeze = range_pct_60s <= 1.25

        # [특징 2] CVD 가속도 폭발 배율 (Acceleration Multiplier)
        dt_total = max(1.0, now - hist[0][0])
        cum_delta_60s = abs(self.cum_cvd[key] - hist[0][1])
        avg_sec_cvd = cum_delta_60s / dt_total
        accel_ratio = round(abs(slope_3s) / max(300.0, avg_sec_cvd), 1) if avg_sec_cvd > 0 else 1.0

        # 최근 1초 델타 기반 표준편차(Z-score) 계산
        if len(hist) >= 10:
            step_slopes = [
                (hist[i][1] - hist[i-1][1]) / max(0.1, hist[i][0] - hist[i-1][0])
                for i in range(1, len(hist))
            ]
            mean_s = sum(step_slopes) / len(step_slopes)
            var_s = sum((s - mean_s) ** 2 for s in step_slopes) / len(step_slopes)
            std_s = math.sqrt(var_s) if var_s > 0 else 1000.0
            z_score = (slope_3s - mean_s) / max(500.0, std_s)
        else:
            z_score = 0.0

        # 임계값: |Z| >= 2.0 및 최소 초당 $2,000 델타 이상
        abs_slope = abs(slope_3s)
        if abs(z_score) >= 2.0 and abs_slope >= 2000.0:
            if now - self.last_alert_time.get(key, 0.0) < self.cooldown_sec:
                return None
            self.last_alert_time[key] = now

            is_buy = slope_3s > 0
            side = "buy" if is_buy else "sell"

            # [특징 4] 바이낸스 0.9s 선행 도화선 트래킹
            is_bin_spark = False
            is_byb_transition = False
            if exchange == "binance":
                self.binance_lead_sparks[symbol] = (slope_3s, now, side)
                is_bin_spark = True
            elif exchange == "bybit":
                if symbol in self.binance_lead_sparks:
                    b_slope, b_time, b_side = self.binance_lead_sparks[symbol]
                    if now - b_time <= 4.0 and b_side == side:
                        is_byb_transition = True

            # [특징 3] 가격-거래량 가성비 4대 퀀트 진단
            if is_buy:
                if dp_pct >= 0.05:
                    insight = f"🚀 진성 돌파 ({accel_ratio}x 가속)" + (" [수렴탈출]" if is_squeeze else "")
                elif dp_pct <= 0.015:
                    insight = f"🪤 숏 트랩 (매수흡수·윗벽)" + (" [수렴]" if is_squeeze else "")
                else:
                    insight = f"🟢 순매수 급증 ({accel_ratio}x)"
            else:
                if dp_pct <= -0.05:
                    insight = f"🔴 파열 덤핑 ({accel_ratio}x 가속)" + (" [수렴이탈]" if is_squeeze else "")
                elif dp_pct >= -0.015:
                    insight = f"⚠️ 지지선 흡수 중 (붕괴주의)"
                else:
                    insight = f"🔴 순매도 급증 ({accel_ratio}x)"

            if is_byb_transition:
                insight = "⚡ [선행전이] " + insight
            elif is_bin_spark:
                insight = " 도화선 " + insight

            return {
                "exchange": exchange,
                "symbol": symbol,
                "slope_usd_sec": round(slope_3s, 1),
                "z_score": round(z_score, 1),
                "side": side,
                "price": price,
                "dp_pct": round(dp_pct, 2),
                "accel_ratio": accel_ratio,
                "is_squeeze": is_squeeze,
                "is_lead_lag": is_byb_transition or is_bin_spark,
                "insight": insight,
                "time": int(now * 1000)
            }

        return None

        return None


@web.middleware
async def no_cache_middleware(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


class CascadeTradingServer:
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application(middlewares=[no_cache_middleware])
        self.trader = BybitTradingService(CRED_PATH)
        self.liq_manager = InMemoryLiquidationManager(DB_PATH)
        self.cvd_slope_tracker = CVDSlopeTracker()
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
        self.trigger_history_by_sym: Dict[str, deque] = {}
        # 초기 기본 20개 동시 상장 심볼
        self.top20_symbols: List[str] = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "COWUSDT", "CYSUSDT",
            "ACEUSDT", "AKEUSDT", "HYPEUSDT", "HEMIUSDT", "XRPUSDT",
            "BEATUSDT", "HUSDT", "LINKUSDT", "VELVETUSDT", "APRUSDT",
            "DOGEUSDT", "ZECUSDT", "ADAUSDT", "WALUSDT", "WLDUSDT"
        ]
        self.subscribed_ticker_symbols: Set[str] = set(self.top20_symbols)

        # 초기 메모리 적재
        self.liq_manager.sync_from_disk_snapshot()

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
                    for s in added:
                        spec = self.trader.get_symbol_spec(s)
                        logger.info(f"🔍 [신규 심볼 규격 검증] {s}: 최대레버리지 {spec.get('max_leverage')}x | minQty {spec.get('min_qty')} | qtyStep {spec.get('qty_step')} | minNotional ${spec.get('min_notional')}")
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
        spec = self.trader.get_symbol_spec(symbol)
        logger.info(f"🔍 [온디맨드 심볼 규격 검증] {symbol}: 최대레버리지 {spec.get('max_leverage')}x | minQty {spec.get('min_qty')} | qtyStep {spec.get('qty_step')}")
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
        self.app.router.add_get("/api/trigger_history", self.handle_api_trigger_history)
        self.app.router.add_get("/api/liquidations/analytics", self.handle_api_liquidation_analytics)
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
            "latest_prices": self.latest_prices,
            "max_leverages": {s: sp.get("max_leverage", 20.0) for s, sp in self.trader.symbol_specs.items()}
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

        # In-memory trigger history for this symbol
        trig_list = list(self.trigger_history_by_sym.get(symbol, []))
        spec = self.trader.get_symbol_spec(symbol)
        max_lev = spec.get("max_leverage", 20.0)
        max_lev_str = spec.get("max_leverage_str", "20")

        return web.json_response({
            "symbol": symbol,
            "candles": chart_data["candles"],
            "trades": chart_data["trades"],
            "liquidations": liq_data,
            "triggers": trig_list,
            "max_leverage": max_lev,
            "max_leverage_str": max_lev_str
        })

    async def handle_api_trigger_history(self, request: web.Request) -> web.Response:
        """심볼별 또는 전 종목 최근 트리거 히스토리 반환"""
        symbol = request.query.get("symbol")
        if symbol:
            symbol = symbol.upper()
            triggers = list(self.trigger_history_by_sym.get(symbol, []))
        else:
            all_trigs = []
            for s, q in self.trigger_history_by_sym.items():
                all_trigs.extend(list(q))
            all_trigs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            triggers = all_trigs[:100]
        return web.json_response({"symbol": symbol, "triggers": triggers})

    async def handle_api_liquidation_analytics(self, request: web.Request) -> web.Response:
        """3대 거래소 실시간 및 히스토리 청산 데이터 심층 분석 및 퀀트 인사이트 반환 (<2ms RAM Query)"""
        timeframe = request.query.get("timeframe", "auto")
        exchange = request.query.get("exchange", "all")
        symbol = request.query.get("symbol")
        data = await asyncio.to_thread(self.liq_manager.query_analytics_with_insight, timeframe, exchange, symbol)
        return web.json_response(data, dumps=lambda x: orjson.dumps(x).decode('utf-8'))

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
            self.ws_clients.discard(ws)
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
                        now = time.time()
                        event_dict = event.to_dict()
                        
                        # 전 종목 청산 데이터 심볼별 인메모리 큐 영구 보존 및 초고속 In-Memory DB 삽입
                        if sym not in self.recent_liquidations_by_sym:
                            self.recent_liquidations_by_sym[sym] = deque(maxlen=200)
                        self.recent_liquidations_by_sym[sym].append(event_dict)
                        self.liq_manager.add_live_event(event_dict)

                        # 2. 💥 [연쇄 폭포수 전이 레이더 피드] 상위 거래대금 TOP 20종만 엄격 필터링
                        if self.top20_symbols and (sym not in self.top20_symbols and sym not in self.subscribed_ticker_symbols):
                            continue

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

                                    # CVD 스냅샷 및 방향 판단
                                    cur_cvd = self._cvd_deltas.get(sym, {})
                                    bin_cvd = cur_cvd.get("binance", 0.0)
                                    byb_cvd = cur_cvd.get("bybit", 0.0)
                                    cur_price = self.latest_prices.get(sym, event.price)

                                    if bin_cvd < 0 and byb_cvd < 0:
                                        cvd_trend = "STRONG_SELL"
                                        cvd_desc = "🌊 양사 순매도 (숏 일치)"
                                    elif bin_cvd > 0 and byb_cvd > 0:
                                        cvd_trend = "STRONG_BUY"
                                        cvd_desc = "🌊 양사 순매수 (롱 일치)"
                                    elif bin_cvd < 0 and byb_cvd >= 0:
                                        cvd_trend = "DIV_BIN_SELL"
                                        cvd_desc = "⚠️ BIN 매도 / BYB 매수"
                                    else:
                                        cvd_trend = "DIV_BYB_SELL"
                                        cvd_desc = "⚠️ BIN 매수 / BYB 매도"

                                    trig_record = {
                                        "id": f"trig_{sym}_{int(now*1000)}",
                                        "symbol": sym,
                                        "timestamp": int(now * 1000),
                                        "time_str": time.strftime("%H:%M:%S", time.localtime(now)),
                                        "target_side": target_side, # "Sell" or "Buy"
                                        "target_side_kr": "🔴 숏 (SHORT)" if target_side == "Sell" else "🟢 롱 (LONG)",
                                        "trigger_price": cur_price,
                                        "binance_usd": bin_liq["usd"],
                                        "bybit_usd": event.notional_usd,
                                        "lag_sec": lag_sec,
                                        "binance_cvd": bin_cvd,
                                        "bybit_cvd": byb_cvd,
                                        "cvd_trend": cvd_trend,
                                        "cvd_desc": cvd_desc,
                                        "post_eval": None
                                    }

                                    if sym not in self.trigger_history_by_sym:
                                        self.trigger_history_by_sym[sym] = deque(maxlen=50)
                                    self.trigger_history_by_sym[sym].appendleft(trig_record)

                                    cascade_data["trigger_record"] = trig_record

                                    # 사후 가격 반응 평가 비동기 태스크 가동
                                    asyncio.create_task(self._evaluate_trigger_outcome(trig_record))

                                    logger.info(f"💥 [연쇄 청산 격발!] {sym} (Binance ${bin_liq['usd']:,.0f} ➔ Bybit ${event.notional_usd:,.0f} | {lag_sec}s 전이) [권장: {trig_record['target_side_kr']}] [CVD: {cvd_desc}]")
                                    await self.broadcast({
                                        "type": "CASCADE_BURST",
                                        "cascade": cascade_data,
                                        "trigger": trig_record
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
                    async with session.ws_connect(BYBIT_WS_URL, heartbeat=20.0) as ws:
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
                async with self._http_session.ws_connect(url, heartbeat=15.0) as ws:
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

                                # 🌊 Binance CVD 급격한 기울기 피크 감지
                                peak_ev = self.cvd_slope_tracker.push_delta("binance", sym, delta, p, time.time())
                                if peak_ev:
                                    await self.broadcast({
                                        "type": "CVD_SLOPE_PEAK",
                                        "peak": peak_ev
                                    })
            except Exception as e:
                logger.error(f"Binance Trade Stream 에러: {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def run_bybit_trade_stream(self):
        """바이비트 Linear 선물 20종 실시간 체결(publicTrade) 스트림 ➔ CVD 델타 집계"""
        while self.is_running:
            try:
                async with self._http_session.ws_connect(BYBIT_WS_URL, heartbeat=20.0) as ws:
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

                                    # 🌊 Bybit CVD 급격한 기울기 피크 감지
                                    peak_ev = self.cvd_slope_tracker.push_delta("bybit", sym, delta, p, time.time())
                                    if peak_ev:
                                        await self.broadcast({
                                            "type": "CVD_SLOPE_PEAK",
                                            "peak": peak_ev
                                        })
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
        """실시간 포지션 감시 및 경과시간 추적 엔진"""
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

                # 종료된 포지션의 엔트리 타임 정리
                for sym in list(self.position_entry_times.keys()):
                    if sym not in active_symbols_in_pos:
                        self.position_entry_times.pop(sym, None)

            except Exception as e:
                logger.error(f"포지션 가드 루프 에러: {e}")

            await asyncio.sleep(0.5)

    async def _evaluate_trigger_outcome(self, rec: Dict[str, Any]):
        """트리거 발동 후 10초 / 30초 시점의 가격 방향성 및 시그널 적중률 평가"""
        sym = rec.get("symbol")
        start_px = float(rec.get("trigger_price", 0.0))
        side = rec.get("target_side", "Sell")
        if start_px <= 0:
            return

        # 10초 후 1차 체크
        await asyncio.sleep(10)
        px_10s = float(self.latest_prices.get(sym, start_px))
        diff_pct_10s = ((px_10s - start_px) / start_px) * 100.0 if start_px > 0 else 0.0
        hit_10s = (diff_pct_10s < -0.02) if side == "Sell" else (diff_pct_10s > 0.02)

        # 30초 후 최종 체크 (추가 20초)
        await asyncio.sleep(20)
        px_30s = float(self.latest_prices.get(sym, px_10s))
        diff_pct_30s = ((px_30s - start_px) / start_px) * 100.0 if start_px > 0 else 0.0
        hit_30s = (diff_pct_30s < -0.04) if side == "Sell" else (diff_pct_30s > 0.04)

        rec["post_eval"] = {
            "px_10s": px_10s,
            "diff_pct_10s": round(diff_pct_10s, 2),
            "hit_10s": hit_10s,
            "px_30s": px_30s,
            "diff_pct_30s": round(diff_pct_30s, 2),
            "hit_30s": hit_30s,
        }

        # 프론트엔드에 실시간 평가 결과 갱신 브로드캐스트
        await self.broadcast({
            "type": "TRIGGER_EVAL_UPDATE",
            "trigger": rec
        })

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

    async def run_liq_snapshot_sync_loop(self):
        """30초마다 백그라운드에서 DuckDB 디스크 스냅샷을 RAM 인메모리 엔진으로 비동기 동기화"""
        while self.is_running:
            try:
                await asyncio.sleep(30.0)
                await asyncio.to_thread(self.liq_manager.sync_from_disk_snapshot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"청산 스냅샷 동기화 루프 에러: {e}")

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
            self.run_position_guard_loop(),
            self.run_liq_snapshot_sync_loop()
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
