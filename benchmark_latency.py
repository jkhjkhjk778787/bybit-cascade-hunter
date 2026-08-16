#!/usr/bin/env python3
"""
Bybit 실시간 지연시간(Latency) 정밀 벤치마크 도구
1. TCP Handshake / ICMP Ping Latency
2. REST API Round Trip Time (RTT) - 10회 연속 측정 (Min / Avg / Max / Jitter)
3. Bybit V5 WebSocket Ping-Pong Latency (실시간 체결 스트림 통신 지연)
"""

import time
import socket
import json
import statistics
import urllib.request
import asyncio
import websockets

REST_HOST = "api.bybit.com"
REST_URL = "https://api.bybit.com/v5/market/time"
ORDERBOOK_URL = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=ACEUSDT&limit=1"
WS_URL = "wss://stream.bybit.com/v5/public/linear"


def test_tcp_latency(host="api.bybit.com", port=443, count=5):
    """TCP 3-way Handshake 연결 지연시간 측정"""
    latencies = []
    for _ in range(count):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        start = time.perf_counter()
        try:
            s.connect((host, port))
            latencies.append((time.perf_counter() - start) * 1000.0)
        except Exception as e:
            pass
        finally:
            s.close()
        time.sleep(0.05)
    return latencies


def test_rest_api_rtt(count=10):
    """Bybit REST API 왕복 딜레이 (HTTP Request/Response)"""
    latencies = []
    for _ in range(count):
        start = time.perf_counter()
        try:
            req = urllib.request.Request(REST_URL, headers={"User-Agent": "LatencyTester/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            latencies.append((time.perf_counter() - start) * 1000.0)
        except Exception as e:
            pass
        time.sleep(0.05)
    return latencies


async def test_ws_ping_latency(count=5):
    """Bybit WebSocket 프레임 핑퐁 왕복 지연시간"""
    latencies = []
    try:
        async with websockets.connect(WS_URL, ping_interval=None) as ws:
            for _ in range(count):
                start = time.perf_counter()
                pong_waiter = await ws.ping()
                await pong_waiter
                latencies.append((time.perf_counter() - start) * 1000.0)
                await asyncio.sleep(0.1)
    except Exception as e:
        print(f"[!] WS Latency Test Error: {e}")
    return latencies


async def main():
    print("================================================================================")
    print(" ⏱️ [Bybit Mainnet] 실시간 네트워크 & API 지연시간(Latency) 정밀 벤치마크")
    print("================================================================================\n")

    # 1. TCP Handshake
    print("[1/3] TCP 3-Way Handshake 지연시간 측정 중 (api.bybit.com:443)...")
    tcp_res = test_tcp_latency("api.bybit.com", 443, 5)
    if tcp_res:
        print(f"  ➔ 최소: {min(tcp_res):.2f}ms | 평균: {statistics.mean(tcp_res):.2f}ms | 최대: {max(tcp_res):.2f}ms")
    else:
        print("  ➔ 연결 실패")

    # 2. REST API RTT
    print("\n[2/3] REST API 왕복 딜레이 측정 중 (/v5/market/time 10회 연속)...")
    rest_res = test_rest_api_rtt(10)
    if rest_res:
        avg_rtt = statistics.mean(rest_res)
        min_rtt = min(rest_res)
        max_rtt = max(rest_res)
        jitter = statistics.stdev(rest_res) if len(rest_res) > 1 else 0.0
        print(f"  ➔ 최소: {min_rtt:.2f}ms | 평균: {avg_rtt:.2f}ms | 최대: {max_rtt:.2f}ms | 지터(Jitter): ±{jitter:.2f}ms")
    else:
        print("  ➔ REST API 호출 실패")

    # 3. WebSocket Ping-Pong
    print("\n[3/3] WebSocket 실시간 핑퐁(Ping-Pong) 지연시간 측정 중 (wss://stream.bybit.com)...")
    ws_res = await test_ws_ping_latency(5)
    if ws_res:
        print(f"  ➔ 최소: {min(ws_res):.2f}ms | 평균: {statistics.mean(ws_res):.2f}ms | 최대: {max(ws_res):.2f}ms")
    else:
        print("  ➔ WebSocket 핑퐁 실패")

    print("\n================================================================================")
    print(" 📊 [종합 레이턴시 등급 평가]")
    if rest_res and ws_res:
        avg_overall = (statistics.mean(rest_res) + statistics.mean(ws_res)) / 2.0
        if avg_overall < 30:
            rating = "🚀 초고속 (HFT / 초단타 최적화)"
        elif avg_overall < 80:
            rating = "⚡ 매우 빠름 (사전배치 그리드 및 단타 매매 완벽)"
        elif avg_overall < 150:
            rating = "✅ 양호 (일반 봇 및 조건부 OCO 주문 적합)"
        else:
            rating = "⚠️ 지연 발생 (원거리 해외 서버 가능성)"
        print(f"  ➔ 종합 평균 레이턴시: 약 {avg_overall:.2f} ms")
        print(f"  ➔ 트레이딩 환경 등급: {rating}")
    print("================================================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
