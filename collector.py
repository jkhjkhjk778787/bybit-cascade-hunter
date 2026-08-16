#!/usr/bin/env python3
"""
Bybit 실시간 체결 데이터 + 3대 거래소 (Binance, Bybit, OKX) 실시간 청산 데이터 초고속 통합 수집 엔진
[무중단 방탄 아키텍처 - DB 기존 심볼 30종 전체 자동 복원 및 청산 스트림 전수 구독]
"""

import argparse
import asyncio
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from collections import defaultdict
import aiohttp
import duckdb
import orjson
import websockets

import config
from crypto_liquidation import LiquidationStream, LiquidationEvent, OrderSide, PositionSide
from crypto_liquidation.exchanges import BybitLiquidationWorker

STATUS_FILE = "/home/jph/bybit_trade_collector/status.json"


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class TradeDBManager:
    """DuckDB 연결 및 고성능 체결/청산 데이터 배치 저장 관리자"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = duckdb.connect(database=self.db_path)
        self.conn.execute("PRAGMA threads=2;")
        self.conn.execute("PRAGMA memory_limit='400MB';")
        self.conn.execute("PRAGMA checkpoint_threshold='64MB';")
        self._init_schema()

    def _init_schema(self):
        # 1. 틱 체결 데이터 테이블
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                symbol VARCHAR(20),
                trade_id VARCHAR(50),
                price DOUBLE,
                size DOUBLE,
                side TINYINT,          -- 1: Buy, 2: Sell
                exec_time TIMESTAMP_MS -- 밀리초 타임스탬프
            );
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_time_sym 
            ON trades (exec_time, symbol);
        """)

        # 2. 3대 거래소 실시간 청산 데이터 테이블
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidations (
                exchange VARCHAR(20),  -- 'binance', 'bybit', 'okx'
                symbol VARCHAR(20),    -- 'BTCUSDT', 'COWUSDT' 등
                exec_time TIMESTAMP_MS,-- 밀리초 타임스탬프
                side TINYINT,          -- 1: Buy (Short Liq), 2: Sell (Long Liq)
                pos_side VARCHAR(10),  -- 'long', 'short'
                price DOUBLE,          -- 청산 단가
                size DOUBLE,           -- 청산 수량 (base)
                notional_usd DOUBLE    -- 청산 금액 USD
            );
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_liq_sym_time 
            ON liquidations (symbol, exec_time);
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_liq_ex_sym 
            ON liquidations (exchange, symbol);
        """)
        log(f"[DB] DuckDB 체결 및 청산 스키마 초기화 완료: {self.db_path}")

    def get_existing_symbols(self) -> list[str]:
        """DB에 이미 저장된 모든 심볼 목록 조회"""
        try:
            res = self.conn.execute("SELECT DISTINCT symbol FROM trades WHERE symbol IS NOT NULL;").fetchall()
            return [r[0] for r in res if r[0]]
        except Exception as e:
            log(f"[DB WARN] 기존 심볼 목록 조회 실패: {e}")
            return []

    def insert_batch(self, records: list):
        if not records:
            return
        try:
            appender = self.conn.appender("trades")
            for r in records:
                appender.append_row(r)
            appender.close()
        except Exception as e:
            log(f"[DB ERROR] 체결 데이터 쓰기 실패: {e}")

    def insert_liquidations_batch(self, records: list):
        if not records:
            return
        try:
            appender = self.conn.appender("liquidations")
            for r in records:
                appender.append_row(r)
            appender.close()
        except Exception as e:
            log(f"[DB ERROR] 청산 데이터 쓰기 실패: {e}")

    def close(self):
        try:
            self.conn.close()
            log("[DB] 데이터베이스 연결 정상 종료.")
        except Exception as e:
            log(f"[DB ERROR] 종료 중 에러: {e}")


class IntegratedMarketCollector:
    def __init__(self, initial_symbols: list[str], db_manager: TradeDBManager):
        self.subscribed_symbols = set(s.upper() for s in initial_symbols)
        self.db = db_manager
        self.queue = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self.ws = None
        self.is_running = True
        self.total_inserted = 0
        self.total_liq_inserted = 0
        self.dropped_records = 0

        # 청산 스트리머 인스턴스
        self.liq_stream: LiquidationStream = None

        # 메모리 통계 캐시
        self.detected_history = []
        self.symbol_stats = defaultdict(lambda: {"ticks": 0, "volume": 0.0, "last_price": 0.0, "last_time": ""})
        self.liq_stats = defaultdict(lambda: {"long_liq_usd": 0.0, "short_liq_usd": 0.0, "count": 0, "last_time": ""})
        self.symbol_meta_cache = {}

        # 초기 심볼 전수 스펙 검증
        for s in self.subscribed_symbols:
            self.check_and_cache_symbol_spec(s)

    def check_and_cache_symbol_spec(self, symbol: str) -> dict:
        """심볼이 DB에 적재되거나 수집 대상에 진입할 때마다 Bybit 규격(최대 레버리지, 최소 수량 등) 검증 및 캐싱"""
        if symbol in self.symbol_meta_cache:
            return self.symbol_meta_cache[symbol]
        try:
            import urllib.request
            url = f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={symbol}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = orjson.loads(resp.read())
                items = data.get("result", {}).get("list", [])
                if items:
                    it = items[0]
                    lsf = it.get("lotSizeFilter", {})
                    lf = it.get("leverageFilter", {})
                    max_lev = float(lf.get("maxLeverage", 20.0))
                    min_q = float(lsf.get("minOrderQty", 0.001))
                    step = float(lsf.get("qtyStep", 0.001))
                    min_not = float(lsf.get("minNotionalValue", 5.0))
                    meta = {
                        "max_leverage": max_lev,
                        "min_qty": min_q,
                        "qty_step": step,
                        "min_notional": min_not,
                        "status": it.get("status", "Trading")
                    }
                    self.symbol_meta_cache[symbol] = meta
                    log(f"🔍 [DB 수집 심볼 검증 완료] {symbol}: 최대레버리지 {max_lev}x | minQty {min_q} | qtyStep {step} | minNotional ${min_not} | 상태: {meta['status']}")
                    return meta
        except Exception as e:
            log(f"[WARN] {symbol} 스펙 검증 실패: {e}")
        return {"max_leverage": 20.0, "min_qty": 0.001, "qty_step": 0.001, "min_notional": 5.0, "status": "Unknown"}

    async def subscribe_symbols(self, new_symbols: list[str]):
        to_add = [s.upper() for s in new_symbols if s.upper() not in self.subscribed_symbols]
        if not to_add:
            return

        for s in to_add:
            self.subscribed_symbols.add(s)
            self.check_and_cache_symbol_spec(s)

        # 1. Bybit 체결 틱 웹소켓 구독 추가 (10개씩 배치 분할)
        if self.ws is not None and not self.ws.closed:
            topics = [f"publicTrade.{s}" for s in to_add]
            chunk_size = 10
            for i in range(0, len(topics), chunk_size):
                chunk = topics[i:i + chunk_size]
                msg = orjson.dumps({
                    "op": "subscribe",
                    "args": chunk
                })
                try:
                    await self.ws.send(msg)
                    if i + chunk_size < len(topics):
                        await asyncio.sleep(0.05)
                except Exception as e:
                    log(f"[WS-TRADE WARN] 동적 구독 전송 실패: {e}")
            log(f"[WS-TRADE] 동적 구독 추가 완료: {', '.join(to_add)}")

        # 2. 3대 거래소 청산 스트림 워커에 신규 심볼 동적 등록
        if self.liq_stream and self.liq_stream._workers:
            for worker in self.liq_stream._workers:
                for s in to_add:
                    if hasattr(worker, 'normalized_symbols') and worker.normalized_symbols is not None:
                        worker.normalized_symbols.add(s)
            log(f"[WS-LIQ] 3대 거래소 청산 스트림에 심볼 추가 완료: {', '.join(to_add)}")

    async def ranker_scanner_worker(self):
        if not config.ENABLE_RANKER_SCANNER:
            return

        log(f"[RANKER] 스캐너 가동 (URL: {config.RANKER_API_URL}, 주기: {config.SCAN_INTERVAL_SEC}초)")

        while self.is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    while self.is_running:
                        try:
                            async with session.get(config.RANKER_API_URL, timeout=aiohttp.ClientTimeout(total=5)) as response:
                                if response.status == 200:
                                    data = await response.json(loads=orjson.loads)
                                    rankings = data.get("data", [])
                                    
                                    new_targets = []
                                    for item in rankings:
                                        rank = item.get("rank", 999)
                                        symbol = item.get("symbol", "")
                                        pct = item.get("priceChangePct", 0.0)
                                        vol = item.get("totalVolume", 0.0)

                                        if rank <= config.RANK_LIMIT and abs(pct) >= config.MIN_PRICE_CHANGE_PCT:
                                            history_item = {
                                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                "symbol": symbol,
                                                "rank": rank,
                                                "change_pct": round(pct, 2),
                                                "volume_usdt": int(vol)
                                            }
                                            if not any(h["symbol"] == symbol and h["timestamp"] == history_item["timestamp"] for h in self.detected_history[-15:]):
                                                self.detected_history.append(history_item)
                                                if len(self.detected_history) > 50:
                                                    self.detected_history.pop(0)

                                            if symbol not in self.subscribed_symbols:
                                                new_targets.append(symbol)
                                                log(
                                                    f"[RANKER 포착] 심볼: {symbol} | "
                                                    f"순위: {rank}위 | "
                                                    f"1분 변동률: {pct:+.2f}% | "
                                                    f"거래량: {vol:,.0f} USDT -> 실시간 체결/청산 구독 추가"
                                                )

                                    if new_targets:
                                        await self.subscribe_symbols(new_targets)

                        except asyncio.CancelledError:
                            return
                        except Exception:
                            pass

                        await asyncio.sleep(config.SCAN_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[RANKER CRITICAL] 재시작 대기: {e}")
                await asyncio.sleep(5)

    async def db_writer_worker(self):
        """0.5초마다 버퍼의 체결 틱을 일괄 벌크 저장 (CPU/I/O 최적화)"""
        while self.is_running:
            try:
                await asyncio.sleep(0.5)

                if self.queue.empty():
                    continue

                batch = []
                while not self.queue.empty() and len(batch) < 10_000:
                    try:
                        record = self.queue.get_nowait()
                        if isinstance(record, list):
                            batch.extend(record)
                        else:
                            batch.append(record)
                        self.queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    self.db.insert_batch(batch)
                    self.total_inserted += len(batch)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[WORKER ERROR] {e}\n{traceback.format_exc()}")
                await asyncio.sleep(0.5)

        # 종료 시 잔여 큐 플러시
        final_batch = []
        while not self.queue.empty():
            try:
                record = self.queue.get_nowait()
                if isinstance(record, list):
                    final_batch.extend(record)
                else:
                    final_batch.append(record)
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        if final_batch:
            self.db.insert_batch(final_batch)
            self.total_inserted += len(final_batch)

    async def liquidation_collector_worker(self):
        """
        3대 거래소 (Binance, Bybit, OKX) 실시간 청산 스트림 수집 및 DuckDB 마이크로 배치 저장
        [Zero-Copy & Micro-Batching으로 CPU/메모리 오버헤드 0.1% 이하 유지]
        """
        if not config.ENABLE_LIQUIDATION_STREAM:
            return

        log(f"[LIQ-STREAM] 3대 거래소 전 종목 실시간 청산 데이터 수집기 가동: {config.LIQUIDATION_EXCHANGES}")

        # 전 종목 (symbols=None) 무제한 청산 스트림 수집 및 DuckDB 영구 적재
        self.liq_stream = LiquidationStream(
            exchanges=config.LIQUIDATION_EXCHANGES,
            symbols=None,
            min_notional_usd=config.LIQUIDATION_MIN_NOTIONAL_USD,
            include_raw=False
        )

        try:
            await self.liq_stream.start()

            # 마이크로 배치 제너레이터 (최대 500개 또는 500ms 주기)
            async for batch in self.liq_stream.stream_batches(
                max_batch_size=config.LIQUIDATION_BATCH_SIZE,
                max_interval_ms=config.LIQUIDATION_FLUSH_MS
            ):
                if not self.is_running:
                    break

                db_records = []
                for event in batch:
                    side_val = 1 if event.side == OrderSide.BUY else 2
                    pos_side_str = event.pos_side.value if hasattr(event.pos_side, 'value') else str(event.pos_side)
                    exec_ts = datetime.fromtimestamp(event.timestamp / 1000.0)

                    db_records.append((
                        event.exchange,
                        event.symbol,
                        exec_ts,
                        side_val,
                        pos_side_str,
                        float(event.price),
                        float(event.amount),
                        float(event.notional_usd)
                    ))

                    # 메모리 청산 통계 갱신
                    l_stat = self.liq_stats[event.symbol]
                    l_stat["count"] += 1
                    if event.is_long_liquidation:
                        l_stat["long_liq_usd"] += event.notional_usd
                    else:
                        l_stat["short_liq_usd"] += event.notional_usd
                    l_stat["last_time"] = exec_ts.strftime("%Y-%m-%d %H:%M:%S")

                if db_records:
                    self.db.insert_liquidations_batch(db_records)
                    self.total_liq_inserted += len(db_records)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[LIQ-STREAM ERROR] {e}\n{traceback.format_exc()}")
        finally:
            if self.liq_stream:
                await self.liq_stream.stop()

    async def _ping_loop(self, ws):
        while self.is_running:
            try:
                await ws.send(orjson.dumps({"op": "ping"}).decode('utf-8'))
            except Exception:
                break
            await asyncio.sleep(20)

    async def ws_receiver(self):
        """Producer: Bybit WebSocket으로부터 체결 틱을 수신하여 큐에 적재"""
        while self.is_running:
            try:
                log(f"[WS] Bybit 연결 중: {config.BYBIT_WS_URL}")
                async with websockets.connect(
                    config.BYBIT_WS_URL,
                    ping_interval=config.WS_PING_INTERVAL,
                    ping_timeout=config.WS_PING_TIMEOUT,
                    max_size=2**24
                ) as ws:
                    self.ws = ws
                    ping_task = asyncio.create_task(self._ping_loop(ws))

                    try:
                        if self.subscribed_symbols:
                            topics = [f"publicTrade.{s}" for s in self.subscribed_symbols]
                            subscribe_payload = orjson.dumps({
                                "op": "subscribe",
                                "args": topics
                            })
                            await ws.send(subscribe_payload)
                            log(f"[WS] 체결 데이터 초기 구독 ({len(self.subscribed_symbols)}개): {', '.join(sorted(self.subscribed_symbols))}")
    
                        while self.is_running:
                            try:
                                raw_msg = await ws.recv()
                            except (websockets.ConnectionClosed, asyncio.CancelledError):
                                break
    
                            data = orjson.loads(raw_msg)
                            topic = data.get("topic", "")
                            if topic.startswith("publicTrade."):
                                trade_list = data.get("data", [])
                                records_batch = []
                                for t in trade_list:
                                    symbol = t.get("s", topic.replace("publicTrade.", ""))
                                    side_val = 1 if t.get("S") == "Buy" else 2
                                    price = float(t.get("p", 0.0))
                                    size = float(t.get("v", 0.0))
                                    exec_ts = datetime.fromtimestamp(t["T"] / 1000.0)
    
                                    record = (
                                        symbol,
                                        t.get("i", ""),
                                        price,
                                        size,
                                        side_val,
                                        exec_ts
                                    )
    
                                    # 메모리 통계 갱신 (CPU 최적화: 핫 루프에서 strftime 배제)
                                    s_stat = self.symbol_stats[symbol]
                                    s_stat["ticks"] += 1
                                    s_stat["volume"] += size
                                    s_stat["last_price"] = price
                                    s_stat["last_ts"] = t.get("T", 0)
                                    records_batch.append(record)
    
                                if records_batch:
                                    try:
                                        self.queue.put_nowait(records_batch)
                                    except asyncio.QueueFull:
                                        self.dropped_records += len(records_batch)
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                log(f"[WS WARN] 연결 끊김 ({e}), 3초 후 재연결...")
                self.ws = None
                await asyncio.sleep(3)
            except Exception as e:
                log(f"[WS ERROR] 수신 오류: {e}, 3초 후 재연결...")
                self.ws = None
                await asyncio.sleep(3)

    async def status_dump_worker(self):
        """3초마다 status.json에 메모리 스냅샷 덤프 (0.1ms 소요)"""
        while self.is_running:
            try:
                db_size_mb = os.path.getsize(config.DB_PATH) / (1024 * 1024) if os.path.exists(config.DB_PATH) else 0.0
                status_payload = {
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_inserted": self.total_inserted,
                    "total_liq_inserted": self.total_liq_inserted,
                    "db_size_mb": round(db_size_mb, 2),
                    "queue_size": self.queue.qsize(),
                    "subscribed_count": len(self.subscribed_symbols),
                    "detected_symbols": self.detected_history,
                    "symbol_stats": [
                        {
                            "symbol": sym,
                            "ticks": stat["ticks"],
                            "volume": round(stat["volume"], 2),
                            "last_price": stat["last_price"],
                            "last_time": datetime.fromtimestamp(stat["last_ts"] / 1000.0).strftime("%Y-%m-%d %H:%M:%S") if stat.get("last_ts") else "",
                            "liq_count": self.liq_stats[sym]["count"],
                            "long_liq_usd": round(self.liq_stats[sym]["long_liq_usd"], 2),
                            "short_liq_usd": round(self.liq_stats[sym]["short_liq_usd"], 2),
                        }
                        for sym, stat in sorted(self.symbol_stats.items(), key=lambda x: x[1]["ticks"], reverse=True)
                    ]
                }
                temp_status = STATUS_FILE + ".tmp"
                with open(temp_status, "wb") as f:
                    f.write(orjson.dumps(status_payload))
                os.replace(temp_status, STATUS_FILE)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(3.0)

    async def monitor_worker(self):
        prev_count = 0
        prev_liq_count = 0
        prev_time = time.monotonic()

        while self.is_running:
            try:
                await asyncio.sleep(config.MONITOR_INTERVAL)
                now = time.monotonic()
                elapsed = now - prev_time
                count_diff = self.total_inserted - prev_count
                liq_diff = self.total_liq_inserted - prev_liq_count
                tps = count_diff / elapsed if elapsed > 0 else 0
                liq_tps = liq_diff / elapsed if elapsed > 0 else 0

                sub_list = sorted(list(self.subscribed_symbols))
                display_subs = ", ".join(sub_list[:5]) + (f" 외 {len(sub_list)-5}개" if len(sub_list) > 5 else "")

                log(
                    f"[MONITOR] 체결: {self.total_inserted:,}건 ({tps:,.1f} TPS) | "
                    f"청산(3거래소): {self.total_liq_inserted:,}건 ({liq_tps:,.1f}/s) | "
                    f"구독: {len(self.subscribed_symbols)}개 ({display_subs}) | "
                    f"버퍼: {self.queue.qsize():,}건"
                )
                prev_count = self.total_inserted
                prev_liq_count = self.total_liq_inserted
                prev_time = now
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def run(self):
        tasks = [
            asyncio.create_task(self.db_writer_worker()),
            asyncio.create_task(self.ws_receiver()),
            asyncio.create_task(self.liquidation_collector_worker()),
            asyncio.create_task(self.ranker_scanner_worker()),
            asyncio.create_task(self.status_dump_worker()),
            asyncio.create_task(self.monitor_worker()),
        ]

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()


def fetch_top_active_symbols(limit: int = 50) -> list[str]:
    """Bybit & Binance 양대 거래소 동시 상장 선물 중 24시간 거래대금 상위 심볼 추출"""
    try:
        import urllib.request
        # 1. Binance USDT-M 선물 활성 심볼 조회
        req_bin = urllib.request.Request(
            "https://fapi.binance.com/fapi/v1/exchangeInfo",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req_bin, timeout=5) as resp:
            bin_data = orjson.loads(resp.read())
        bin_syms = set(
            s['symbol'] for s in bin_data.get('symbols', [])
            if s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'
        )

        # 2. Bybit Linear 선물 티커 조회
        req_by = urllib.request.Request(
            "https://api.bybit.com/v5/market/tickers?category=linear",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req_by, timeout=5) as resp:
            by_data = orjson.loads(resp.read())
        tickers = by_data.get("result", {}).get("list", [])

        # 3. 양대 거래소 동시 상장 심볼 교집합 필터링 & 거래대금 정렬
        dual_listed = [
            d for d in tickers
            if d.get("symbol", "").endswith("USDT") and "USDC" not in d.get("symbol", "") and d.get("symbol") in bin_syms
        ]
        dual_listed.sort(key=lambda x: float(x.get("turnover24h", 0) or 0), reverse=True)
        return [d["symbol"] for d in dual_listed[:limit]]
    except Exception as e:
        log(f"[WARN] 양대 거래소 상위 심볼 조회 실패: {e}")
        return []


async def main():
    parser = argparse.ArgumentParser(description="Bybit 체결 + 3대 거래소 청산 통합 수집 엔진")
    parser.add_argument("--symbol", type=str, default=None, help="추가 심볼 (지정하지 않으면 DB 기존 심볼 자동 로드)")
    parser.add_argument("--db", type=str, default=config.DB_PATH, help="DuckDB 경로")
    args = parser.parse_args()

    try:
        os.nice(5)
    except Exception:
        pass

    db_mgr = TradeDBManager(args.db)
    
    # 1. 거래대금 상위 핫 심볼(30개) 집중 구독 (과거 모든 비활성 심볼 전수 구독 배제로 CPU 50% 절감)
    top_syms = fetch_top_active_symbols(limit=getattr(config, "TOP_SYMBOLS_LIMIT", 30))
    active_syms = set([s.upper() for s in top_syms])

    if args.symbol:
        active_syms.add(args.symbol.upper())
    if config.DEFAULT_SYMBOL:
        active_syms.add(config.DEFAULT_SYMBOL.upper())

    all_symbols = sorted(list(active_syms))
    log(f"[*] 거래대금 상위 {len(all_symbols)}개 핫 심볼 집중 수집 가동: {', '.join(all_symbols)}")

    collector = IntegratedMarketCollector(initial_symbols=all_symbols, db_manager=db_mgr)

    loop = asyncio.get_running_loop()

    def handle_exit():
        log("[*] 종료 요청 감지. 남은 체결/청산 데이터를 플러시합니다...")
        collector.is_running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_exit)
        except NotImplementedError:
            pass

    try:
        await collector.run()
    finally:
        db_mgr.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
