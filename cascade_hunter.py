#!/usr/bin/env python3
"""
========================================================================================
🚀 [CASCADE HUNTER v5.0 - DUAL-EXCHANGE TWO-STAGE HUNTER] 
바이낸스 도화선 장전 ➔ 바이비트 실시간 확증 2단계 복합 숏 스캘핑 엔진
========================================================================================
- [듀얼 거래소 2단계 메커니즘]
  1. Binance Public WS: 롱 청산 >= bin_arm_usd 감지 시 ➔ 해당 심볼 SHORT_ARMED 장전 (유효시간 arm_sec)
  2. Bybit Public WS: 장전 상태에서 Bybit 소액 청산(bybit_confirm_usd) 또는 호가 붕괴(bybit_confirm_drop) 감지 시 ➔ 0ms 숏 격발!
  3. 지능형 트레일링 스탑: 심볼별 최적 trailing_bounce 비율로 최고점 시장가 익절
  4. 1분 자율 튜너의 5대 세부 설정값 3초 무중단 핫 리로드
  5. 2연속 손절 1시간 블랙리스트 + 당일 누적 손실(-0.6U) 서킷브레이커
========================================================================================
"""

import asyncio
import json
import orjson
import logging
import math
import os
import signal
import sys
import time
import urllib.request
import urllib.parse
import hmac
import hashlib
from collections import deque
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Dict, Any, Optional

import websockets
from dotenv import load_dotenv
from crypto_liquidation import LiquidationStream, LiquidationEvent, OrderSide, PositionSide

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("CascadeHunter")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CRED_PATH = "/home/jph/.bybit/oauth_token.json"
ACTIVE_SYMBOLS_PATH = "/home/jph/bybit_trade_collector/active_symbols.json"

LEVERAGE = 15
MARGIN_PER_TRADE_USDT = 1.2
NOTIONAL_PER_TRADE = MARGIN_PER_TRADE_USDT * LEVERAGE  # 18 USDT
MAX_DAILY_LOSS_USDT = 0.60  # 당일 최대 손실 서킷브레이커

BYBIT_REST_URL = "https://api.bybit.com"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_embed(self, title: str, description: str, color: int = 3447003):
        if not self.webhook_url:
            return
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": "🌊 Bybit Dual-Exchange Hunter v5.0"}
            }]
        }
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                pass
        except Exception as e:
            logger.error(f"디스코드 알림 실패: {e}")

    async def async_send_embed(self, title: str, description: str, color: int = 3447003):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.send_embed, title, description, color)


class BybitV5Client:
    def __init__(self, cred_file: str):
        with open(cred_file, "r") as f:
            cred = json.load(f)
        ai_cred = cred.get("ai-account", {})
        self.api_key = ai_cred.get("api_key")
        self.api_secret = ai_cred.get("api_secret")
        self.sub_member_id = ai_cred.get("sub_member_id")

    def _sign_request(self, method: str, endpoint: str, body: dict = None, params: dict = None) -> dict:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        body_str = json.dumps(body) if body else ""
        query_str = urllib.parse.urlencode(params) if params else ""
        raw_sign = ts + self.api_key + recv_window + (query_str if method == "GET" else body_str)
        signature = hmac.new(self.api_secret.encode('utf-8'), raw_sign.encode('utf-8'), hashlib.sha256).hexdigest()
        
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }
        url = f"{BYBIT_REST_URL}{endpoint}" + (f"?{query_str}" if query_str else "")
        req_data = body_str.encode('utf-8') if method == "POST" else None
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}

    def get_wallet_balance(self):
        return self._sign_request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})

    def set_leverage(self, symbol: str, leverage: int):
        body = {"category": "linear", "symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)}
        return self._sign_request("POST", "/v5/position/set-leverage", body=body)

    def get_positions(self, symbol: str = None):
        params = {"category": "linear"}
        if symbol: params["symbol"] = symbol
        else: params["settleCoin"] = "USDT"
        return self._sign_request("GET", "/v5/position/list", params=params)

    def get_instruments_info(self):
        url = f"{BYBIT_REST_URL}/v5/market/instruments-info?category=linear"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def place_market_order_with_tpsl(self, symbol: str, side: str, qty_str: str, tp_str: str, sl_str: str, link_id: str):
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty_str,
            "orderLinkId": link_id,
            "takeProfit": tp_str,
            "stopLoss": sl_str,
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market"
        }
        return self._sign_request("POST", "/v5/order/create", body=body)

    def place_market_short_with_tpsl(self, symbol: str, qty_str: str, tp_str: str, sl_str: str, link_id: str):
        return self.place_market_order_with_tpsl(symbol, "Sell", qty_str, tp_str, sl_str, link_id)

    def close_position_market(self, symbol: str):
        pos_res = self.get_positions(symbol)
        if pos_res.get("retCode") == 0:
            for p in pos_res.get("result", {}).get("list", []):
                size = float(p.get("size", 0.0))
                side = p.get("side", "")
                if size > 0:
                    close_side = "Buy" if side == "Sell" else "Sell"
                    body = {"category": "linear", "symbol": symbol, "side": close_side, "orderType": "Market", "qty": str(p.get("size")), "reduceOnly": True}
                    return self._sign_request("POST", "/v5/order/create", body=body)
        return {"retCode": -1, "retMsg": "No open position"}

    def cancel_all_orders(self, symbol: str):
        return self._sign_request("POST", "/v5/order/cancel-all", body={"category": "linear", "symbol": symbol})

    def get_closed_pnl(self, symbol: str, limit: int = 1):
        return self._sign_request("GET", "/v5/position/closed-pnl", params={"category": "linear", "symbol": symbol, "limit": str(limit)})


