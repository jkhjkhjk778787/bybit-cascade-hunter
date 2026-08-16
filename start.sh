#!/usr/bin/env bash
# Bybit 실시간 체결 수집기 실행 스크립트
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYMBOL="${1:-AKEUSDT}"

echo "=========================================="
echo " Bybit Trade Collector 시작 (심볼: $SYMBOL)"
echo "=========================================="

exec "$DIR/venv/bin/python" "$DIR/collector.py" --symbol "$SYMBOL"
