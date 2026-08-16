#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 기존 중복 프로세스 정리
pkill -9 -f "collector.py" 2>/dev/null || true
sleep 1

echo "[*] Bybit 실시간 체결 수집 데몬을 시작합니다..."
nohup "$DIR/venv/bin/python" -u "$DIR/collector.py" --symbol AKEUSDT >> "$DIR/collector.log" 2>&1 &
echo "[*] 백그라운드 PID: $!"