class DualExchangeCascadeHunter:
    def __init__(self, client: BybitV5Client, notifier: DiscordNotifier):
        self.client = client
        self.notifier = notifier
        self.is_running = True

        self.symbol_configs: Dict[str, Any] = {}
        self.monitored_symbols: list = []
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self.last_config_mtime: float = 0.0

        # 상태 관리
        self.in_position = False
        self.active_symbol: Optional[str] = None
        self.active_side: Optional[str] = None
        self.active_entry_price: float = 0.0
        self.active_entry_time: float = 0.0
        self.active_timeout_sec: float = 45.0
        self.cooldown_until: float = 0.0

        # 🎯 [2단계 도화선 장전 상태 관리]: {symbol: expires_at_timestamp}
        self.binance_armed: Dict[str, float] = {}

        # 동적 트레일링 & 소진 탈출
        self.lowest_price_seen: float = 999999.0
        self.last_liq_event_time: float = time.time()

        # 🛡️ 2연속 손절 심볼 블랙리스트
        self.symbol_loss_count: Dict[str, int] = {}
        self.symbol_blacklist: Dict[str, float] = {}

        # 일일 손실 서킷브레이커
        self.daily_pnl_usdt: float = 0.0
        self.is_circuit_breaker_triggered = False
        self._last_reset_date: str = time.strftime("%Y-%m-%d")

        # 버퍼
        self.bybit_liq_buffers: Dict[str, deque] = {}
        self.bybit_price_buffers: Dict[str, deque] = {}
        self.latest_prices: Dict[str, float] = {}

        self.bybit_ws_conn = None
        self.bybit_subscribed_topics = set()
        self.ws_send_lock = asyncio.Lock()
        self.last_known_pnl_id = ""

    def load_instruments_meta(self):
        try:
            res = self.client.get_instruments_info()
            for item in res.get("result", {}).get("list", []):
                sym = item.get("symbol")
                tick = item.get("priceFilter", {}).get("tickSize", "0.0001")
                qty_step = item.get("lotSizeFilter", {}).get("qtyStep", "1")
                min_qty = item.get("lotSizeFilter", {}).get("minOrderQty", "1")
                self.instrument_meta[sym] = {"tickSize": tick, "qtyStep": qty_step, "minQty": min_qty}
            logger.info(f"📐 [메타데이터 로드] 총 {len(self.instrument_meta)}개 심볼 규격 동기화")
        except Exception as e:
            logger.error(f"메타데이터 로드 실패: {e}")

    def format_price(self, symbol: str, price: float) -> str:
        meta = self.instrument_meta.get(symbol, {})
        tick = meta.get("tickSize", "0.0001")
        d_tick = Decimal(str(tick))
        d_px = Decimal(str(price))
        rounded = (d_px / d_tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * d_tick
        return format(rounded.normalize(), 'f') if '.' in str(tick) else str(int(rounded))

    def format_qty(self, symbol: str, notional: float, price: float) -> str:
        meta = self.instrument_meta.get(symbol, {})
        step = meta.get("qtyStep", "1")
        min_q = Decimal(str(meta.get("minQty", "1")))
        d_step = Decimal(str(step))
        raw_qty = Decimal(str(notional)) / Decimal(str(price))
        rounded = (raw_qty / d_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_step
        if rounded < min_q:
            # minQty로 올릴 경우 실제 노셔널이 설정의 3배 초과 시 진입 거부
            actual_notional = float(min_q) * price
            if actual_notional > notional * 3.0:
                logger.warning(f"⚠️ [{symbol}] minQty 강제 올림 시 노셔널 ${actual_notional:.1f} > 한도 ${notional * 3.0:.1f} ➔ 진입 거부")
                return "0"
            rounded = min_q
        return format(rounded.normalize(), 'f') if '.' in str(step) else str(int(rounded))

    def load_active_symbols_from_disk(self) -> bool:
        if not os.path.exists(ACTIVE_SYMBOLS_PATH):
            return False
        mtime = os.path.getmtime(ACTIVE_SYMBOLS_PATH)
        if mtime == self.last_config_mtime:
            return False

        try:
            with open(ACTIVE_SYMBOLS_PATH, "r") as f:
                data = json.load(f)
            new_configs = data.get("symbols", {})
            if not new_configs:
                return False

            self.symbol_configs = new_configs
            self.monitored_symbols = list(new_configs.keys())
            self.last_config_mtime = mtime

            for s in self.monitored_symbols:
                if s not in self.bybit_liq_buffers: self.bybit_liq_buffers[s] = deque(maxlen=500)
                if s not in self.bybit_price_buffers: self.bybit_price_buffers[s] = deque(maxlen=500)
                if s not in self.latest_prices: self.latest_prices[s] = 0.0
                self.client.set_leverage(s, LEVERAGE)

            logger.info(f"🔄 [2단계 핫 리로드] {len(self.monitored_symbols)}개 엘리트 심볼 로드: {', '.join(self.monitored_symbols)}")
            return True
        except Exception as e:
            logger.error(f"설정 로드 실패: {e}")
            return False

    def cleanup_all(self):
        for sym in self.monitored_symbols:
            try: self.client.cancel_all_orders(sym)
            except Exception: pass

    async def initialize(self):
        logger.info(f"🔑 [MAINNET] Bybit AI 계정 연동 완료 (Sub Member ID: {self.client.sub_member_id})")
        self.load_instruments_meta()
        self.load_active_symbols_from_disk()

        bal_res = self.client.get_wallet_balance()
        if bal_res.get("retCode") == 0:
            coins = bal_res.get("result", {}).get("list", [{}])[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    logger.info(f"💰 [계좌 잔고] UNIFIED USDT 잔고: {c.get('walletBalance')} USDT")

        await self.sync_positions()
        sym_list = ", ".join(list(self.symbol_configs.keys()))
        await self.notifier.async_send_embed(
            title="🌊 [가동 시작] 2단계 전조 확정 듀얼 트레이더 v5.0",
            description=f"운용 심볼: `{sym_list}` | 레버리지: `{LEVERAGE}x`",
            color=3447003
        )

    async def sync_positions(self):
        pos_res = self.client.get_positions()
        if pos_res.get("retCode") == 0:
            positions = pos_res.get("result", {}).get("list", [])
            open_pos = [p for p in positions if float(p.get("size", 0.0)) > 0]
            if open_pos:
                p = open_pos[0]
                self.in_position = True
                self.active_symbol = p.get("symbol")
                self.active_side = p.get("side")
                self.active_entry_price = float(p.get("avgPrice", 0.0))
                self.lowest_price_seen = self.active_entry_price
                up_time = int(p.get("updatedTime", 0)) / 1000.0
                self.active_entry_time = up_time if up_time > 0 else time.time()
                cfg = self.symbol_configs.get(self.active_symbol, {})
                self.active_timeout_sec = cfg.get("timeout_sec", 45.0)
                logger.info(f"🔒 [기존 포지션 동기화] {self.active_symbol} {self.active_side} @ ${self.active_entry_price}")
            else:
                self.in_position = False
                self.active_symbol = None
                self.active_side = None

    async def unified_liquidation_listener(self):
        """사용자님의 crypto-liquidation-stream 라이브러리를 통한 Binance & Bybit 실시간 청산 통합 감시"""
        while self.is_running:
            try:
                async with LiquidationStream(exchanges=["binance", "bybit"], min_notional_usd=0.0) as stream:
                    logger.info("🟡 [crypto_liquidation] Binance & Bybit 듀얼 청산 감시망 연결 성공!")
                    async for event in stream:
                        if not self.is_running:
                            break
                        sym = event.symbol
                        if sym not in self.symbol_configs:
                            continue

                        cfg = self.symbol_configs.get(sym, {})
                        now = time.time()

                        # 1. Binance 청산 -> 1단계 장전(Armed)
                        if event.exchange == "binance":
                            arm_threshold = cfg.get("bin_arm_usd", 300.0)
                            arm_duration = cfg.get("arm_sec", 8.0)

                            if event.notional_usd >= arm_threshold:
                                if event.is_long_liquidation:
                                    self.binance_armed[sym] = {"target_side": "Sell", "expires": now + arm_duration}
                                    logger.info(f"🟡 [Binance 롱 청산 도화선!] {sym} 롱 청산 ${event.notional_usd:,.0f} USD ➔ Bybit 숏 장전 ({arm_duration}초 유효)")
                                elif event.is_short_liquidation:
                                    self.binance_armed[sym] = {"target_side": "Buy", "expires": now + arm_duration}
                                    logger.info(f"🟢 [Binance 숏 청산 도화선!] {sym} 숏 청산 ${event.notional_usd:,.0f} USD ➔ Bybit 롱 장전 ({arm_duration}초 유효)")

                        # 2. Bybit 청산 -> 2단계 확증 즉시 검사
                        elif event.exchange == "bybit":
                            liq_side = "Sell" if event.is_long_liquidation else "Buy"
                            if sym not in self.bybit_liq_buffers:
                                self.bybit_liq_buffers[sym] = deque(maxlen=300)
                            self.bybit_liq_buffers[sym].append((now, event.notional_usd, event.price, liq_side))
                            self.last_liq_event_time = now
                            await self.check_bybit_confirmation(sym, event.price, now, event_type="LIQ", liq_usd=event.notional_usd, liq_side=liq_side)

            except Exception as e:
                logger.error(f"[crypto_liquidation 에러] {e} ➔ 3초 후 재연결...")
                await asyncio.sleep(3)

    async def bybit_ticker_listener(self):
        """바이비트 실시간 호가/체결가 감시 ➔ 가격 변동률 확증 및 트레일링 스탑"""
        while self.is_running:
            try:
                # 재연결 시 과거 가격 버퍼를 클리어하여 단절 전후 갭으로 인한 오신호 방지
                self.bybit_price_buffers.clear()

                async with websockets.connect(BYBIT_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self.bybit_ws_conn = ws
                    self.bybit_subscribed_topics = set()
                    await self.sync_bybit_subscriptions()

                    # Bybit 애플리케이션 레벨 핑 루프 (20초 주기)
                    async def _bybit_ping_task():
                        while self.is_running:
                            await asyncio.sleep(20)
                            try:
                                async with self.ws_send_lock:
                                    if self.bybit_ws_conn:
                                        await self.bybit_ws_conn.send('{"op":"ping"}')
                            except Exception:
                                break

                    ping_task = asyncio.create_task(_bybit_ping_task())

                    try:
                        async for message in ws:
                            if not self.is_running: break
                            data = orjson.loads(message)
                            topic = data.get("topic", "")

                            if topic.startswith("tickers."):
                                sym = topic.split(".")[1]
                                t_data = data.get("data", {})
                                lp = t_data.get("lastPrice")
                                if lp:
                                    now = time.time()
                                    p_float = float(lp)
                                    self.latest_prices[sym] = p_float
                                    if sym not in self.bybit_price_buffers:
                                        self.bybit_price_buffers[sym] = deque(maxlen=300)
                                    self.bybit_price_buffers[sym].append((now, p_float))
                                    while self.bybit_price_buffers[sym] and (now - self.bybit_price_buffers[sym][0][0] > 5.0):
                                        self.bybit_price_buffers[sym].popleft()

                                    # 가격 변동 확증 검사
                                    await self.check_bybit_confirmation(sym, p_float, now, event_type="TICK")
                    finally:
                        ping_task.cancel()

            except Exception as e:
                logger.error(f"Bybit Ticker WS 에러: {e} ➔ 2초 후 재연결...")
                await asyncio.sleep(2)

    async def sync_bybit_subscriptions(self):
        if not self.bybit_ws_conn: return
        target = set([f"tickers.{s}" for s in self.monitored_symbols])
        to_sub = list(target - self.bybit_subscribed_topics)
        to_unsub = list(self.bybit_subscribed_topics - target)

        async with self.ws_send_lock:
            if to_sub:
                chunk_size = 10
                for i in range(0, len(to_sub), chunk_size):
                    chunk = to_sub[i:i + chunk_size]
                    await self.bybit_ws_conn.send(json.dumps({"op": "subscribe", "args": chunk}))
                    if i + chunk_size < len(to_sub):
                        await asyncio.sleep(0.05)
                self.bybit_subscribed_topics.update(to_sub)
                logger.info(f"📡 [Bybit Ticker 구독] {len(to_sub)}개 토픽 추가 ({len(self.monitored_symbols)}개 심볼)")

            if to_unsub:
                chunk_size = 10
                for i in range(0, len(to_unsub), chunk_size):
                    chunk = to_unsub[i:i + chunk_size]
                    await self.bybit_ws_conn.send(json.dumps({"op": "unsubscribe", "args": chunk}))
                    if i + chunk_size < len(to_unsub):
                        await asyncio.sleep(0.05)
                self.bybit_subscribed_topics.difference_update(to_unsub)

            if to_unsub:
                chunk_size = 10
                for i in range(0, len(to_unsub), chunk_size):
                    chunk = to_unsub[i:i + chunk_size]
                    await self.bybit_ws_conn.send(json.dumps({"op": "unsubscribe", "args": chunk}))
                    if i + chunk_size < len(to_unsub):
                        await asyncio.sleep(0.05)
                self.bybit_subscribed_topics.difference_update(to_unsub)

    async def hot_reload_loop(self):
        while self.is_running:
            await asyncio.sleep(3.0)
            try:
                # 일일 PnL 자정 리셋
                today = time.strftime("%Y-%m-%d")
                if today != self._last_reset_date:
                    logger.info(f"🔄 [일일 리셋] 날짜 변경 감지 ({self._last_reset_date} ➔ {today}) | 누적 손익 {self.daily_pnl_usdt:+.4f}U 리셋")
                    self.daily_pnl_usdt = 0.0
                    self.is_circuit_breaker_triggered = False
                    self._last_reset_date = today

                # binance_armed 만료 키 주기적 청소 (TTL Cleaner)
                now = time.time()
                expired_keys = [k for k, v in self.binance_armed.items() if now > (v.get("expires", 0) if isinstance(v, dict) else v)]
                for k in expired_keys:
                    del self.binance_armed[k]

                if self.load_active_symbols_from_disk():
                    await self.sync_bybit_subscriptions()
            except Exception as e:
                logger.error(f"[hot_reload_loop 에러] {e}")

    async def check_bybit_confirmation(self, symbol: str, current_price: float, now: float, event_type: str, liq_usd: float = 0.0, liq_side: str = ""):
        """2단계 확증 검사: 바이낸스 장전 중 + Bybit 확증 신호 ➔ 0ms 양방향(롱/숏) 격발!"""
        if self.in_position or self.is_circuit_breaker_triggered:
            return
        if now < self.cooldown_until:
            return

        if symbol in self.symbol_blacklist:
            if now < self.symbol_blacklist[symbol]: return
            else:
                del self.symbol_blacklist[symbol]
                self.symbol_loss_count[symbol] = 0

        cfg = self.symbol_configs.get(symbol, {})
        by_conf_usd = cfg.get("bybit_confirm_usd", 50.0)
        by_conf_drop = cfg.get("bybit_confirm_drop", 0.10)

        # 1. 바이낸스 장전 유효성 체크
        armed_info = self.binance_armed.get(symbol)
        is_armed = False
        target_side = "Sell"

        if armed_info:
            exp = armed_info.get("expires", 0.0) if isinstance(armed_info, dict) else armed_info
            if now <= exp:
                is_armed = True
                target_side = armed_info.get("target_side", "Sell") if isinstance(armed_info, dict) else "Sell"
            else:
                del self.binance_armed[symbol]

        # 2. 확증 조건 검사
        is_confirmed = False
        confirm_reason = ""

        if is_armed:
            if target_side == "Sell":
                # 롱 청산 폭포수 ➔ 숏 격발
                if event_type == "LIQ" and liq_side == "Sell" and liq_usd >= by_conf_usd:
                    is_confirmed = True
                    confirm_reason = f"🟡 Binance 롱청산 ➔ 🔴 Bybit 전이청산 ${liq_usd:,.0f}"
                elif event_type == "TICK":
                    p_buf = self.bybit_price_buffers.get(symbol, deque(maxlen=500))
                    if len(p_buf) >= 2:
                        p_old = p_buf[0][1]
                        drop_pct = (p_old - current_price) / p_old * 100.0
                        if drop_pct >= by_conf_drop:
                            is_confirmed = True
                            confirm_reason = f"🟡 Binance 롱청산 ➔ 🔴 Bybit 낙폭 -{drop_pct:.2f}%"
            elif target_side == "Buy":
                # 숏 청산 스퀴즈 ➔ 롱 격발
                if event_type == "LIQ" and liq_side == "Buy" and liq_usd >= by_conf_usd:
                    is_confirmed = True
                    confirm_reason = f"🟢 Binance 숏청산 ➔ 🚀 Bybit 스퀴즈청산 ${liq_usd:,.0f}"
                elif event_type == "TICK":
                    p_buf = self.bybit_price_buffers.get(symbol, deque(maxlen=500))
                    if len(p_buf) >= 2:
                        p_old = p_buf[0][1]
                        rise_pct = (current_price - p_old) / p_old * 100.0
                        if rise_pct >= by_conf_drop:
                            is_confirmed = True
                            confirm_reason = f"🟢 Binance 숏청산 ➔ 🚀 Bybit 급등 +{rise_pct:.2f}%"
        else:
            # 바이비트 자체 대형 청산($300+) 백업 트리거
            if event_type == "LIQ":
                buf = self.bybit_liq_buffers.get(symbol, deque(maxlen=500))
                while buf and (now - buf[0][0] > 5.0): buf.popleft()
                tot_liq_sell = sum([b[1] for b in buf if len(b) > 3 and b[3] == "Sell"])
                tot_liq_buy = sum([b[1] for b in buf if len(b) > 3 and b[3] == "Buy"])
                if tot_liq_sell >= 300.0:
                    is_confirmed = True
                    target_side = "Sell"
                    confirm_reason = f"🔴 Bybit 자체 롱대형청산 ${tot_liq_sell:,.0f} 폭발"
                elif tot_liq_buy >= 300.0:
                    is_confirmed = True
                    target_side = "Buy"
                    confirm_reason = f"🟢 Bybit 자체 숏대형청산 ${tot_liq_buy:,.0f} 폭발"

        if is_confirmed:
            side_kr = "숏" if target_side == "Sell" else "롱"
            icon = "🌊" if target_side == "Sell" else "🚀"
            logger.warning(f"🚀 [2단계 {side_kr} 격발!] {symbol} ({confirm_reason}) ➔ 0ms 시장가 {side_kr} 진입!")

            px = current_price if current_price > 0 else self.latest_prices.get(symbol, 0.0)
            if px <= 0: return

            tp_pct = cfg.get("tp_pct", 2.50)
            sl_pct = cfg.get("sl_pct", 0.60)
            self.active_timeout_sec = cfg.get("timeout_sec", 45.0)

            p_buf = self.bybit_price_buffers.get(symbol, deque())
            if target_side == "Sell":
                p_high = max([item[1] for item in p_buf]) if p_buf else px * (1.0 + sl_pct / 100.0)
                sl_raw = max(px * (1.0 + sl_pct / 100.0), p_high * 1.001)
                tp_raw = px * (1.0 - tp_pct / 100.0)
            else:
                p_low = min([item[1] for item in p_buf]) if p_buf else px * (1.0 - sl_pct / 100.0)
                sl_raw = min(px * (1.0 - sl_pct / 100.0), p_low * 0.999)
                tp_raw = px * (1.0 + tp_pct / 100.0)

            tp_str = self.format_price(symbol, tp_raw)
            sl_str = self.format_price(symbol, sl_raw)
            qty_str = self.format_qty(symbol, NOTIONAL_PER_TRADE, px)
            if qty_str == "0":
                logger.warning(f"⚠️ [{symbol}] 수량 산출 불가 (minQty 과다) ➔ 진입 스킵")
                return

            link_id = f"CASCADE_{target_side[0]}_{int(now*1000)}"
            res = self.client.place_market_order_with_tpsl(symbol, target_side, qty_str, tp_str, sl_str, link_id)

            if res.get("retCode") == 0:
                self.in_position = True
                self.active_symbol = symbol
                self.active_side = target_side
                self.active_entry_price = px
                self.active_entry_time = now
                self.lowest_price_seen = px
                self.last_liq_event_time = now
                if symbol in self.binance_armed: del self.binance_armed[symbol]

                logger.info(f"🚀 [{side_kr} 탑승 성공] {symbol} 수량: {qty_str} | 진입: ${px} | TP: ${tp_str} | SL: ${sl_str} | 근거: {confirm_reason}")
                await self.notifier.async_send_embed(
                    title=f"{icon} [{side_kr} 진입] {symbol}",
                    description=f"근거: `{confirm_reason}`\n진입: `${px}` | 수량: `{qty_str}` | TP: `${tp_str}` | SL: `${sl_str}`",
                    color=15158332 if target_side == "Sell" else 3066993
                )
            else:
                logger.error(f"⚠️ [주문 실패] {symbol} 거절: {res.get('retMsg')}")
                self.cooldown_until = now + 30.0

    async def position_guard_loop(self):
        while self.is_running:
            await asyncio.sleep(0.5)
            if not self.in_position or not self.active_symbol:
                continue

            pos_res = self.client.get_positions(self.active_symbol)
            if pos_res.get("retCode") == 0:
                positions = pos_res.get("result", {}).get("list", [])
                curr_pos = [p for p in positions if float(p.get("size", 0.0)) > 0]

                if not curr_pos:
                    logger.info(f"🎉 [{self.active_symbol} 포지션 종료] OCO 체결 완료 확인!")
                    await self.check_trade_result(self.active_symbol)
                    self.in_position = False
                    self.active_symbol = None
                    self.cooldown_until = time.time() + 30.0
                    continue

                now = time.time()
                cur_px = self.latest_prices.get(self.active_symbol, self.active_entry_price)
                if cur_px <= 0: continue

                # 양방향(롱/숏) 실시간 손익 및 극값(최고/최저) 갱신
                if self.active_side == "Sell":
                    if cur_px < self.lowest_price_seen:
                        self.lowest_price_seen = cur_px
                    pnl_pct = (self.active_entry_price - cur_px) / self.active_entry_price * 100.0
                    max_gain_pct = (self.active_entry_price - self.lowest_price_seen) / self.active_entry_price * 100.0
                    bounce_pct = (cur_px - self.lowest_price_seen) / self.lowest_price_seen * 100.0
                else: # "Buy" (Long)
                    if cur_px > self.lowest_price_seen:
                        self.lowest_price_seen = cur_px
                    pnl_pct = (cur_px - self.active_entry_price) / self.active_entry_price * 100.0
                    max_gain_pct = (self.lowest_price_seen - self.active_entry_price) / self.active_entry_price * 100.0
                    bounce_pct = (self.lowest_price_seen - cur_px) / self.lowest_price_seen * 100.0

                elapsed = now - self.active_entry_time

                cfg = self.symbol_configs.get(self.active_symbol, {})
                min_gain_req = cfg.get("min_gain_pct", 1.00)
                bounce_req = cfg.get("trailing_bounce", 0.20)

                # 1. 지능형 트레일링 스탑
                if max_gain_pct >= min_gain_req and bounce_pct >= bounce_req:
                    side_kr = "숏" if self.active_side == "Sell" else "롱"
                    logger.warning(f"🎯 [{self.active_symbol} {side_kr} 트레일링 익절] 최고 +{max_gain_pct:.2f}% ➔ 반등/눌림 {bounce_pct:.2f}% 시장가 익절!")
                    close_res = self.client.close_position_market(self.active_symbol)
                    if close_res.get("retCode") != 0:
                        logger.error(f"⚠️ [{self.active_symbol}] 청산 실패: {close_res.get('retMsg')} ➔ 포지션 유지")
                        continue
                    sym = self.active_symbol
                    self.in_position = False
                    self.active_symbol = None
                    self.cooldown_until = now + 45.0
                    await asyncio.sleep(0.5)
                    await self.check_trade_result(sym)
                    await self.notifier.async_send_embed(
                        title=f"🎯 [트레일링 익절] {sym} ({side_kr})",
                        description=f"최고 수익 `+{max_gain_pct:.2f}%` ➔ 되돌림 `{bounce_pct:.2f}%` 감지 시장가 익절",
                        color=3066993
                    )
                    continue

                # 2. 청산 소진 조기 탈출
                time_since_liq = now - self.last_liq_event_time
                if time_since_liq >= 5.0 and pnl_pct >= 0.35 and elapsed >= 8.0:
                    side_kr = "숏" if self.active_side == "Sell" else "롱"
                    logger.warning(f"⏱️ [{self.active_symbol} {side_kr} 청산 소진 익절] 5초간 청산 멈춤 & 수익 +{pnl_pct:.2f}% ➔ 시장가 조기 익절!")
                    close_res = self.client.close_position_market(self.active_symbol)
                    if close_res.get("retCode") != 0:
                        logger.error(f"⚠️ [{self.active_symbol}] 청산 실패: {close_res.get('retMsg')} ➔ 포지션 유지")
                        continue
                    sym = self.active_symbol
                    self.in_position = False
                    self.active_symbol = None
                    self.cooldown_until = now + 45.0
                    await asyncio.sleep(0.5)
                    await self.check_trade_result(sym)
                    await self.notifier.async_send_embed(
                        title=f"⏱️ [소진 탈출] {sym} ({side_kr})",
                        description=f"청산 멈춤 ➔ `+{pnl_pct:.2f}%` 조기 익절",
                        color=3066993
                    )
                    continue

                # 3. 절대 타임아웃
                if elapsed >= self.active_timeout_sec:
                    logger.warning(f"⏱️ [{self.active_symbol} 타임아웃] {elapsed:.1f}초 경과 ➔ 시장가 탈출!")
                    close_res = self.client.close_position_market(self.active_symbol)
                    if close_res.get("retCode") != 0:
                        logger.error(f"⚠️ [{self.active_symbol}] 청산 실패: {close_res.get('retMsg')} ➔ 포지션 유지")
                        continue
                    sym = self.active_symbol
                    self.in_position = False
                    self.active_symbol = None
                    self.cooldown_until = now + 45.0
                    await asyncio.sleep(0.5)
                    await self.check_trade_result(sym)
                    await self.notifier.async_send_embed(
                        title=f"⏱️ [타임아웃] {sym}",
                        description=f"제한시간 `{self.active_timeout_sec:.0f}s` 경과 시장가 종료",
                        color=15105570
                    )

    async def check_trade_result(self, symbol: str):
        pnl_res = self.client.get_closed_pnl(symbol, limit=1)
        if pnl_res.get("retCode") == 0:
            list_pnl = pnl_res.get("result", {}).get("list", [])
            if list_pnl:
                p = list_pnl[0]
                closed_pnl = float(p.get("closedPnl", 0.0))
                pnl_id = p.get("orderId", "")
                if pnl_id != self.last_known_pnl_id:
                    self.last_known_pnl_id = pnl_id
                    self.daily_pnl_usdt += closed_pnl

                    if closed_pnl > 0:
                        self.symbol_loss_count[symbol] = 0
                        logger.info(f"🎉 [폭포수 익절] {symbol} 손익: +{closed_pnl:.4f} USDT (누적: {self.daily_pnl_usdt:+.4f}U)")
                        await self.notifier.async_send_embed(
                            title=f"🎉 [익절 완료] {symbol}",
                            description=f"손익: `+{closed_pnl:.4f} USDT` | 당일 누적: `{self.daily_pnl_usdt:+.4f} USDT`",
                            color=3066993
                        )
                    else:
                        self.symbol_loss_count[symbol] = self.symbol_loss_count.get(symbol, 0) + 1
                        logger.info(f"🚨 [칼손절 마감] {symbol} 손익: {closed_pnl:.4f} USDT (연속 손절: {self.symbol_loss_count[symbol]}회)")
                        
                        if self.symbol_loss_count[symbol] >= 2:
                            self.symbol_blacklist[symbol] = time.time() + 3600.0
                            logger.error(f"🚫 [{symbol} 블랙리스트] 2연속 손절 ➔ 1시간 거래 자동 차단!")
                            await self.notifier.async_send_embed(
                                title=f"🚫 [1시간 차단] {symbol}",
                                description="2연속 손절 피격으로 1시간 거래 일시 차단",
                                color=15158332
                            )

                        await self.notifier.async_send_embed(
                            title=f"🚨 [손절 마감] {symbol}",
                            description=f"손익: `{closed_pnl:.4f} USDT` | 당일 누적: `{self.daily_pnl_usdt:+.4f} USDT`",
                            color=15158332
                        )

                    if self.daily_pnl_usdt <= -MAX_DAILY_LOSS_USDT:
                        self.is_circuit_breaker_triggered = True
                        logger.error(f"🛑 [서킷브레이커] 누적 손실 {self.daily_pnl_usdt:.4f} USDT 도달 ➔ 거래 중단!")
                        await self.notifier.async_send_embed(
                            title="🛑 [서킷브레이커] 일일 한도 도달",
                            description=f"당일 누적 손실 `{self.daily_pnl_usdt:.4f} USDT`로 금일 거래 종료",
                            color=10038562
                        )

    async def run(self):
        await self.initialize()
        await asyncio.gather(
            self.unified_liquidation_listener(),
            self.bybit_ticker_listener(),
            self.position_guard_loop(),
            self.hot_reload_loop()
        )


def main():
    client = BybitV5Client(CRED_PATH)
    notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
    trader = DualExchangeCascadeHunter(client, notifier)

    def handle_exit(signum, frame):
        trader.is_running = False
        trader.cleanup_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        handle_exit(2, None)


if __name__ == "__main__":
    main()
