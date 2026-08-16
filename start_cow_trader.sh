#!/bin/bash
# COW 실전 그리드 트레이더 데몬 시작 스크립트

SCRIPT_DIR="/home/jph/bybit_trade_collector"
VENV_PY="$SCRIPT_DIR/venv/bin/python"
TARGET="$SCRIPT_DIR/cow_grid_trader.py"
LOG_FILE="$SCRIPT_DIR/cow_trader.log"

# 기존 프로세스 종료
pkill -f "cow_grid_trader.py" 2>/dev/null || true
sleep 1

# 백그라운드 가동
nohup "$VENV_PY" -u "$TARGET" > "$LOG_FILE" 2>&1 &
echo "[*] COW Grid Trader PID: $!"
