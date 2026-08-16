# 🌌 Gemini AI Guide for Dual-Exchange Cascade Hunter & Autonomous Tuner

이 문서는 **Gemini AI 에이전트**가 이 레포지토리의 아키텍처와 트레이딩 전략, 데이터 파이프라인을 빠르고 정확하게 분석하고 유지보수할 수 있도록 최적화된 시스템 명세서입니다.

---

## 🏛️ 시스템 핵심 개요 (System Overview)

본 시스템은 **바이낸스(Binance)와 바이비트(Bybit)의 실시간 청산(Liquidation) 웹소켓 피드를 듀얼 감시**하여, **롱 포지션 연쇄 강제청산(Cascade) 폭발 시 0ms 초단타 숏(Short) 스캘핑으로 수익을 창출하는 HFT급 퀀트 트레이딩 플랫폼**입니다.

### 🌟 3대 핵심 데몬 (3 Core Daemons)

1. **`cascade_hunter.py` (v5.0 - 실전 트레이더 봇)**:
   - Binance WS (`!forceOrder@arr`)에서 롱 청산 감지 시 해당 심볼 **`SHORT_ARMED (장전)`**.
   - Bybit WS (`liquidation.*`, `tickers.*`)에서 8초 이내 호가 붕괴(-0.08%~-0.15%) 또는 소액 전이 청산($50+) 확증 시 **0ms 즉시 숏 격발**.
   - 진입 즉시 OCO TP/SL 주문 연동, 지능형 트레일링 스탑, 청산 소진 탈출, 서킷브레이커 탑재.
   - 3초 무중단 핫 리로드로 튜너의 설정을 실시간 스왑.

2. **`symbol_auto_tuner.py` (v6.0 - 1분 주기 자율 진화 튜너)**:
   - 매 1분마다 DuckDB의 최근 틱/청산 데이터를 인메모리로 로드.
   - 5대 세부 파라미터(`bin_arm_usd`, `arm_sec`, `bybit_confirm_usd`, `bybit_confirm_drop`, `trailing_bounce`)를 전수 백테스트.
   - 승률 75%+ & 손익비 2.0+ 검증을 통과한 엘리트 심볼을 `active_symbols.json`에 주입.

3. **`symbol_incubator.py` (v4.0 - 500종 전 종목 레이더 & 섀도우 인큐베이터)**:
   - Bybit 500개 전 종목을 1분마다 스캔하여 거래대금/변동성 폭발 핫 코인 발굴.
   - 실시간 가상 섀도우 트레이딩으로 3회 이상 승률 75%+ 검증 시 정규 실전으로 자동 승격.

4. **`collector.py` (초고속 데이터 수집기)**:
   - Bybit 틱 체결 데이터 + 3대 거래소 (Binance, Bybit, OKX) 실시간 청산 데이터를 DuckDB에 마이크로초 단위 적재.

---

## 📁 주요 디렉토리 및 파일 맵

```
.
├── cascade_hunter.py               # [실전 트레이더] 듀얼 거래소 2단계 전조 확정 숏 스캘퍼
├── symbol_auto_tuner.py            # [자율 튜너] 1분 주기 5대 세팅값 실시간 최적화 엔진
├── symbol_incubator.py             # [인큐베이터] 500종 핫 심볼 레이더 & 섀도우 페이퍼 트레이딩
├── collector.py                    # [수집기] 3대 거래소 틱 & 청산 DuckDB 초고속 수집기
├── active_symbols.json             # [상태 공유] 튜너와 트레이더 간 실시간 핫리로드 설정 파일
│
├── backtest_two_stage_trigger.py   # 2단계 전조 확정 vs 기존 단독 비교 백테스터
├── tune_two_stage_parameters.py    # 5대 세부 설정값 수학적 최적화 백테스터
├── verify_binance_precursor.py     # 바이낸스 도화선 ➔ 바이비트 대폭포 연쇄 실증 스크립트
├── compare_exchange_liquidations.py# 거래소별 청산 선행성(Lead-Lag) 측정기
├── portfolio_total_comparison.py   # 전체 계좌 포트폴리오 총수익률 백테스터
│
├── GEMINI.md                       # Gemini AI 전용 시스템 가이드 (본 문서)
├── ARCHITECTURE.md                 # 시스템 구조 및 알고리즘 상세 설계도
├── requirements.txt                # Python 패키지 의존성
└── .gitignore                      # DuckDB, WAL, 로그, 토큰 제외 설정
```

---

## 🎯 2단계 전조 확정(Two-Stage Trigger) 핵심 알고리즘

```python
# [1단계: 장전] Binance 도화선 점화
if exchange == "binance" and side == "Sell" and notional_usd >= cfg["bin_arm_usd"]:
    binance_armed[symbol] = now + cfg["arm_sec"]

# [2단계: 확증] Bybit 지지선 붕괴 확증 ➔ 0ms 숏 격발
if symbol in binance_armed and now <= binance_armed[symbol]:
    if (bybit_liq_usd >= cfg["bybit_confirm_usd"]) or (bybit_drop_pct >= cfg["bybit_confirm_drop"]):
        del binance_armed[symbol]
        execute_market_short(symbol)
```

---

## 🛡️ 리스크 관리 및 방탄(Bulletproof) 규칙

1. **단일 포지션 락 (`Single Position Lock`)**: 동시 1개 포지션만 진입하여 자본 집중 및 휩쏘 연쇄 노출 방지.
2. **구조적 직전 고점 SL (`Structural Pivot SL`)**: 고정 퍼센트가 아닌 직전 5초 최고가 + 0.10% 버퍼로 손절선 자동 지정.
3. **2연속 손절 1시간 블랙리스트**: 동일 심볼에서 2회 연속 손절 발생 시 1시간 동안 해당 심볼 거래 자동 차단.
4. **일일 손실 서킷브레이커 (`Daily Loss Circuit Breaker`)**: 당일 누적 손실이 -0.60 USDT에 도달 시 모든 거래 자동 중단.
