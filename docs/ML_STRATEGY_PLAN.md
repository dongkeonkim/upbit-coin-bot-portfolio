# ML 기반 고승률 전략 발굴 계획서

## 1. 개요 및 현황

### 1.1 배경
- 현재 `upbit-coin-bot` 프로젝트에서 다양한 알고리즘 트레이딩 전략을 탐색 중.
- 기존의 **단순 지표 기반 접근(RSI, 급락 반등 등)**으로는 목표 성과(승률 46% 이상, 고정 TP/SL)를 달성하지 못함.
- 수동적인 파라미터 튜닝 대신, **머신러닝(ML)을 활용하여 데이터 기반의 고승률 진입 구간을 역추적**하기로 결정함.

### 1.2 목표 (Objective)
- **고정 포지션:** TP(익절) **+1.5%** / SL(손절) **-1.0%**
- **목표 성과:**
  - 기대값 E[R] > 0 (수수료 0.1% 왕복 포함 시에도 수익)
  - 승률 **46% 이상** (P1~P4 전 기간에서 안정적)
  - Look-ahead bias(미래 참조 편향) 없는 현실적 전략

---

## 2. 상세 실행 계획 (Workflow)

다음 세션 작업자는 아래 4단계를 순차적으로 수행하여 전략을 완성해야 합니다.

### Step 1: 환경 설정 및 라이브러리 설치
Python 가상환경(`.venv_analysis`)에 ML 관련 라이브러리가 필요합니다.
```bash
# 예상 필요 패키지
pip install scikit-learn xgboost pandas numpy psycopg2-binary
```

### Step 2: 데이터 전처리 및 라벨링 (`backtests/ml_data_prep.py`)
PostgreSQL의 1분봉 데이터를 로드하여 학습 데이터셋(Features + Label)을 생성합니다.

- **Features (X):** 모델이 판단할 근거
  - **기초:** RSI(14), MFI(14), MACD, Bollinger Band 위치(%B)
  - **변동성:** ATR, 캔들 몸통/꼬리 비율
  - **추세:** 이평선 정배열 여부, 이격도(Close/MA60)
  - **거래량:** 전봉 대비 거래량 변화율, 거래대금
- **Label (y):** 정답지 (0 또는 1)
  - 각 시점(t)에서 진입했다고 가정.
  - 미래(t+1 ~ t+N) 구간 탐색:
    - **+1.5%**에 먼저 도달하면 `1` (승리)
    - **-1.0%**에 먼저 도달하면 `0` (패배)
    - 6시간(360분) 내 도달 못 하면 `0` (시간 초과)

### Step 3: 모델 학습 및 규칙 추출 (`backtests/ml_train.py`)
XGBoost 또는 Decision Tree 모델을 사용하여 학습합니다.

- **Train/Test Split:**
  - 학습: P1, P2 기간
  - 검증: P3, P4 기간 (Overfitting 방지)
- **규칙 추출 (Rule Extraction):**
  - 단순히 모델 예측값만 쓰는 게 아니라, 사람이 이해 가능한 조건을 찾습니다.
  - 예: `Tree Visualization`을 통해 "RSI < 30 AND Volume_Ratio > 2.5 일 때 승률 60%" 같은 노드를 발견.
  - 또는 모델의 `predict_proba()`가 0.6 이상인 구간을 분석.

### Step 4: 최종 검증 및 전략화 (`backtests/ml_strategy_verify.py`)
추출된 규칙을 기존 백테스트 엔진 로직에 넣어 시뮬레이션합니다.

- **검증 항목:**
  - 총 거래 횟수 (너무 적으면 안 됨)
  - 기간별 승률 안정성
  - 최종 PnL 및 MDD

---

## 3. 파일 구조 가이드

작업 시 다음 파일들을 생성/활용하세요.

- `backtests/ml_data_prep.py`: 데이터 로딩, 지표 계산, 라벨링 후 `.csv` 또는 `.parquet` 저장.
- `backtests/ml_train.py`: 저장된 데이터로 모델 학습, Feature Importance 분석, 모델 저장.
- `backtests/strategy_ml.py`: 최종 선정된 로직을 구현한 전략 파일 (기존 `strategy_finder.py` 참고).

## 4. 참고 사항

- **데이터베이스:** 로컬 PostgreSQL (`coinbot` DB) 사용.
- **수수료:** 업비트 기준 왕복 0.1% (매수 0.05%, 매도 0.05%).
- **다중 코인:** BTC, ETH, XRP, SOL, ADA, DOGE 데이터 모두 활용하여 일반화된 모델을 만드는 것이 좋음.

---
**작성일:** 2025-12-21
**작성자:** Gemini Assistant
