# ML 파이프라인 문서

## 개요

XGBoost 기반 머신러닝 전략의 학습부터 백테스트까지의 전체 파이프라인을 설명합니다.

## 파이프라인 구조

```
┌─────────────────────────────────────────────────────────────────┐
│                        1. 피처 분석 단계                          │
│  ml_data_prep.py → ml_train.py → 피처 중요도 출력 (53개 피처)      │
└─────────────────────────────────────────────────────────────────┘
                                ↓
                    상위 25개 피처 수동 선별
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│                      2. 모델 최적화 단계                          │
│  ml_optimize_per_coin.py → 코인별 최적 파라미터 탐색 (25개 피처)   │
│  - Grid Search: hold_time × threshold 조합                       │
│  - Cross-Validation: P1~P5 기간별 검증                           │
└─────────────────────────────────────────────────────────────────┘
                                ↓
                    models/ml_model_*.json 저장
                    models/coin_params.json 저장
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│                        3. 백테스트 단계                           │
│  run_backtest.py → ml_strategy.py 로드 → 시뮬레이션 (25개 피처)   │
└─────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│                         4. 실제 운영                              │
│  trading_loop.py → ml_strategy.py (25개 피처)                    │
└─────────────────────────────────────────────────────────────────┘
```

## 파일별 역할

### 1. ml_data_prep.py
- **역할**: 원본 캔들 데이터에서 학습용 데이터셋 생성
- **입력**: PostgreSQL candles 테이블
- **출력**: `ml_prepared_data.csv`
- **피처 수**: 53개 (전체)
- **라벨링**: 고정 타임아웃 30분 (피처 분석용이므로 영향 적음)

### 2. ml_train.py
- **역할**: 피처 중요도 분석
- **입력**: `ml_prepared_data.csv`
- **출력**: 피처 중요도 순위 출력, `ml_model.json` (단일 모델)
- **피처 수**: 53개 (전체)
- **용도**: 어떤 피처가 중요한지 파악 → 상위 25개 선별 근거

### 3. ml_optimize_per_coin.py
- **역할**: 코인별 최적 파라미터 탐색 및 모델 학습
- **입력**: PostgreSQL candles 테이블 (직접 조회)
- **출력**:
  - `models/ml_model_{COIN}.json` (코인별 모델)
  - `models/coin_params.json` (최적 파라미터)
- **피처 수**: 25개 (선별된 중요 피처)
- **Grid Search 범위**:
  - `HOLD_TIMES`: [30, 60, 90, 120, 180] 분
  - `THRESHOLDS`: [0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48]

### 4. ml_strategy.py
- **역할**: 실제 매매 신호 생성
- **위치**: `services/trading-engine/src/strategy/ml_strategy.py`
- **피처 수**: 25개 (ml_optimize_per_coin.py와 동일해야 함)
- **모델 로드**: `models/ml_model_{COIN}.json`
- **파라미터 로드**: `models/coin_params.json`

## 피처 선정 과정

### 전체 피처 (53개)
ml_train.py와 ml_data_prep.py에서 사용:
- 기존 피처 (19개): returns, dist_ma*, rsi*, macd*, mfi14, volatility_atr, bb_width, vol_*
- 1차 추가 (7개): adx14, pullback_depth, bullish_reversal, vol_surge, hour, is_active_hour, atr_change
- 2차 추가 - 모멘텀 (5개): stoch_k, stoch_d, williams_r, cci14, roc10
- 2차 추가 - 캔들 패턴 (5개): candle_body_ratio, upper/lower_wick_ratio, consecutive_up/down
- 2차 추가 - 가격 구조 (5개): higher_high/low, dist_high/low_20, price_position
- 2차 추가 - 거래량/변동성 (6개): obv_slope, buying_pressure, bb_position, squeeze, range_ratio, ma_slope_20
- 2차 추가 - 시간 (3개): day_of_week, is_weekend, minutes_since_midnight
- 3차 추가 - 거래대금 (8개): qv_change, qv_ma*, qv_surge, avg_trade_size_ratio, trade_count_ratio, taker_buy_*

