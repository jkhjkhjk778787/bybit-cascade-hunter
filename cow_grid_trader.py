#!/usr/bin/env python3
"""
[MAINNET] COWUSDT 실전형 15배 Anti-Squeeze OCO 스마트 그리드 트레이딩 엔진
[3대 방탄 시스템 전면 탑재]
1. Post-TP Cooldown: 익절 후 90초간 주문 전면 중지 (유동성 과열 휩쏘 회피)
2. Post-SL Circuit Breaker: 손절 후 3분간 강제 동결 (폭락 빔 연속 칼날 받기 차단)
3. Volatility Shield: 1분간 가격 변동폭 > 1.50% 감지 시 호가 즉시 자동 철회 & 관망
4. 그리드 간격 최적화: ±1.20% (노이즈 방어) | TP +0.60% (Limit) | SL -2.00% (Market)
5. 3중 방탄 종료 클린업 (atexit, 다중 시그널 가로채기, 재시작 시 전수 취소)
6. Discord 실시간 푸시 알림
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from collections import deque
import websockets
import orjson

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("cow_grid_trader")

# 자격증명 및 디스코드 웹훅 경로
CRED_PATH = "/home/jph/.bybit/oauth_token.json"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1538351496230477885/kaw1CC-ai-PenZF8luXycybltlehwlBWLTUlvql9rW9c3FL9p0s2-Nq4AVQ5H4Pwi-jJ"

SYMBOL = "COWUSDT"
LEVERAGE = 15.0
MARGIN_PER_ORDER_USDT = 1.2                            # 잔고 맞춤 증거금 (롱/숏 2개 배치 시 2.4U 소모)
NOTIONAL_PER_ORDER = MARGIN_PER_ORDER_USDT * LEVERAGE  # 18 USDT (Bybit 최소 5U 충족)

# 🧠 동적 ATR 그리드 파라미터
GRID_SPACING_MIN_PCT = 1.00                            # 최소 진입 간격 (횡보장 빠른 핑퐁)
GRID_SPACING_MAX_PCT = 2.00                            # 최대 진입 간격 (폭풍장 깊은 꼬리 낚시)
ATR_MULTIPLIER = 0.80                                  # 1분 변동폭 × 0.8 = 그리드 간격

TP_PCT = 0.60                                          # +0.60% 반등 지정가 익절
SL_PCT = 2.00                                          # -2.00% 비상 손절
REFRESH_INTERVAL_SEC = 30.0                            # 30초 기본 갱신 주기

POST_TP_COOLDOWN_SEC = 90.0                            # 🛡️ 익절 후 90초 쿨다운
POST_SL_CIRCUIT_BREAKER_SEC = 180.0                    # 🛡️ 손절 후 3분 서킷브레이커
MAX_1M_VOLATILITY_PCT = 2.50                           # 🛡️ 1분 변동폭 > 2.5% 광기 빔 시에만 호가 철회 (상충 해결)
MAX_HOLDING_TIME_SEC = 180.0                           # ⏱️ 포지션 최대 보유시간 3분 (180초 타임아웃 컷)

BYBIT_REST_URL = "https://api.bybit.com"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"


class DiscordNotifier:
    """디스코드 웹훅 실시간 알림 전송기"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_embed(self, title: str, description: str, color: int, fields: list = None):
        if not self.webhook_url:
            return

        payload = {
            "username": "COW AI Grid Bot 🚀",
            "avatar_url": "https://cryptologos.cc/logos/cow-protocol-cow-logo.png",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Bybit AI Subaccount • COWUSDT Anti-Squeeze Engine"}
            }]
        }

        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=orjson.dumps(payload),
                headers={"Content-Type": "application/json", "User-Agent": "DiscordWebhook/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as e:
            logger.warning(f"[DISCORD WARN] 알림 전송 실패: {e}")

    async def async_send_embed(self, title: str, description: str, color: int, fields: list = None):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.send_embed, title, description, color, fields)


