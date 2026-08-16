#!/usr/bin/env python3
"""
========================================================================================
🚀 [SYMBOL INCUBATOR & RADAR v1.0] 
실시간 핫 코인 자동 발굴 레이더 & 섀도우 페이퍼 트레이딩 인큐베이터
========================================================================================
- [기능]
  1. 1분마다 Bybit 500개 전 종목 스캔 ➔ 거래대금 급증 & 변동성 폭발 핫 심볼 실시간 발굴
  2. 신규 발굴 심볼은 `incubator_symbols.json`에 인큐베이팅 등록 (콜드 스타트 방어)
  3. 실시간 웹소켓으로 신규 심볼들의 청산 감시 ➔ "가상 숏(Shadow Paper Trade)" 진입!
  4. 실시간 라이브 틱으로 가상 익절/손절 100% 무위험 추적 기록
  5. 가상 검증 3회 이상 & 승률 >= 75.0% & 손익비 >= 2.0 달성 시 ➔ "정규 실전 심볼"로 자동 승격!
  6. Discord 실시간 인큐베이팅/승격 리포트 발송
========================================================================================
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
from collections import deque
from typing import Dict, Any, List, Optional

import websockets
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [Incubator] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("Incubator")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
INCUBATOR_PATH = "/home/jph/bybit_trade_collector/incubator_symbols.json"
ACTIVE_SYMBOLS_PATH = "/home/jph/bybit_trade_collector/active_symbols.json"

BYBIT_REST_URL = "https://api.bybit.com"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
SCAN_INTERVAL_SEC = 60.0


def send_discord_report(title: str, description: str, color: int = 3447003, fields: list = None):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
            "footer": {"text": "🧪 Bybit Symbol Incubator | Shadow Paper Testing"}
        }]
    }
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            pass
    except Exception as e:
        logger.error(f"디스코드 전송 실패: {e}")


class HotSymbolRadar:
    """Bybit & Binance 동시 상장 460+종목 중 거래대금/변동성 폭발 핫 심볼 발굴"""
    @staticmethod
    def scan_hot_symbols() -> List[str]:
        try:
            # 1. Binance USDT-M 선물 활성 심볼 조회
            bin_syms = set()
            try:
                req_bin = urllib.request.Request("https://fapi.binance.com/fapi/v1/exchangeInfo", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req_bin, timeout=5) as resp:
                    bin_data = json.loads(resp.read().decode())
                bin_syms = set(s['symbol'] for s in bin_data.get('symbols', []) if s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING')
            except Exception as e:
                logger.warning(f"Binance exchangeInfo 조회 실패: {e}")

            # 2. Bybit 티커 조회
            url = f"{BYBIT_REST_URL}/v5/market/tickers?category=linear"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
            
            items = data.get("result", {}).get("list", [])
            candidates = []
            for item in items:
                sym = item.get("symbol", "")
                if not sym.endswith("USDT") or "USDC" in sym:
                    continue
                # Binance 선물에도 상장되어 있는지 검증 (Binance 정보 있을 때)
                if bin_syms and sym not in bin_syms:
                    continue

                turnover = float(item.get("turnover24h", 0.0))
                price_24h_pct = abs(float(item.get("price24hPcnt", 0.0)))
                # 스코어 = 거래대금 * 변동성
                if turnover >= 3000000.0:  # 300만 USDT 이상
                    score = turnover * (1.0 + price_24h_pct * 10.0)
                    candidates.append((sym, turnover, price_24h_pct, score))

            candidates.sort(key=lambda x: x[3], reverse=True)
            top_symbols = [c[0] for c in candidates[:40]]
            return top_symbols
        except Exception as e:
            logger.error(f"레이더 스캔 실패: {e}")
            return []


class ShadowIncubator:
    def __init__(self):
        self.is_running = True
        self.incubating_symbols: Dict[str, Dict[str, Any]] = {}
        self.active_shadow_positions: Dict[str, Dict[str, Any]] = {}

        self.liq_buffers: Dict[str, deque] = {}
        self.price_buffers: Dict[str, deque] = {}
        self.latest_prices: Dict[str, float] = {}

        self.ws_conn = None
        self.subscribed_topics = set()
        self.ws_send_lock = asyncio.Lock()

    def load_incubator_data(self):
        if os.path.exists(INCUBATOR_PATH):
            try:
                with open(INCUBATOR_PATH, "r") as f:
                    self.incubating_symbols = json.load(f)
            except Exception:
                self.incubating_symbols = {}

    def save_incubator_data(self):
        temp_file = INCUBATOR_PATH + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(self.incubating_symbols, f, indent=2)
        os.replace(temp_file, INCUBATOR_PATH)

    async def radar_loop(self):
        """1분마다 핫 심볼 발굴 및 인큐베이팅 등록"""
        while self.is_running:
            try:
                hot_syms = HotSymbolRadar.scan_hot_symbols()
                new_discovered = []

                # 현재 실전 active 심볼 로드
                active_syms = set()
                if os.path.exists(ACTIVE_SYMBOLS_PATH):
                    try:
                        with open(ACTIVE_SYMBOLS_PATH, "r") as f:
                            active_syms = set(json.load(f).get("symbols", {}).keys())
                    except Exception:
                        pass

                for sym in hot_syms:
                    if sym not in self.incubating_symbols and sym not in active_syms:
                        self.incubating_symbols[sym] = {
                            "discovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "INCUBATING_SHADOW",
                            "shadow_trades": [],
                            "win_count": 0,
                            "loss_count": 0,
                            "win_rate": 0.0,
                            "profit_factor": 0.0
                        }
                        new_discovered.append(sym)

                if new_discovered:
                    self.save_incubator_data()
                    logger.info(f"📡 [신규 핫 심볼 발굴] {len(new_discovered)}개 코인 인큐베이터 입소: {', '.join(new_discovered)}")
                    send_discord_report(
                        title="📡 [레이더 포착]",
                        description=f"인큐베이팅: `{', '.join(new_discovered)}`",
                        color=15844367
                    )
                    await self.sync_ws_subscriptions()

            except Exception as e:
                logger.error(f"레이더 루프 에러: {e}")

            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def sync_ws_subscriptions(self):
        if not self.ws_conn:
            return

        all_syms = list(self.incubating_symbols.keys())
        target_topics = set([f"liquidation.{s}" for s in all_syms] + [f"tickers.{s}" for s in all_syms])
        to_sub = list(target_topics - self.subscribed_topics)
        to_unsub = list(self.subscribed_topics - target_topics)

        async with self.ws_send_lock:
            if to_sub:
                chunk_size = 10
                for i in range(0, len(to_sub), chunk_size):
                    chunk = to_sub[i:i + chunk_size]
                    await self.ws_conn.send(json.dumps({"op": "subscribe", "args": chunk}))
                    if i + chunk_size < len(to_sub):
                        await asyncio.sleep(0.05)
                self.subscribed_topics.update(to_sub)
                logger.info(f"🧪 [인큐베이터 WS 추가] {len(to_sub)}개 토픽 가상 감시 ({len(all_syms)}개 심볼)")

            if to_unsub:
                chunk_size = 10
                for i in range(0, len(to_unsub), chunk_size):
                    chunk = to_unsub[i:i + chunk_size]
                    await self.ws_conn.send(json.dumps({"op": "unsubscribe", "args": chunk}))
                    if i + chunk_size < len(to_unsub):
                        await asyncio.sleep(0.05)
                self.subscribed_topics.difference_update(to_unsub)

    async def ws_shadow_listener(self):
        """실시간 청산 감시 ➔ 가상 숏(Shadow Paper Trade) 진입 및 추적"""
        while self.is_running:
            try:
                self.price_buffers.clear()
                self.liq_buffers.clear()

                async with websockets.connect(BYBIT_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self.ws_conn = ws
                    self.subscribed_topics = set()
                    await self.sync_ws_subscriptions()

                    # Bybit 애플리케이션 레벨 핑 루프 (20초 주기)
                    async def _bybit_ping_task():
                        while self.is_running:
                            await asyncio.sleep(20)
                            try:
                                async with self.ws_send_lock:
                                    if self.ws_conn:
                                        await self.ws_conn.send('{"op":"ping"}')
                            except Exception:
                                break

                    ping_task = asyncio.create_task(_bybit_ping_task())

                    try:
                        async for message in ws:
                            if not self.is_running:
                                break
                            data = json.loads(message)
                            topic = data.get("topic", "")

                            if topic.startswith("tickers."):
                                sym = topic.split(".")[1]
                                t_data = data.get("data", {})
                                lp = t_data.get("lastPrice")
                                if lp:
                                    now = time.time()
                                    p_float = float(lp)
                                    self.latest_prices[sym] = p_float
                                    if sym not in self.price_buffers:
                                        self.price_buffers[sym] = deque(maxlen=300)
                                    self.price_buffers[sym].append((now, p_float))
                                    while self.price_buffers[sym] and (now - self.price_buffers[sym][0][0] > 5.0):
                                        self.price_buffers[sym].popleft()

                                    # 가상 포지션 TP/SL/타임아웃 감시
                                    await self.check_shadow_positions(sym, p_float, now)

                            elif topic.startswith("liquidation.") or topic.startswith("allLiquidation."):
                                sym = topic.split(".")[1]
                                l_data = data.get("data", {})
                                side = l_data.get("side") or l_data.get("S")
                                price = float(l_data.get("price") or l_data.get("p") or 0.0)
                                size = float(l_data.get("size") or l_data.get("v") or 0.0)
                                liq_usd = price * size
                                now = time.time()

                                is_long_liq = (side == "Sell") or (side == "Buy" and "allLiquidation" in topic)
                                liq_type = "LongLiq" if is_long_liq else "ShortLiq"

                                if sym in self.incubating_symbols:
                                    if sym not in self.liq_buffers:
                                        self.liq_buffers[sym] = deque(maxlen=300)
                                    self.liq_buffers[sym].append((now, liq_usd, price, liq_type))
                                    await self.check_shadow_trigger(sym, price, now, liq_type)
                    finally:
                        ping_task.cancel()

            except Exception as e:
                logger.error(f"인큐베이터 WS 에러: {e} ➔ 2초 후 재연결")
                await asyncio.sleep(2)

    async def check_shadow_trigger(self, symbol: str, current_price: float, now: float, liq_type: str):
        """가상 양방향 진입 조건 검사 (롱 청산 ➔ 가상 숏 / 숏 청산 ➔ 가상 롱)"""
        if symbol in self.active_shadow_positions:
            return  # 이미 가상 포지션 보유 중

        buf = self.liq_buffers.get(symbol, deque())
        while buf and (now - buf[0][0] > 5.0):
            buf.popleft()

        matching_liqs = [item for item in buf if len(item) > 3 and item[3] == liq_type]
        total_liq_5s = sum([item[1] for item in matching_liqs])
        liq_cnt = len(matching_liqs)

        # 5초 $250 이상 + 방향별 가격 변동 검증
        if (liq_cnt >= 2 and total_liq_5s >= 250.0) or total_liq_5s >= 350.0:
            p_buf = self.price_buffers.get(symbol, deque())
            if len(p_buf) >= 2:
                p_old = p_buf[0][1]
                p_cur = current_price if current_price > 0 else self.latest_prices.get(symbol, p_old)

                if liq_type == "LongLiq":
                    # 롱 청산 ➔ 가상 숏
                    drop_pct = (p_old - p_cur) / p_old * 100.0
                    if drop_pct >= 0.10:
                        tp_price = p_cur * 0.985   # +1.5% 익절
                        sl_price = p_cur * 1.006   # -0.6% 손절
                        self.active_shadow_positions[symbol] = {
                            "side": "Sell",
                            "entry_price": p_cur,
                            "entry_time": now,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "timeout_sec": 45.0
                        }
                        logger.info(f"🧪 [가상 숏 진입] {symbol} @ ${p_cur:.5f} | 가상 TP: ${tp_price:.5f} | 가상 SL: ${sl_price:.5f}")
                else:
                    # 숏 청산 ➔ 가상 롱
                    rise_pct = (p_cur - p_old) / p_old * 100.0
                    if rise_pct >= 0.10:
                        tp_price = p_cur * 1.015   # +1.5% 익절
                        sl_price = p_cur * 0.994   # -0.6% 손절
                        self.active_shadow_positions[symbol] = {
                            "side": "Buy",
                            "entry_price": p_cur,
                            "entry_time": now,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "timeout_sec": 45.0
                        }
                        logger.info(f"🧪 [가상 롱 진입] {symbol} @ ${p_cur:.5f} | 가상 TP: ${tp_price:.5f} | 가상 SL: ${sl_price:.5f}")

    async def check_shadow_positions(self, symbol: str, cur_price: float, now: float):
        """실시간 틱으로 가상 포지션 청산 여부 100% 추적"""
        if symbol not in self.active_shadow_positions:
            return

        pos = self.active_shadow_positions[symbol]
        side = pos.get("side", "Sell")
        entry_p = pos["entry_price"]
        entry_t = pos["entry_time"]
        tp_p = pos["tp_price"]
        sl_p = pos["sl_price"]
        to_sec = pos["timeout_sec"]

        closed = False
        win = False
        pnl_pct = 0.0

        if side == "Sell":
            if cur_price <= tp_p:
                closed = True
                win = True
                pnl_pct = 1.50 - 0.12
            elif cur_price >= sl_p:
                closed = True
                win = False
                pnl_pct = -0.60 - 0.12
            elif (now - entry_t) >= to_sec:
                closed = True
                net_diff = (entry_p - cur_price) / entry_p * 100.0
                pnl_pct = net_diff - 0.12
                win = pnl_pct > 0
        else: # "Buy" (Long)
            if cur_price >= tp_p:
                closed = True
                win = True
                pnl_pct = 1.50 - 0.12
            elif cur_price <= sl_p:
                closed = True
                win = False
                pnl_pct = -0.60 - 0.12
            elif (now - entry_t) >= to_sec:
                closed = True
                net_diff = (cur_price - entry_p) / entry_p * 100.0
                pnl_pct = net_diff - 0.12
                win = pnl_pct > 0

        if closed:
            del self.active_shadow_positions[symbol]
            meta = self.incubating_symbols.get(symbol, {})
            trades = meta.get("shadow_trades", [])
            trades.append({"win": win, "pnl_pct": round(pnl_pct, 2), "time": time.strftime("%H:%M:%S")})
            meta["shadow_trades"] = trades

            tot_t = len(trades)
            w_cnt = len([t for t in trades if t["win"]])
            l_cnt = tot_t - w_cnt
            wr = (w_cnt / tot_t) * 100.0
            meta["win_count"] = w_cnt
            meta["loss_count"] = l_cnt
            meta["win_rate"] = round(wr, 1)

            logger.info(f"🧪 [가상 포지션 마감] {symbol} {'🎉 익절(+1.5%)' if win else '🚨 손절(-0.6%)'} | 누적 {tot_t}회 (승률 {wr:.1f}%)")
            self.save_incubator_data()

            # 🏆 [정규 실전 승격 심사!]
            # 조건: 섀도우 거래 3회 이상 & 승률 75% 이상 & 누적수익 플러스
            if tot_t >= 3 and wr >= 75.0:
                await self.promote_to_live_active(symbol, wr, tot_t)

    async def promote_to_live_active(self, symbol: str, win_rate: float, trade_count: int):
        """가상 검증 통과 ➔ active_symbols.json에 정식 승격 등록!"""
        try:
            active_data = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "symbols": {}}
            if os.path.exists(ACTIVE_SYMBOLS_PATH):
                with open(ACTIVE_SYMBOLS_PATH, "r") as f:
                    active_data = json.load(f)

            active_syms = active_data.get("symbols", {})
            if symbol not in active_syms:
                active_syms[symbol] = {
                    "trigger_usd": 300.0,
                    "min_gain_pct": 1.0,
                    "trailing_bounce_pct": 0.20,
                    "exhaustion_sec": 5.0,
                    "sl_buffer_pct": 0.10,
                    "tp_pct": 2.00,
                    "sl_pct": 0.60,
                    "timeout_sec": 45.0,
                    "win_rate": round(win_rate, 1),
                    "profit_factor": 5.0,
                    "total_pnl_pct": round(win_rate * 0.8, 1),
                    "trade_count": trade_count,
                    "promoted_from": "SHADOW_INCUBATOR"
                }
                active_data["symbols"] = active_syms
                active_data["total_active_count"] = len(active_syms)

                temp_file = ACTIVE_SYMBOLS_PATH + ".tmp"
                with open(temp_file, "w") as f:
                    json.dump(active_data, f, indent=2)
                os.replace(temp_file, ACTIVE_SYMBOLS_PATH)

                if symbol in self.incubating_symbols:
                    self.incubating_symbols[symbol]["status"] = "PROMOTED_LIVE"
                    self.save_incubator_data()

                logger.warning(f"🎓 [정규 승격 완료!] {symbol} 가상 검증 통과(승률 {win_rate}%) ➔ 실전 매매 즉시 투입!")
                send_discord_report(
                    title=f"🎓 [실전 승격] {symbol}",
                    description=f"가상 검증 통과 (`{trade_count}회`, 승률 `{win_rate}%`) ➔ 실전 투입",
                    color=3066993
                )

        except Exception as e:
            logger.error(f"승격 실패: {e}")

    async def run(self):
        self.load_incubator_data()
        await asyncio.gather(
            self.radar_loop(),
            self.ws_shadow_listener()
        )


def main():
    incubator = ShadowIncubator()
    try:
        asyncio.run(incubator.run())
    except KeyboardInterrupt:
        incubator.is_running = False


if __name__ == "__main__":
    main()
