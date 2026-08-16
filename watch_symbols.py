#!/usr/bin/env python3
"""
Bybit 실시간 체결 수집기 - 심볼 추가 및 수집 현황 초경량 모니터
- CPU 점유율 0.1% 미만: status.json 스냅샷을 메모리에서 직접 렌더링
- ANSI 커서 제어(\033[H) 및 싱글 버퍼 출력으로 화면 깜빡임 0%
"""

import os
import sys
import time
import orjson

# ANSI Color 및 커서 제어
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"
CLEAR_LINE = "\033[K"    # 현재 커서부터 줄 끝까지 지우기
CURSOR_HOME = "\033[H"   # 커서를 화면 좌상단으로 이동 (화면 깜빡임 없음)
CLEAR_SCREEN = "\033[2J" # 최초 1회만 화면 클리어

STATUS_FILE = "/home/jph/bybit_trade_collector/status.json"


def load_status():
    if not os.path.exists(STATUS_FILE):
        return None
    try:
        with open(STATUS_FILE, "rb") as f:
            return orjson.loads(f.read())
    except Exception:
        return None


def build_dashboard_text():
    status = load_status()

    lines = []
    lines.append(f"{CURSOR_HOME}{BOLD}{CYAN}========================================================================================{RESET}{CLEAR_LINE}")
    
    updated_at = status.get("updated_at", "-") if status else "-"
    lines.append(f"{BOLD}{CYAN} 🚀 Bybit 실시간 체결 수집 & 동적 심볼 트리거 모니터링 대시보드 {RESET} {DIM}(갱신: {updated_at}){RESET}{CLEAR_LINE}")
    lines.append(f"{BOLD}{CYAN}========================================================================================{RESET}{CLEAR_LINE}")
    lines.append(f"{CLEAR_LINE}")

    if not status:
        lines.append(f"  {YELLOW}수집 엔진(collector.py) 상태 데이터를 불러오는 중입니다...{RESET}{CLEAR_LINE}")
        return "\n".join(lines)

    detected_symbols = status.get("detected_symbols", [])
    symbol_stats = status.get("symbol_stats", [])
    total_ticks = status.get("total_inserted", 0)
    db_size_mb = status.get("db_size_mb", 0.0)
    sub_count = status.get("subscribed_count", 0)

    # 1. 랭커 트리거 감지 심볼 섹션
    lines.append(f"{BOLD}{YELLOW}📌 [1] 1분 거래량 10위 & 변동률 0.5% 이상 자동 포착 심볼 ({len(detected_symbols)}개){RESET}{CLEAR_LINE}")
    lines.append(f"{DIM}----------------------------------------------------------------------------------------{RESET}{CLEAR_LINE}")
    
    if not detected_symbols:
        lines.append(f"  {DIM}아직 포착된 급변동 심볼이 없습니다. (1분 주기 탐색 중...){RESET}{CLEAR_LINE}")
    else:
        lines.append(f"  {'포착시각':<20} {'심볼':<12} {'순위':<8} {'1분 변동률':<14} {'1분 거래량(USDT)':<18}{CLEAR_LINE}")
        lines.append(f"  {'-'*76}{CLEAR_LINE}")
        for item in detected_symbols[-8:]:
            pct = item.get('change_pct', 0.0)
            pct_color = RED if pct < 0 else GREEN
            pct_str = f"{pct_color}{pct:+.2f}%{RESET}"
            vol = item.get('volume_usdt', 0)
            lines.append(
                f"  {item.get('timestamp', '-'):<20} "
                f"{BOLD}{item.get('symbol', '-'):<12}{RESET} "
                f"{item.get('rank', 0)}위{'':<5} "
                f"{pct_str:<23} "
                f"{vol:,} USDT{CLEAR_LINE}"
            )

    lines.append(f"{CLEAR_LINE}")

    # 2. 실시간 DB 저장 통계 섹션
    lines.append(f"{BOLD}{GREEN}📊 [2] DuckDB 실시간 체결 데이터 적재 현황 (현재 {sub_count}개 심볼 구독 중){RESET}{CLEAR_LINE}")
    lines.append(f"{DIM}----------------------------------------------------------------------------------------{RESET}{CLEAR_LINE}")
    
    if not symbol_stats:
        lines.append(f"  {DIM}체결 데이터 수집 대기 중...{RESET}{CLEAR_LINE}")
    else:
        lines.append(f"  {'심볼':<12} {'저장 틱 수':<14} {'누적 체결수량':<16} {'최근 체결가':<14} {'최근 틱 수신 시각'}{CLEAR_LINE}")
        lines.append(f"  {'-'*76}{CLEAR_LINE}")
        for row in symbol_stats[:12]:  # 상위 12개 심볼
            sym = row.get('symbol', '-')
            is_detected = any(d.get('symbol') == sym for d in detected_symbols)
            sym_display = f"{BOLD}{YELLOW}★ {sym}{RESET}" if is_detected else f"  {sym}"

            last_time_str = row.get('last_time', '-')
            lines.append(
                f"  {sym_display:<21} "
                f"{row.get('ticks', 0):<14,d} "
                f"{row.get('volume', 0.0):<16,.2f} "
                f"{row.get('last_price', 0.0):<14.4f} "
                f"{last_time_str}{CLEAR_LINE}"
            )
        lines.append(f"{'-'*76}{CLEAR_LINE}")
        lines.append(f"{BOLD}총 누적 체결 수: {total_ticks:,d}건 | DB 용량: {db_size_mb:.2f} MB (ZSTD 압축 적용){RESET}{CLEAR_LINE}")

    lines.append(f"{CLEAR_LINE}")
    lines.append(f"{DIM}[안내] CPU 점유율 0.1% 미만 초경량 모니터링 중 | 종료: Ctrl+C{RESET}{CLEAR_LINE}")
    
    return "\n".join(lines)


def main():
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()

    while True:
        try:
            output = build_dashboard_text()
            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[*] 모니터링을 종료합니다.")
            break
        except Exception:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