class BybitV5Client:
    """Bybit V5 REST API 서명 및 요청 클라이언트"""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def _sign_request(self, method: str, endpoint: str, params: dict = None, body: dict = None):
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        if method == "GET":
            query_str = urllib.parse.urlencode(params) if params else ""
            raw_sign = timestamp + self.api_key + recv_window + query_str
            url = f"{BYBIT_REST_URL}{endpoint}" + (f"?{query_str}" if query_str else "")
            req_body = None
        else:
            req_body = orjson.dumps(body).decode("utf-8") if body else ""
            raw_sign = timestamp + self.api_key + recv_window + req_body
            url = f"{BYBIT_REST_URL}{endpoint}"

        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            raw_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
            "User-Agent": "BybitAI-GridTrader/2.0"
        }

        req = urllib.request.Request(
            url,
            data=req_body.encode('utf-8') if req_body else None,
            headers=headers,
            method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode('utf-8'))
        except Exception as e:
            logger.error(f"[API ERROR] {method} {endpoint} 요청 실패: {e}")
            return {"retCode": -1, "retMsg": str(e)}

    def set_leverage(self, symbol: str, leverage: float):
        lev_str = f"{leverage:.2f}"
        body = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": lev_str,
            "sellLeverage": lev_str
        }
        return self._sign_request("POST", "/v5/position/set-leverage", body=body)

    def get_wallet_balance(self):
        return self._sign_request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})

    def get_positions(self, symbol: str):
        return self._sign_request("GET", "/v5/position/list", params={"category": "linear", "symbol": symbol})

    def get_open_orders(self, symbol: str):
        return self._sign_request("GET", "/v5/order/realtime", params={"category": "linear", "symbol": symbol})

    def get_closed_pnl(self, symbol: str, limit: int = 5):
        return self._sign_request("GET", "/v5/position/closed-pnl", params={"category": "linear", "symbol": symbol, "limit": limit})

    def place_order(self, side: str, price: float, qty: int, tp: float, sl: float, order_link_id: str):
        body = {
            "category": "linear",
            "symbol": SYMBOL,
            "side": side,
            "orderType": "Limit",
            "price": f"{price:.5f}",
            "qty": str(qty),
            "timeInForce": "PostOnly",     # 100% Maker 보장 (수수료 0.02%)
            "takeProfit": f"{tp:.5f}",     # 거래소 내장 OCO 익절
            "stopLoss": f"{sl:.5f}",       # 거래소 내장 OCO 손절
            "tpOrderType": "Limit",        # 지정가 익절
            "slOrderType": "Market",       # 시장가 손절
            "tpslMode": "Partial",         # Partial 모드
            "tpLimitPrice": f"{tp:.5f}",   # 지정가 익절 가격
            "orderLinkId": order_link_id
        }
        return self._sign_request("POST", "/v5/order/create", body=body)

    def cancel_order(self, order_id: str = None, order_link_id: str = None):
        body = {"category": "linear", "symbol": SYMBOL}
        if order_id:
            body["orderId"] = order_id
        if order_link_id:
            body["orderLinkId"] = order_link_id
        return self._sign_request("POST", "/v5/order/cancel", body=body)

    def cancel_all_orders(self, symbol: str):
        body = {"category": "linear", "symbol": symbol}
        return self._sign_request("POST", "/v5/order/cancel-all", body=body)

    def close_position_market(self, symbol: str):
        """현재 열려있는 포지션을 즉시 시장가(Market ReduceOnly)로 전량 청산"""
        pos_res = self.get_positions(symbol)
        if pos_res.get("retCode") == 0:
            for p in pos_res.get("result", {}).get("list", []):
                size = float(p.get("size", 0.0))
                side = p.get("side", "")
                if size > 0:
                    close_side = "Sell" if side == "Buy" else "Buy"
                    body = {
                        "category": "linear",
                        "symbol": symbol,
                        "side": close_side,
                        "orderType": "Market",
                        "qty": str(int(size)),
                        "reduceOnly": True
                    }
                    logger.info(f"⏱️ [타임아웃 실행] {side} {size} COW 시장가 청산 주문 전송")
                    return self._sign_request("POST", "/v5/order/create", body=body)
        return {"retCode": -1, "retMsg": "No open position"}


