#!/usr/bin/env python3
"""
Bybit 체결 데이터 고속 분석 및 조회 도구
DuckDB의 컬럼형 벡터 연산을 활용하여 수천만 건의 틱 데이터를 0.1초 이내에 캔들 및 통계로 변환합니다.
(수집기 프로세스가 실행 중인 상태에서도 동시 읽기 지원)
"""

import argparse
import os
import shutil
import tempfile
import time
import duckdb
import pandas as pd

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1000)


def get_connection(db_path: str):
    """
    수집기 프로세스와의 동시성 락 충돌을 방지하며 안전하게 읽기 연결을 반환
    """
    if not os.path.exists(db_path):
        print(f"[!] DB 파일이 존재하지 않습니다: {db_path}")
        return None, None

    try:
        conn = duckdb.connect(database=db_path, read_only=True)
        return conn, None
    except Exception:
        # 수집기가 Write Lock을 쥐고 있는 경우 임시 스냅샷 복사본을 생성하여 즉시 조회
        temp_dir = tempfile.mkdtemp()
        temp_db = os.path.join(temp_dir, "snapshot.duckdb")
        shutil.copy2(db_path, temp_db)
        # WAL 파일이 있는 경우 함께 복사
        wal_path = db_path + ".wal"
        if os.path.exists(wal_path):
            shutil.copy2(wal_path, temp_db + ".wal")

        conn = duckdb.connect(database=temp_db, read_only=True)
        return conn, temp_dir


def cleanup_connection(conn, temp_dir):
    if conn:
        conn.close()
    if temp_dir and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def show_stats(db_path: str):
    """DB 전체 상태, 용량, 레코드 수 요약"""
    conn, temp_dir = get_connection(db_path)
    if not conn:
        return

    try:
        file_size_mb = os.path.getsize(db_path) / (1024 * 1024)

        t0 = time.perf_counter()
        summary = conn.execute("""
            SELECT 
                symbol,
                COUNT(*) AS total_trades,
                MIN(exec_time) AS first_trade,
                MAX(exec_time) AS last_trade,
                ROUND(SUM(size), 4) AS total_volume,
                ROUND(AVG(price), 4) AS avg_price
            FROM trades
            GROUP BY symbol
            ORDER BY total_trades DESC;
        """).df()
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"\n================ [ DB 용량 및 통계 ] ================")
        print(f"DB 파일 경로: {db_path}")
        print(f"디스크 용량 : {file_size_mb:.2f} MB")
        print(f"쿼리 소요시간: {elapsed:.2f} ms")
        print("------------------------------------------------------")
        if summary.empty:
            print("저장된 데이터가 아직 없습니다.")
        else:
            print(summary.to_string(index=False))
            total_rows = summary['total_trades'].sum()
            if total_rows > 0:
                bytes_per_row = (os.path.getsize(db_path)) / total_rows
                print(f"\n* 총 레코드: {total_rows:,}건 | 레코드당 평균 용량: 약 {bytes_per_row:.1f} Bytes")
        print("======================================================\n")
    finally:
        cleanup_connection(conn, temp_dir)


def show_recent_ticks(db_path: str, symbol: str, limit: int = 20):
    """최신 틱 데이터 조회"""
    conn, temp_dir = get_connection(db_path)
    if not conn:
        return

    try:
        t0 = time.perf_counter()
        query = """
            SELECT 
                exec_time,
                symbol,
                price,
                size,
                CASE WHEN side = 1 THEN 'Buy' ELSE 'Sell' END AS side,
                trade_id
            FROM trades
            WHERE symbol = ?
            ORDER BY exec_time DESC
            LIMIT ?;
        """
        df = conn.execute(query, [symbol.upper(), limit]).df()
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"\n[최신 틱 데이터 ({symbol.upper()}) - {len(df)}건] (소요시간: {elapsed:.2f} ms)")
        print(df.to_string(index=False))
    finally:
        cleanup_connection(conn, temp_dir)


def aggregate_candles(db_path: str, symbol: str, timeframe: str = "1 MINUTE", limit: int = 15):
    """
    고속 OHLCV 캔들 집계
    timeframe 예: '1 SECOND', '10 SECOND', '1 MINUTE', '5 MINUTE', '1 HOUR'
    """
    conn, temp_dir = get_connection(db_path)
    if not conn:
        return

    try:
        t0 = time.perf_counter()
        query = f"""
            SELECT 
                time_bucket(INTERVAL '{timeframe}', exec_time) AS candle_time,
                FIRST(price ORDER BY exec_time ASC) AS open,
                MAX(price) AS high,
                MIN(price) AS low,
                LAST(price ORDER BY exec_time ASC) AS close,
                ROUND(SUM(size), 4) AS volume,
                COUNT(*) AS trade_count,
                ROUND(SUM(CASE WHEN side = 1 THEN size ELSE 0 END), 4) AS buy_vol,
                ROUND(SUM(CASE WHEN side = 2 THEN size ELSE 0 END), 4) AS sell_vol
            FROM trades
            WHERE symbol = ?
            GROUP BY candle_time
            ORDER BY candle_time DESC
            LIMIT ?;
        """
        df = conn.execute(query, [symbol.upper(), limit]).df()
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"\n[{timeframe} 캔들 집계 (OHLCV) - {symbol.upper()}] (소요시간: {elapsed:.2f} ms)")
        print(df.to_string(index=False))
    except Exception as e:
        print(f"[!] 캔들 집계 에러: {e}")
    finally:
        cleanup_connection(conn, temp_dir)


def main():
    parser = argparse.ArgumentParser(description="Bybit 체결 데이터 분석 및 캔들 집계")
    parser.add_argument("--db", type=str, default="bybit_trades.duckdb", help="DuckDB 파일 경로")
    parser.add_argument("--symbol", type=str, default="COWUSDT", help="심볼명 (예: COWUSDT, ACEUSDT, BTCUSDT)")
    parser.add_argument("--mode", type=str, default="stats", choices=["stats", "ticks", "candle"], 
                        help="실행 모드: stats(요약 통계), ticks(최신 틱), candle(OHLCV 집계)")
    parser.add_argument("--timeframe", type=str, default="1 MINUTE", 
                        help="캔들 주기 (예: '1 SECOND', '10 SECOND', '1 MINUTE', '5 MINUTE')")
    parser.add_argument("--limit", type=int, default=15, help="출력 개수")

    args = parser.parse_args()

    if args.mode == "stats":
        show_stats(args.db)
    elif args.mode == "ticks":
        show_recent_ticks(args.db, args.symbol, args.limit)
    elif args.mode == "candle":
        aggregate_candles(args.db, args.symbol, args.timeframe, args.limit)


if __name__ == "__main__":
    main()
