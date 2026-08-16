"""
Bybit 실시간 체결 데이터 수집기 설정 파일
"""

# 기본 구독 심볼 (초기 시작 시 등록할 심볼)
DEFAULT_SYMBOL = "AKEUSDT"

# Bybit V5 Public WebSocket Endpoint
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# 데이터베이스 파일 저장 경로
DB_PATH = "bybit_trades.duckdb"

# -------------------------------------------------------------
# crypto-volume-ranker 연동 및 동적 심볼 감지 설정
# -------------------------------------------------------------
ENABLE_RANKER_SCANNER = True
RANKER_API_URL = "http://localhost:3000/api/volume-ranking"
RANK_LIMIT = 10              # 상위 순위 범위 (1위 ~ 10위)
MIN_PRICE_CHANGE_PCT = 0.5   # 1분 변동률 기준 (%) (|변동률| >= 0.5)
SCAN_INTERVAL_SEC = 60       # 랭커 갱신 주기 (60초 = 1분)

# -------------------------------------------------------------
# 3대 거래소 (Binance / Bybit / OKX) 청산 데이터 수집 설정
# -------------------------------------------------------------
ENABLE_LIQUIDATION_STREAM = True
LIQUIDATION_EXCHANGES = ["binance", "bybit", "okx"]
LIQUIDATION_MIN_NOTIONAL_USD = 0.0  # 최소 청산 금액 필터 (USD)
LIQUIDATION_BATCH_SIZE = 500        # 청산 DB 배치 크기
LIQUIDATION_FLUSH_MS = 500          # 청산 플러시 주기 (ms)

# -------------------------------------------------------------
# 성능 및 리소스 튜닝 파라미터 (CPU / 메모리 / 스토리지 최적화)
# -------------------------------------------------------------
QUEUE_MAXSIZE = 200_000      # 큐 버퍼 최대 개수 (멀티 심볼 동시 수집 지원)
BATCH_SIZE = 5_000           # DB 단일 배치 저장 레코드 수
FLUSH_INTERVAL = 1.0         # 플러시 주기 (초)
MONITOR_INTERVAL = 10.0      # 모니터링 로그 출력 주기 (초)
WS_PING_INTERVAL = 20        # 웹소켓 핑 주기 (초)
WS_PING_TIMEOUT = 10         # 웹소켓 핑 타임아웃 (초)