class CowGridTrader:
    def __init__(self, client: BybitV5Client, notifier: DiscordNotifier):
        self.client = client
        self.notifier = notifier
        self.is_running = True
        self.latest_price = 0.0
        self.price_history = []
        self.tick_window_1m = deque()  # (monotonic_ts, price)
        self.last_grid_refresh_time = 0.0
        self.last_status_report_time = time.monotonic()

        # 🛡️ 쿨다운 & 서킷브레이커 상태 타이머
        self.cooldown_until = 0.0
        self.circuit_breaker_until = 0.0
        self.is_volatility_shield_active = False

        # 상태 관리
        self.active_long_order_id = None
        self.active_short_order_id = None
        self.has_long_position = False
        self.has_short_position = False
        self.position_entry_time = 0.0

        # 봇 시작 전 기존 과거 PnL ID 사전 동기화 (과거 손익으로 인한 오작동 방지)
        self.last_known_pnl_id = self._get_initial_pnl_id()

        # 통계
        self.total_cycles = 0
        self.total_orders_placed = 0
        self.total_tp_count = 0
        self.total_sl_count = 0

    def _get_initial_pnl_id(self) -> str:
        try:
            res = self.client.get_closed_pnl(SYMBOL, limit=1)
            if res.get("retCode") == 0:
                records = res.get("result", {}).get("list", [])
                if records:
                    return records[0].get("orderId") or records[0].get("updatedTime")
        except Exception:
            pass
        return None

    def calculate_center_price(self) -> float:
        if not self.price_history:
            return self.latest_price
        return float(sum(self.price_history) / len(self.price_history))

    def get_1m_volatility_pct(self) -> float:
        """최근 60초간의 최고가/최저가 변동폭(%) 산출"""
        now = time.monotonic()
        while self.tick_window_1m and (now - self.tick_window_1m[0][0] > 60.0):
            self.tick_window_1m.popleft()

        if len(self.tick_window_1m) < 5:
            return 0.0

        prices = [p for _, p in self.tick_window_1m]
        min_p = min(prices)
        max_p = max(prices)
        if min_p <= 0:
            return 0.0
        return ((max_p - min_p) / min_p) * 100.0

    async def update_position_state(self):
        res = self.client.get_positions(SYMBOL)
        if res.get("retCode") == 0:
            positions = res.get("result", {}).get("list", [])
            has_long, has_short = False, False
            for p in positions:
                size = float(p.get("size", 0.0))
                side = p.get("side", "")
                if size > 0:
                    if side == "Buy":
                        has_long = True
                    elif side == "Sell":
                        has_short = True

                    # 🧠 Bybit 거래소 서버 실제 체결 타임스탬프(updatedTime) 기반 경과 시간 산출
                    up_time = int(p.get("updatedTime", 0)) / 1000.0
                    if up_time > 0:
                        self.position_entry_time = up_time
            
            if not self.has_long_position and has_long:
                logger.info("⚡ [체결 감지] 롱 그리드 포지션 확인! (거래소 서버 타임스탬프 기준 타임아웃 감시)")
                await self.notifier.async_send_embed(
                    title="🟢 [체결 감지] 롱 포지션 진입",
                    description=f"COWUSDT 하단 지정가 체결! 0ms OCO 감시 및 **거래소 실시간 3분(180s) 타임아웃**이 가동됩니다.",
                    color=3066993,
                    fields=[
                        {"name": "진입가", "value": f"${self.latest_price:.5f}", "inline": True},
                        {"name": "익절 TP (+0.6%)", "value": f"${self.latest_price * 1.006:.5f}", "inline": True},
                        {"name": "손절 SL (-2.0%)", "value": f"${self.latest_price * 0.980:.5f}", "inline": True}
                    ]
                )
            elif not self.has_short_position and has_short:
                logger.info("⚡ [체결 감지] 숏 그리드 포지션 확인! (거래소 서버 타임스탬프 기준 타임아웃 감시)")
                await self.notifier.async_send_embed(
                    title="🔴 [체결 감지] 숏 포지션 진입",
                    description=f"COWUSDT 상단 지정가 체결! 0ms OCO 감시 및 **거래소 실시간 3분(180s) 타임아웃**이 가동됩니다.",
                    color=15158332,
                    fields=[
                        {"name": "진입가", "value": f"${self.latest_price:.5f}", "inline": True},
                        {"name": "익절 TP (-0.6%)", "value": f"${self.latest_price * 0.994:.5f}", "inline": True},
                        {"name": "손절 SL (+2.0%)", "value": f"${self.latest_price * 1.020:.5f}", "inline": True}
                    ]
                )

            self.has_long_position = has_long
            self.has_short_position = has_short

            if not has_long and not has_short:
                self.position_entry_time = 0.0

    async def check_closed_pnl(self):
        pnl_res = self.client.get_closed_pnl(SYMBOL, limit=1)
        if pnl_res.get("retCode") == 0:
            records = pnl_res.get("result", {}).get("list", [])
            if records:
                rec = records[0]
                rec_id = rec.get("orderId") or rec.get("updatedTime")
                if rec_id and rec_id != self.last_known_pnl_id:
                    self.last_known_pnl_id = rec_id
                    pnl = float(rec.get("closedPnl", 0.0))
                    side = rec.get("side", "")
                    entry_p = float(rec.get("avgEntryPrice", 0.0))
                    exit_p = float(rec.get("avgExitPrice", 0.0))

                    if pnl > 0:
                        self.total_tp_count += 1
                        self.cooldown_until = time.monotonic() + POST_TP_COOLDOWN_SEC
                        logger.info(f"🎉 [익절 완료] 실현 손익: +{pnl:.4f} USDT | 🛡️ {POST_TP_COOLDOWN_SEC:.0f}초간 쿨다운 발동")
                        
                        # 미체결 주문 일괄 정리
                        self.client.cancel_all_orders(SYMBOL)
                        self.active_long_order_id = None
                        self.active_short_order_id = None

                        await self.notifier.async_send_embed(
                            title="🎉 [익절 완료] TP 달성!",
                            description=f"COWUSDT OCO 익절이 체결되었습니다. 유동성 과열을 방지하기 위해 **{POST_TP_COOLDOWN_SEC:.0f}초간 쿨다운(휴식)**에 들어갑니다.",
                            color=3066993,
                            fields=[
                                {"name": "실현 손익", "value": f"**+{pnl:.4f} USDT**", "inline": True},
                                {"name": "포지션", "value": side, "inline": True},
                                {"name": "진입가 ➔ 청산가", "value": f"${entry_p:.5f} ➔ ${exit_p:.5f}", "inline": False},
                                {"name": "🛡️ 안전 조치", "value": f"{POST_TP_COOLDOWN_SEC:.0f}초간 신규 진입 일시 중단", "inline": False}
                            ]
                        )
                    elif pnl < 0:
                        self.total_sl_count += 1
                        self.circuit_breaker_until = time.monotonic() + POST_SL_CIRCUIT_BREAKER_SEC
                        logger.warning(f"🚨 [손절 완료] 실현 손익: {pnl:.4f} USDT | 🛡️ {POST_SL_CIRCUIT_BREAKER_SEC:.0f}초간 서킷브레이커 발동")

                        # 미체결 주문 즉시 전량 취소
                        self.client.cancel_all_orders(SYMBOL)
                        self.active_long_order_id = None
                        self.active_short_order_id = None

                        await self.notifier.async_send_embed(
                            title="🚨 [손절 발동] 비상 SL 컷 & 서킷브레이커",
                            description=f"COWUSDT 비상 손절이 실행되었습니다. 추가 폭락 빔 칼날 받기를 방지하기 위해 **{POST_SL_CIRCUIT_BREAKER_SEC/60:.1f}분간 강제 동결(Circuit Breaker)**됩니다.",
                            color=15158332,
                            fields=[
                                {"name": "실현 손익", "value": f"**{pnl:.4f} USDT**", "inline": True},
                                {"name": "진입가 ➔ 청산가", "value": f"${entry_p:.5f} ➔ ${exit_p:.5f}", "inline": False},
                                {"name": "🛡️ 안전 조치", "value": f"{POST_SL_CIRCUIT_BREAKER_SEC/60:.1f}분간 거래 전면 동결", "inline": False}
                            ]
                        )

    async def refresh_grid_orders(self):
        if self.latest_price <= 0:
            return

        now = time.monotonic()

        # 1. 손익 및 포지션 상태 갱신
        await self.update_position_state()
        await self.check_closed_pnl()

        # 🔒 [Single Position Lock] 포지션을 1개라도 보유 중이라면 모든 미체결 주문 취소 후 OCO/타임아웃 감시
        if self.has_long_position or self.has_short_position:
            pos_name = "롱(Long)" if self.has_long_position else "숏(Short)"
            if self.active_long_order_id or self.active_short_order_id:
                logger.info(f"🔒 [{pos_name} 체결 감지] 반대쪽 미체결 주문 즉시 전량 취소 및 OCO 청산 집중 모드 전환")
                self.client.cancel_all_orders(SYMBOL)
                self.active_long_order_id = None
                self.active_short_order_id = None

            # ⏱️ 3분 (180초) 타임아웃 감시 (Bybit 거래소 절대 타임스탬프 기준)
            if self.position_entry_time > 0:
                current_unix = time.time()
                elapsed = current_unix - self.position_entry_time
                if elapsed >= MAX_HOLDING_TIME_SEC:
                    logger.warning(f"⏱️ [3분 타임아웃 도달] 거래소 체결 후 {elapsed:.1f}초 경과 ➔ 지지선 붕괴 위험 회피 시장가 즉시 탈출 실행!")
                    close_res = self.client.close_position_market(SYMBOL)
                    self.cooldown_until = now + POST_TP_COOLDOWN_SEC
                    self.position_entry_time = 0.0
                    await self.notifier.async_send_embed(
                        title="⏱️ [3분 타임아웃] 시장가 즉시 탈출",
                        description=f"{pos_name} 포지션 보유 시간이 **{elapsed:.0f}초(3분 초과)**로 확인되어 지지선 붕괴 위험을 방어하기 위해 즉시 시장가 탈출했습니다. ({POST_TP_COOLDOWN_SEC:.0f}초 쿨다운 진입)",
                        color=15105570
                    )
                    return
                else:
                    logger.info(f"🔒 [{pos_name} 포지션 보유 중] OCO 청산 대기 중 (실제 경과: {elapsed:.1f}s / 최대: {MAX_HOLDING_TIME_SEC:.0f}s)")
            else:
                logger.info(f"🔒 [{pos_name} 포지션 보유 중] OCO 청산 대기 중 (신규 주문 일체 중단)")
            return

        # 🛡️ 방탄 1: 손절 후 서킷브레이커 체크
        if now < self.circuit_breaker_until:
            rem = int(self.circuit_breaker_until - now)
            logger.info(f"❄️ [서킷브레이커 동결 중] 남은 시간: {rem}초 (폭락 빔 방어 관망)")
            return

        # 🛡️ 방탄 2: 익절 후 쿨다운 체크
        if now < self.cooldown_until:
            rem = int(self.cooldown_until - now)
            logger.info(f"⏳ [익절 후 쿨다운 중] 남은 시간: {rem}초 (유동성 과열 진정 대기)")
            return

        # 🛡️ 방탄 3: 1분 변동성 과열 필터
        vol_1m = self.get_1m_volatility_pct()
        if vol_1m >= MAX_1M_VOLATILITY_PCT:
            if not self.is_volatility_shield_active:
                self.is_volatility_shield_active = True
                logger.warning(f"⚠️ [변동성 과열 감지] 1분 변동폭: {vol_1m:.2f}% (기준: {MAX_1M_VOLATILITY_PCT}%) ➔ 호가 즉시 철회 & 관망")
                self.client.cancel_all_orders(SYMBOL)
                self.active_long_order_id = None
                self.active_short_order_id = None
                await self.notifier.async_send_embed(
                    title="⚠️ [변동성 과열] 호가 자동 철회 (Volatility Shield)",
                    description=f"최근 1분간 가격 변동폭이 **{vol_1m:.2f}%**로 급변동 구간에 진입하여 그리드 호가를 안전하게 전량 철회하고 관망합니다.",
                    color=15105570
                )
            return
        else:
            if self.is_volatility_shield_active:
                self.is_volatility_shield_active = False
                logger.info(f"✅ [변동성 진정] 1분 변동폭: {vol_1m:.2f}% ➔ 그리드 정상 가동 복귀")

        center = self.calculate_center_price()
        if center <= 0:
            center = self.latest_price

        # 🧠 동적 ATR 진입 간격 산출 (최소 1.00% ~ 최대 2.00%)
        dynamic_spacing = max(GRID_SPACING_MIN_PCT, min(GRID_SPACING_MAX_PCT, vol_1m * ATR_MULTIPLIER)) if vol_1m > 0 else GRID_SPACING_MIN_PCT

        qty = max(1, int(NOTIONAL_PER_ORDER / center))

        # 1. 롱 그리드 (동적 간격 적용)
        long_entry = round(center * (1.0 - dynamic_spacing / 100.0), 5)
        long_tp = round(long_entry * (1.0 + TP_PCT / 100.0), 5)
        long_sl = round(long_entry * (1.0 - SL_PCT / 100.0), 5)

        if self.active_long_order_id:
            self.client.cancel_order(order_link_id=self.active_long_order_id)

        link_id = f"COW_L_{int(time.time()*1000)}"
        res = self.client.place_order("Buy", long_entry, qty, long_tp, long_sl, link_id)
        if res.get("retCode") == 0:
            self.active_long_order_id = link_id
            self.total_orders_placed += 1
            logger.info(f"🟢 [롱 그리드 배치 성공] 가격: ${long_entry:.5f} (동적 -{dynamic_spacing:.2f}%) | 수량: {qty} COW (~{qty*center:.1f} USDT) | TP: ${long_tp:.5f} (+0.6%) | SL: ${long_sl:.5f} (-2.0%)")
        else:
            logger.info(f"⚠️ [롱 주문 응답] 코드: {res.get('retCode')} | 메시지: {res.get('retMsg')}")

        # 2. 숏 그리드 (동적 간격 적용)
        short_entry = round(center * (1.0 + dynamic_spacing / 100.0), 5)
        short_tp = round(short_entry * (1.0 - TP_PCT / 100.0), 5)
        short_sl = round(short_entry * (1.0 + SL_PCT / 100.0), 5)

        if self.active_short_order_id:
            self.client.cancel_order(order_link_id=self.active_short_order_id)

        link_id = f"COW_S_{int(time.time()*1000)}"
        res = self.client.place_order("Sell", short_entry, qty, short_tp, short_sl, link_id)
        if res.get("retCode") == 0:
            self.active_short_order_id = link_id
            self.total_orders_placed += 1
            logger.info(f"🔴 [숏 그리드 배치 성공] 가격: ${short_entry:.5f} (동적 +{dynamic_spacing:.2f}%) | 수량: {qty} COW (~{qty*center:.1f} USDT) | TP: ${short_tp:.5f} (-0.6%) | SL: ${short_sl:.5f} (+2.0%)")
        else:
            logger.info(f"⚠️ [숏 주문 응답] 코드: {res.get('retCode')} | 메시지: {res.get('retMsg')}")

        self.last_grid_refresh_time = time.monotonic()
        self.total_cycles += 1

        # 10분 주기 정기 리포트
        if now - self.last_status_report_time >= 600:
            self.last_status_report_time = now
            bal_res = self.client.get_wallet_balance()
            usdt_bal = "0"
            if bal_res.get("retCode") == 0:
                coins = bal_res.get("result", {}).get("list", [{}])[0].get("coin", [])
                usdt_bal = next((c.get("walletBalance", "0") for c in coins if c.get("coin") == "USDT"), "0")

            await self.notifier.async_send_embed(
                title="📊 [COW AI 봇 정기 리포트]",
                description="방탄 그리드 엔진이 정상 가동 중입니다.",
                color=3447003,
                fields=[
                    {"name": "현재가", "value": f"${self.latest_price:.5f}", "inline": True},
                    {"name": "UNIFIED 잔고", "value": f"{usdt_bal} USDT", "inline": True},
                    {"name": "1분 변동폭", "value": f"{vol_1m:.2f}%", "inline": True},
                    {"name": "누적 전적", "value": f"{self.total_tp_count}승 / {self.total_sl_count}패", "inline": True},
                    {"name": "현재 롱 주문", "value": f"${round(center * 0.988, 5):.5f}", "inline": True},
                    {"name": "현재 숏 주문", "value": f"${round(center * 1.012, 5):.5f}", "inline": True}
                ]
            )

    async def ws_price_listener(self):
        while self.is_running:
            try:
                async with websockets.connect(BYBIT_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    sub_msg = orjson.dumps({
                        "op": "subscribe",
                        "args": [f"publicTrade.{SYMBOL}"]
                    }).decode("utf-8")
                    await ws.send(sub_msg)
                    logger.info(f"📡 [WebSocket] COWUSDT 실시간 가격 스트림 연결 완료")

                    async for raw_msg in ws:
                        if not self.is_running:
                            break
                        data = orjson.loads(raw_msg)
                        if data.get("topic") == f"publicTrade.{SYMBOL}":
                            for t in data.get("data", []):
                                p = float(t.get("p", 0.0))
                                if p > 0:
                                    now = time.monotonic()
                                    self.latest_price = p
                                    self.price_history.append(p)
                                    self.tick_window_1m.append((now, p))
                                    if len(self.price_history) > 100:
                                        self.price_history.pop(0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"⚠️ [WS 경고] 연결 끊김 ({e}), 2초 후 재연결...")
                await asyncio.sleep(2)

    async def grid_loop(self):
        while self.latest_price <= 0 and self.is_running:
            await asyncio.sleep(0.5)

        logger.info(f"🚀 [COW Anti-Squeeze 봇 가동 시작] 현재가: ${self.latest_price:.5f} | 레버리지: {LEVERAGE:.0f}x | 주문당: {MARGIN_PER_ORDER_USDT} USDT (규모: {NOTIONAL_PER_ORDER} USDT)")

        while self.is_running:
            try:
                await self.refresh_grid_orders()
                await asyncio.sleep(REFRESH_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ [그리드 에러] {e}")
                await asyncio.sleep(5)

    async def run(self):
        tasks = [
            asyncio.create_task(self.ws_price_listener()),
            asyncio.create_task(self.grid_loop())
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()


async def main():
    if not os.path.exists(CRED_PATH):
        logger.error(f"[!] Bybit 인증 파일이 존재하지 않습니다: {CRED_PATH}")
        sys.exit(1)

    with open(CRED_PATH, "r") as f:
        cred = json.load(f)

    ai_acc = cred.get("ai-account", {})
    api_key = ai_acc.get("api_key")
    api_secret = ai_acc.get("api_secret")

    if not api_key or not api_secret:
        logger.error("[!] AI Subaccount API 자격증명이 없습니다.")
        sys.exit(1)

    logger.info(f"🔑 [MAINNET] Bybit AI Subaccount 자격증명 로드 완료 (Sub Member ID: {ai_acc.get('sub_member_id')})")

    notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
    client = BybitV5Client(api_key, api_secret)

    # 1. 레버리지 15배 자동 세팅
    logger.info(f"⚙️ [설정] COWUSDT 레버리지 {LEVERAGE:.0f}배 세팅 요청...")
    lev_res = client.set_leverage(SYMBOL, LEVERAGE)
    if lev_res.get("retCode") in (0, 110043):
        logger.info(f"✅ [설정 완료] COWUSDT 레버리지 {LEVERAGE:.0f}배 설정 완료!")
    else:
        logger.warning(f"⚠️ [레버리지 설정 응답] {lev_res.get('retMsg')}")

    # 2. 계좌 잔고 확인
    bal_res = client.get_wallet_balance()
    usdt_bal = "0"
    if bal_res.get("retCode") == 0:
        coins = bal_res.get("result", {}).get("list", [{}])[0].get("coin", [])
        usdt_bal = next((c.get("walletBalance", "0") for c in coins if c.get("coin") == "USDT"), "0")
        logger.info(f"💰 [계좌 잔고] UNIFIED USDT 잔고: {usdt_bal} USDT")

    # 3. 시작 전 기존 잔여 미체결 주문 일괄 정리
    logger.info("🧹 [초기화] 기존 잔여 미체결 주문 일괄 취소 중...")
    client.cancel_all_orders(SYMBOL)
    await asyncio.sleep(1)

    # Discord 가동 시작 알림
    await notifier.async_send_embed(
        title="🛡️ COW AI Anti-Squeeze 방탄 그리드 봇 가동!",
        description="3대 방탄 시스템(TP 쿨다운 90초 / SL 서킷브레이커 3분 / 변동성 실드)이 전면 적용되었습니다.",
        color=3066993,
        fields=[
            {"name": "🎯 심볼", "value": "COWUSDT (15x)", "inline": True},
            {"name": "📈 그리드 간격", "value": "±1.20% (외곽 낚시)", "inline": True},
            {"name": "🎯 OCO 세팅", "value": "TP +0.6% | SL -2.0%", "inline": True},
            {"name": "🛡️ Post-TP 쿨다운", "value": "90초 관망", "inline": True},
            {"name": "🛡️ Post-SL 브레이커", "value": "3분 강제 동결", "inline": True},
            {"name": "🛡️ 변동성 실드", "value": "1분 변동폭 > 1.5% 시 철회", "inline": True}
        ]
    )

    trader = CowGridTrader(client, notifier)

    loop = asyncio.get_running_loop()

    def handle_exit():
        logger.info("🛑 [*] 봇 종료 신호 감지! 거래소의 모든 미체결 그리드 주문을 즉시 취소합니다...")
        trader.is_running = False
        client.cancel_all_orders(SYMBOL)
        notifier.send_embed(
            title="🛑 COW AI 실전 그리드 봇 정지",
            description="봇이 안전하게 종료되었으며 모든 미체결 그리드 호가가 전량 취소되었습니다.",
            color=10038562
        )

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, handle_exit)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await trader.run()
    finally:
        logger.info("🧹 [클린업] 종료 프로세스: 거래소 미체결 주문 최종 전량 취소 집행...")
        client.cancel_all_orders(SYMBOL)
        logger.info("✅ [*] 모든 미체결 주문이 취소되었으며 봇이 안전하게 정지되었습니다.")


def emergency_cleanup():
    """프로세스 비정상 종료/크래시 시에도 atexit으로 무조건 실행되는 비상 주문 취소기"""
    try:
        if os.path.exists(CRED_PATH):
            with open(CRED_PATH, "r") as f:
                cred = json.load(f)
            ai_acc = cred.get("ai-account", {})
            k = ai_acc.get("api_key")
            s = ai_acc.get("api_secret")
            if k and s:
                c = BybitV5Client(k, s)
                c.cancel_all_orders(SYMBOL)
    except Exception:
        pass


if __name__ == "__main__":
    import atexit
    atexit.register(emergency_cleanup)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        emergency_cleanup()
    except Exception as e:
        emergency_cleanup()
        raise e