### 선별된 피처 (25개)
ml_optimize_per_coin.py와 ml_strategy.py에서 사용:
```python
FEATURES = [
    'volatility_atr', 'bb_width', 'squeeze', 'is_weekend', 'day_of_week',
    'dist_low_20', 'dist_ma480', 'minutes_since_midnight', 'pullback_depth', 'hour',
    'is_active_hour', 'dist_ma240', 'atr_change', 'dist_ma120', 'macd_signal',
    'dist_high_20', 'rsi60', 'qv_ma20', 'macd', 'dist_ma60',
    'vol_ma20', 'adx14', 'dist_ma30', 'macd_hist', 'obv_slope'
]
```

### 선별 기준
- ml_train.py 실행 시 출력되는 `feature_importances_` 기준 상위 25개
- 과적합 방지 및 노이즈 피처 제거 목적

## 라벨링 로직

### Trailing Stop 기반 라벨링
```
진입 → 1.8% 상승 시 Trailing 활성화 → 고점 대비 0.3% 하락 시 익절
     → 1.0% 하락 시 손절
     → hold_time 초과 시 타임아웃 청산
```

### 라벨 결정
- `target = 1`: 최종 수익 > 0 (수수료 포함)
- `target = 0`: 최종 수익 <= 0

### hold_time과 라벨링 정렬
- ml_optimize_per_coin.py의 `add_labels(df, hold_minutes)` 함수는 hold_time에 맞춰 동적으로 라벨링
- 예: hold_time=60분이면 60분 내 결과로 라벨 결정

## 최적화 프로세스

### Grid Search
```python
for hold_time in [30, 60, 90, 120, 180]:
    for threshold in [0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48]:
        # 1. hold_time에 맞춰 라벨링
        # 2. 모델 학습
        # 3. P1~P5 기간별 백테스트
        # 4. 결과 기록
```

### 최적 파라미터 선정 기준
1. **필수 조건**: 승률 >= 50%
2. **우선 조건**: 모든 기간(P1~P5) 흑자
3. **최종 선택**: 조건 만족하는 것 중 총 수익률 최고

### 출력 파일
- `models/ml_model_{COIN}.json`: XGBoost Booster 모델
- `models/coin_params.json`: 코인별 최적 파라미터
  ```json
  {
    "KRW-ETH": {"hold_minutes": 60, "threshold": 0.44, ...},
    "KRW-BTC": {"hold_minutes": 180, "threshold": 0.44, ...}
  }
  ```

## 백테스트 실행

### 최적화 실행 (모델 학습)
```bash
cd backtests
python ml_optimize_per_coin.py
```

### 백테스트 실행
```bash
cd services/trading-engine
python -m src.main backtest --period P1
```

### 기간 정의
| 기간 | 시작 | 종료 | 특성 |
|------|------|------|------|
| P1 | 2024-01-01 | 2024-03-15 | 상승장 |
| P2 | 2024-03-15 | 2024-11-15 | 횡보장 |
| P3 | 2024-11-16 | 2025-01-14 | 상승장 |
| P4 | 2025-01-15 | 2025-04-17 | 하락장 |
| P5 | 2025-10-01 | 2025-12-22 | 최근 |

## 주의사항

### 피처 일관성
- **중요**: ml_optimize_per_coin.py와 ml_strategy.py의 FEATURES 리스트는 반드시 동일해야 함
- 피처 순서도 동일해야 함 (XGBoost는 피처 순서에 의존)

### 모델 재학습 시점
- 새로운 피처 추가 시
- 시장 환경 변화 시 (정기적 재학습 권장)
- 백테스트 성능 저하 시

### Cross-Validation
- ml_optimize_per_coin.py는 P1~P5 전체 기간으로 검증
- 모든 기간 흑자인 파라미터만 선정 (과적합 방지)

## 변경 이력

| 날짜 | 변경 내용 |
|------|----------|
| 2025-12-31 | 피처 53개 → 25개 통일 (ml_optimize_per_coin.py) |
| 2025-12-31 | COINS 5개 → 10개 확장 |
| 2025-12-31 | THRESHOLDS에 0.36 추가 |
