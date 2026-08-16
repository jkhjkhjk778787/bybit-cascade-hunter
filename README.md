# 🌊 Dual-Exchange Cascade Hunter & Autonomous AI Tuner
### 🚀 High-Frequency Liquidation Cascade Short Scalper (Bybit & Binance)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![DuckDB](https://img.shields.io/badge/DuckDB-In--Memory%20Analytics-yellow.svg)](https://duckdb.org/)
[![Exchange](https://img.shields.io/badge/Bybit-Mainnet%20V5-orange.svg)](https://www.bybit.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Dual-Exchange Cascade Hunter**는 바이낸스(Binance)와 바이비트(Bybit) 선물 거래소의 실시간 청산(Liquidation) 웹소켓 스트림을 교차 감시하여, **롱 포지션 연쇄 강제청산(Cascade) 폭발 시 0ms 초단타 숏(Short) 스캘핑으로 수익을 창출하는 완전 자율 진화형 퀀트 트레이딩 머신**입니다.

---

## 🌟 핵심 특징 (Key Features)

1. **⚡ 2단계 복합 확정 트리거 (Two-Stage Precision Trigger)**:
   - **1단계 [도화선 장전]**: 바이낸스에서 롱 청산($200~$500) 감지 시 해당 심볼 장전(Arming, 유효시간 8초).
   - **2단계 [실시간 확증]**: 바이비트에서 8초 이내 호가창 붕괴(-0.08%~-0.15%) 또는 소액 청산($50+) 포착 즉시 **0ms 시장가 숏 격발**!
   - ➔ **단독 청산 대비 +0.38%~+1.43% 더 높은 고점(천장) 선점 & 기회 4.3배 확장!**

2. **🧠 1분 주기 5대 세부 설정 완전 자율 튜너 (`symbol_auto_tuner.py v6.0`)**:
   - 매 1분마다 137만 개 실시간 틱 데이터 위에서 **5대 파라미터(`bin_arm_usd`, `arm_sec`, `bybit_confirm_usd`, `bybit_confirm_drop`, `trailing_bounce`)를 수학적으로 전수 백테스트**.
   - 승률 75%+ & 손익비 2.0+ 검증을 통과한 엘리트 심볼만 선별하여 `active_symbols.json`에 주입 (3초 무중단 핫 리로드).

3. **📡 500개 전 종목 레이더 & 섀도우 인큐베이터 (`symbol_incubator.py v4.0`)**:
   - 바이비트 500개 전 종목을 실시간 스캔하여 거래대금/변동성 폭발 핫 코인을 자동 발굴.
   - 실시간 가상(Shadow) 매매로 3회 이상 승률 75%+를 증명한 종목만 실전으로 자동 승격.

4. **🛡️ 방탄(Bulletproof) 리스크 관리 엔진**:
   - **단일 포지션 락**: 동시 1개 포지션만 진입하여 자본 집중 및 연쇄 리스크 차단.
   - **구조적 직전 고점 SL**: 직전 5초 최고가 + 0.10% 버퍼로 손절선 자동 지정.
   - **2연속 손절 1시간 블랙리스트**: 연속 2회 손절 피격 시 1시간 동안 해당 심볼 거래 자동 동결.
   - **일일 손실 서킷브레이커**: 당일 누적 손실 -0.60 USDT 도달 시 모든 거래 자동 중단.

---

## 📊 실전 백테스트 성적 (137만 틱 전수 검증)

| 전략 모델 | 거래 기회 포착 | **통합 승률** | **계좌 총 순수익률** | **통합 손익비 (`PF`)** |
| :--- | :---: | :---: | :---: | :---: |
| 🟢 **기존 Bybit 단독 모델** | 10회 | **70.0%** (7승 3패) | **`+7.54%`** | 1.29 |
| 🚀 **2단계 듀얼 거래소 모델** | **43회 (4.3배!)** | **69.8%** (30승 13패) | <font color="#3fb950">**`+40.19%` (+533% 폭증!)**</font> | <font color="#3fb950">**1.38**</font> |

---

## 🏗️ 시스템 아키텍처 (Architecture)

```
┌────────────────────────┐      ┌────────────────────────┐
│ Binance Public WS Feed │      │  Bybit Public WS Feed  │
│ (!forceOrder@arr)      │      │  (liquidation, tickers)│
└───────────┬────────────┘      └───────────┬────────────┘
            │ 1단계: 도화선 감지            │ 2단계: 붕괴 확증
            └───────────────►┌──────────────┴───────────────┐
                             │  cascade_hunter.py (v5.0)    │
                             │  • 0ms 시장가 숏 격발        │
                             │  • 지능형 트레일링 익절      │
                             │  • 구조적 직전 고점 SL       │
                             └──────────────┬───────────────┘
                                            │ 3초 무중단 핫 리로드
┌────────────────────────┐                  │
│ DuckDB (1.3M+ Ticks)   ├──────────────────┤
│ bybit_trades.duckdb    │                  ▼
└───────────┬────────────┘      ┌───────────────────────────┐
            │ 1분 주기 롤링 백테스트 │  active_symbols.json      │
            └──────────────────►│  (심볼별 5대 최적 파라미터)│
                                └───────────────────────────┘
```

---

## 🚀 빠른 시작 (Quick Start)

### 1. 레포지토리 클론 및 의존성 설치
```bash
git clone https://github.com/jkhjkhjk778787/bybit-cascade-hunter.git
cd bybit-cascade-hunter
pip install -r requirements.txt
```

### 2. 환경 변수 설정
`.env.example`을 복사하여 `.env`를 생성하고 Bybit API 키를 입력합니다:
```bash
cp .env.example .env
```

### 3. 3대 데몬 백그라운드 가동
```bash
# 1. 3대 거래소 틱/청산 수집기 실행
python collector.py &

# 2. 1분 주기 5대 세팅 자율 튜너 데몬 실행
python symbol_auto_tuner.py &

# 3. 500종 핫 심볼 레이더 & 섀도우 인큐베이터 실행
python symbol_incubator.py &

# 4. 실전 듀얼 거래소 트레이더 봇 실행
python cascade_hunter.py &
```

---

## 📄 라이선스
본 프로젝트는 [MIT 라이선스](LICENSE)를 따릅니다.
