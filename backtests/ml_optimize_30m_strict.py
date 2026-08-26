#!/usr/bin/env python3
"""
Per-Coin ML Parameter Optimizer (30분 Strict 버전)
- 30분 고정 hold time
- 성공 조건:
  1. 트레일링 스탑 (1.8% 후 -0.3%)
  2. 30분 시간 종료 + 수익률 1.5% 이상
- 실패 조건:
  1. 손절 (-1.0%)
  2. 30분 시간 종료 + 수익률 1.5% 미만
"""
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import xgboost as xgb
from dataclasses import dataclass
import psycopg2
from sqlalchemy import create_engine
import random
import json
import os
from itertools import product

# ==========================================
# Grid Search Configuration (30분 Strict)
# ==========================================
HOLD_TIMES = [30]  # 30분 고정
THRESHOLDS = np.arange(0.36, 0.50, 0.02)  # 0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48

# Strategy Settings (고정)
SL_PCT = 1.0
TIME_EXIT_MIN_PROFIT_PCT = 1.5  # 시간 종료 시 성공으로 인정되는 최소 수익률
FEE_PCT = 0.1
ENTRY_SLIPPAGE = 0.0015
MARKET_EXIT_SLIPPAGE = 0.0005
COIN_MAX_POSITIONS = 3
TRAILING_START_PCT = 1.8
TRAILING_STOP_PCT = 0.3

# Test Periods (월별 - 2024년 1월 ~ 2025년 11월)
PERIODS = {
    # 2024년
    '2024-01': ('2024-01-01', '2024-02-01'),
    '2024-02': ('2024-02-01', '2024-03-01'),
    '2024-03': ('2024-03-01', '2024-04-01'),
    '2024-04': ('2024-04-01', '2024-05-01'),
    '2024-05': ('2024-05-01', '2024-06-01'),
    '2024-06': ('2024-06-01', '2024-07-01'),
    '2024-07': ('2024-07-01', '2024-08-01'),
    '2024-08': ('2024-08-01', '2024-09-01'),
    '2024-09': ('2024-09-01', '2024-10-01'),
    '2024-10': ('2024-10-01', '2024-11-01'),
    '2024-11': ('2024-11-01', '2024-12-01'),
    '2024-12': ('2024-12-01', '2025-01-01'),
    # 2025년
    '2025-01': ('2025-01-01', '2025-02-01'),
    '2025-02': ('2025-02-01', '2025-03-01'),
    '2025-03': ('2025-03-01', '2025-04-01'),
    '2025-04': ('2025-04-01', '2025-05-01'),
    '2025-05': ('2025-05-01', '2025-06-01'),
    '2025-06': ('2025-06-01', '2025-07-01'),
    '2025-07': ('2025-07-01', '2025-08-01'),
    '2025-08': ('2025-08-01', '2025-09-01'),
    '2025-09': ('2025-09-01', '2025-10-01'),
    '2025-10': ('2025-10-01', '2025-11-01'),
    '2025-11': ('2025-11-01', '2025-12-01'),
}

# XRP만 테스트 (단월 최대 손실 -5% 조건 추가)
COINS = ['KRW-XRP']

# 전체 코인 (24개) - 백업
# COINS_ALL = [
#     'KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-ADA',
#     'KRW-DOGE', 'KRW-SUI', 'KRW-XLM', 'KRW-QTUM', 'KRW-AERGO',
#     'KRW-HBAR', 'KRW-AWE', 'KRW-LINK', 'KRW-AQT', 'KRW-HIVE',
#     'KRW-SEI', 'KRW-CBK', 'KRW-ARDR', 'KRW-ETC', 'KRW-ARK',
#     'KRW-WAVES', 'KRW-SNT', 'KRW-STRAX', 'KRW-CRO'
# ]

# DB Config
db_config = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "coinbot"
}

ENTRY_MULT = 1 + ENTRY_SLIPPAGE
EXIT_MULT = 1 - MARKET_EXIT_SLIPPAGE

# 중요도 상위 25개 피처 (ml_train_upbit.py에서 분석 후 선별, Upbit 데이터 기반)
FEATURES = [
    'bb_width', 'pullback_depth', 'dist_high_20', 'volatility_atr', 'squeeze',
    'price_position', 'williams_r', 'cci14', 'candle_body_ratio', 'range_ratio',
    'dist_ma5', 'stoch_k', 'dist_low_20', 'macd', 'returns',
    'higher_high', 'lower_wick_ratio', 'macd_hist', 'dist_ma120', 'macd_signal',
    'higher_low', 'vol_surge', 'buying_pressure', 'dist_ma15', 'obv_slope'
]

@dataclass
class Position:
    entry_price: float
    quantity: float
    tp_price: float
    sl_price: float
    entry_bar: int
    max_bar: int
    trailing_active: bool = False
    highest_price: float = 0.0


def get_engine():
    return create_engine(f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}")


def add_features(df):
    """피처 추가"""
    df = df.copy()
    df['returns'] = df['close'].pct_change()

    for window in [5, 15, 30, 60, 120, 240, 480]:
        df[f'ma{window}'] = df['close'].rolling(window=window).mean()
        df[f'dist_ma{window}'] = df['close'] / df[f'ma{window}'] - 1

    def calculate_rsi(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs.fillna(0)))

    df['rsi14'] = calculate_rsi(df['close'], 14)
    df['rsi60'] = calculate_rsi(df['close'], 60)

    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp12 - exp26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']
    positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0).rolling(window=14).mean()
    negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0).rolling(window=14).mean()
    mfi_ratio = positive_flow / negative_flow.replace(0, np.nan)
    df['mfi14'] = 100 - (100 / (1 + mfi_ratio.fillna(0)))

    df['tr'] = np.maximum(df['high'] - df['low'],
                          np.maximum(abs(df['high'] - df['close'].shift(1)),
                                     abs(df['low'] - df['close'].shift(1))))
    df['atr14'] = df['tr'].rolling(window=14).mean()
    df['volatility_atr'] = df['atr14'] / df['close']

    df['ma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['ma20'] + (df['std20'] * 2)
    df['bb_lower'] = df['ma20'] - (df['std20'] * 2)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['close']

    df['vol_change'] = df['volume'] / df['volume'].rolling(window=10).mean().replace(0, np.nan)
    df['vol_ma20'] = df['volume'] / df['volume'].rolling(window=20).mean().replace(0, np.nan)
    df['vol_ma60'] = df['volume'] / df['volume'].rolling(window=60).mean().replace(0, np.nan)

    # ========== 새로운 피처 추가 ==========

    # 1. ADX (Average Directional Index) - 추세 강도
    def calculate_adx(high, low, close, period=14):
        plus_dm = high.diff()
        minus_dm = low.diff().abs() * -1

        plus_dm = plus_dm.where((plus_dm > minus_dm.abs()) & (plus_dm > 0), 0)
        minus_dm = minus_dm.abs().where((minus_dm.abs() > plus_dm) & (minus_dm < 0), 0)

        tr = np.maximum(high - low,
                       np.maximum(abs(high - close.shift(1)),
                                  abs(low - close.shift(1))))
        atr = tr.rolling(window=period).mean()

        plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.rolling(window=period).mean()
        return adx.fillna(0)

    df['adx14'] = calculate_adx(df['high'], df['low'], df['close'], 14)

    # 2. Pullback Depth - 눌림목 깊이 (최근 20봉 고점 대비 하락률)
    df['recent_high_20'] = df['high'].rolling(window=20).max()
    df['pullback_depth'] = (df['recent_high_20'] - df['close']) / df['recent_high_20']

    # 3. Bullish Reversal - 양봉 전환 (이전 음봉 → 현재 양봉)
    prev_bearish = df['close'].shift(1) < df['open'].shift(1)
    curr_bullish = df['close'] > df['open']
    df['bullish_reversal'] = (prev_bearish & curr_bullish).astype(int)

    # 4. Volume Surge - 거래량 급증 (20일 평균 대비 2배 이상)
    df['vol_surge'] = (df['vol_ma20'] > 2.0).astype(int)

    # 5. Hour - 시간대 (0~23)
    df['hour'] = pd.to_datetime(df['timestamp']).dt.hour

    # 6. Is Active Hour - 활발한 시간대 여부
    active_hours = [9, 10, 11, 14, 15, 16, 21, 22, 23]
    df['is_active_hour'] = df['hour'].isin(active_hours).astype(int)

    # 7. ATR Change - 변동성 변화율 (현재 ATR / 20봉 평균 ATR)
    df['atr_ma20'] = df['atr14'].rolling(window=20).mean()
    df['atr_change'] = df['atr14'] / df['atr_ma20'].replace(0, np.nan)

    # 임시 컬럼 정리
    df = df.drop(columns=['recent_high_20', 'atr_ma20'], errors='ignore')

    # ========== 추가 피처 (2차) ==========

    # 1. Stochastic Oscillator
    low_14 = df['low'].rolling(window=14).min()
    high_14 = df['high'].rolling(window=14).max()
    df['stoch_k'] = 100 * (df['close'] - low_14) / (high_14 - low_14).replace(0, np.nan)
    df['stoch_d'] = df['stoch_k'].rolling(window=3).mean()

    # 2. Williams %R
    df['williams_r'] = -100 * (high_14 - df['close']) / (high_14 - low_14).replace(0, np.nan)

    # 3. CCI (Commodity Channel Index)
    typical_price_cci = (df['high'] + df['low'] + df['close']) / 3
    tp_ma = typical_price_cci.rolling(window=14).mean()
    tp_std = typical_price_cci.rolling(window=14).std()
    df['cci14'] = (typical_price_cci - tp_ma) / (0.015 * tp_std).replace(0, np.nan)

    # 4. ROC (Rate of Change)
    df['roc10'] = (df['close'] - df['close'].shift(10)) / df['close'].shift(10) * 100

    # 5. 캔들 패턴 - 몸통/꼬리 비율
    candle_range = df['high'] - df['low']
    candle_body = abs(df['close'] - df['open'])
    df['candle_body_ratio'] = candle_body / candle_range.replace(0, np.nan)
    df['upper_wick_ratio'] = (df['high'] - df[['close', 'open']].max(axis=1)) / candle_range.replace(0, np.nan)
    df['lower_wick_ratio'] = (df[['close', 'open']].min(axis=1) - df['low']) / candle_range.replace(0, np.nan)

    # 6. 연속 양봉/음봉 수
    is_bullish = (df['close'] > df['open']).astype(int)
    is_bearish = (df['close'] < df['open']).astype(int)

    def count_consecutive(series):
        result = []
        count = 0
        prev_val = 0
        for val in series:
            if val == 1 and prev_val == 1:
                count += 1
            elif val == 1:
                count = 1
            else:
                count = 0
            result.append(count)
            prev_val = val
        return result

    df['consecutive_up'] = count_consecutive(is_bullish.values)
    df['consecutive_down'] = count_consecutive(is_bearish.values)

    # 7. Higher High / Higher Low
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    df['higher_high'] = (df['high'] > df['prev_high']).astype(int)
    df['higher_low'] = (df['low'] > df['prev_low']).astype(int)

    # 8. 20봉 최고/최저 대비 거리
    high_20 = df['high'].rolling(window=20).max()
    low_20 = df['low'].rolling(window=20).min()
    df['dist_high_20'] = (high_20 - df['close']) / high_20
    df['dist_low_20'] = (df['close'] - low_20) / low_20.replace(0, np.nan)

    # 9. Price Position (20봉 범위 내 위치 0~1)
    df['price_position'] = (df['close'] - low_20) / (high_20 - low_20).replace(0, np.nan)

    # 10. OBV Slope (기울기)
    obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    df['obv_slope'] = (obv - obv.shift(10)) / 10  # 10봉 기울기

    # 11. Buying Pressure (매수 압력)
    df['buying_pressure'] = (df['close'] - df['low']) / (df['high'] - df['low']).replace(0, np.nan)

    # 12. BB Position (볼린저밴드 내 위치 0~1)
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)

    # 13. Squeeze (볼린저밴드 수축 상태)
    bb_width_ma = df['bb_width'].rolling(window=20).mean()
    df['squeeze'] = (df['bb_width'] < bb_width_ma * 0.75).astype(int)

    # 14. Range Ratio (변동폭 비율)
    avg_range = candle_range.rolling(window=20).mean()
    df['range_ratio'] = candle_range / avg_range.replace(0, np.nan)

    # 15. Day of Week (요일)
    df['day_of_week'] = pd.to_datetime(df['timestamp']).dt.dayofweek

    # 16. Is Weekend
    df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)

    # 17. Minutes Since Midnight
    df['minutes_since_midnight'] = pd.to_datetime(df['timestamp']).dt.hour * 60 + pd.to_datetime(df['timestamp']).dt.minute

    # 18. MA Slope (20MA 기울기)
    df['ma_slope_20'] = (df['ma20'] - df['ma20'].shift(5)) / df['ma20'].shift(5) * 100

    # ========== 거래대금 기반 피처 (3차) ==========

    # 1. 거래대금 변화율 (10봉 평균 대비)
    qv_ma10 = df['quote_volume'].rolling(window=10).mean()
    df['qv_change'] = df['quote_volume'] / qv_ma10.replace(0, np.nan)

    # 2. 거래대금 20봉 평균 대비
    qv_ma20 = df['quote_volume'].rolling(window=20).mean()
    df['qv_ma20'] = df['quote_volume'] / qv_ma20.replace(0, np.nan)

    # 3. 거래대금 60봉 평균 대비
    qv_ma60 = df['quote_volume'].rolling(window=60).mean()
    df['qv_ma60'] = df['quote_volume'] / qv_ma60.replace(0, np.nan)

    # 4. 거래대금 급증 (20봉 평균 대비 2배 이상)
    df['qv_surge'] = (df['qv_ma20'] > 2.0).astype(int)

    # 5-8번 피처는 Binance 전용 컬럼 필요 (Upbit에는 없음)
    # 25개 주요 피처에도 포함되지 않으므로 계산 생략

    # 임시 컬럼 정리
    df = df.drop(columns=['prev_high', 'prev_low'], errors='ignore')

    return df.dropna()


def add_labels(df, hold_minutes):
    """라벨 추가 (30분 Strict 버전)
    성공(1): 트레일링 스탑 OR 시간종료+1.5%이상
    실패(0): 손절 OR 시간종료+1.5%미만
    """
    TRIGGER_PCT = 0.018     # 1.8% 도달 시 활성화
    DROP_PCT = 0.003        # 고점 대비 0.3% 하락 시 매도
    SL_PCT_LABEL = 0.010    # 손절 1.0%
    TIME_EXIT_MIN = 0.015   # 시간 종료 시 성공 인정 최소 수익률 1.5%
    ENTRY_SLIP = 0.0015
    EXIT_SLIP = 0.0005
    FEE = 0.001

    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    labels = np.zeros(n)

    timeout_bars = hold_minutes

    for i in range(n - timeout_bars):
        entry_price = closes[i] * (1 + ENTRY_SLIP)
        sl_price = entry_price * (1 - SL_PCT_LABEL)
        trigger_price = entry_price * (1 + TRIGGER_PCT)

        peak_price = 0.0
        trailing_active = False
        is_success = False  # 성공 여부

        for j in range(1, timeout_bars + 1):
            curr_high = highs[i + j]
            curr_low = lows[i + j]
            curr_close = closes[i + j]

            # 1. 손절 → 실패
            if curr_low <= sl_price:
                is_success = False
                break

            # 2. 트레일링 스탑 체크
            if not trailing_active:
                if curr_high >= trigger_price:
                    trailing_active = True
                    peak_price = curr_high
            else:
                if curr_high > peak_price:
                    peak_price = curr_high

                trailing_stop_price = peak_price * (1 - DROP_PCT)
                if curr_low <= trailing_stop_price:
                    # 트레일링 스탑 → 성공
                    is_success = True
                    break

            # 3. 시간 종료
            if j == timeout_bars:
                exit_price = curr_close * (1 - EXIT_SLIP)
                final_pnl = (exit_price - entry_price) / entry_price - FEE
                # 1.5% 이상이면 성공, 미만이면 실패
                is_success = (final_pnl >= TIME_EXIT_MIN)

        labels[i] = 1 if is_success else 0

    df['target'] = labels
    return df


def train_model(df):
    """XGBoost 모델 학습"""
    X = df[FEATURES]
    y = df['target']

    if y.sum() < 10:
        return None

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        use_label_encoder=False,
        eval_metric='logloss',
        random_state=42,
        verbosity=0
    )
    model.fit(X, y)
    return model


def backtest_with_threshold(df, threshold, hold_minutes):
    """백테스트 실행 (30분 Strict - 그룹 단위 승패)
    성공: 트레일링 스탑 OR 시간종료+1.5%이상
    실패: 손절 OR 시간종료+1.5%미만
    """
    random.seed(42)

    positions = []
    trades = 0
    wins = 0
    total_profit = 0.0

    # 현재 거래 그룹 추적
    current_group_profit = 0.0
    current_group_count = 0
    current_group_has_trailing = False  # 트레일링 스탑 청산 여부

    signals = (df['prob'] >= threshold).values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values

    n_rows = len(df)

    for i in range(n_rows - 1):
        # 평균 매수가 기준 손절 체크 (전체 포지션 청산)
        if positions:
            total_cost = sum(pos.entry_price * pos.quantity for pos in positions)
            total_qty = sum(pos.quantity for pos in positions)
            avg_buy_price = total_cost / total_qty
            sl_price = avg_buy_price * (1 - SL_PCT / 100)

            if lows[i] <= sl_price:
                exit_price = min(sl_price, lows[i]) * EXIT_MULT
                for pos in positions:
                    pct = (exit_price - pos.entry_price) / pos.entry_price * 100
                    current_group_profit += pct - FEE_PCT
                    total_profit += pct - FEE_PCT

                # 손절 → 무조건 실패
                trades += 1
                # wins += 0 (실패)

                # 그룹 리셋
                current_group_profit = 0.0
                current_group_count = 0
                current_group_has_trailing = False
                positions = []
                continue

        active_positions = []

        for pos in positions:
            bars_held = i - pos.entry_bar
            current_high = highs[i]

            if not pos.trailing_active:
                if current_high >= pos.tp_price:
                    pos.trailing_active = True
                    pos.highest_price = current_high

            if pos.trailing_active:
                if current_high > pos.highest_price:
                    pos.highest_price = current_high

                drop_price = pos.highest_price * (1 - TRAILING_STOP_PCT / 100)
                if lows[i] <= drop_price:
                    # 트레일링 스탑 → 성공
                    exit_price = drop_price * EXIT_MULT
                    pct = (exit_price - pos.entry_price) / pos.entry_price * 100
                    current_group_profit += pct - FEE_PCT
                    total_profit += pct - FEE_PCT
                    current_group_count += 1
                    current_group_has_trailing = True
                    continue

            if bars_held >= hold_minutes:
                # 시간 종료
                exit_price = closes[i] * EXIT_MULT
                pct = (exit_price - pos.entry_price) / pos.entry_price * 100
                current_group_profit += pct - FEE_PCT
                total_profit += pct - FEE_PCT
                current_group_count += 1
                continue

            active_positions.append(pos)

        positions = active_positions

        # 모든 포지션이 청산되면 → 1건의 거래로 기록
        if len(positions) == 0 and current_group_count > 0:
            trades += 1
            # 성공 조건: 트레일링 스탑 OR 수익률 1.5% 이상
            if current_group_has_trailing or current_group_profit >= TIME_EXIT_MIN_PROFIT_PCT:
                wins += 1
            # 그룹 리셋
            current_group_profit = 0.0
            current_group_count = 0
            current_group_has_trailing = False

        if signals[i] and len(positions) < COIN_MAX_POSITIONS:
            entry_price = opens[i+1] * ENTRY_MULT
            tp_price = entry_price * (1 + TRAILING_START_PCT / 100)
            sl_price = entry_price * (1 - SL_PCT / 100)

            positions.append(Position(
                entry_price=entry_price,
                quantity=1.0,
                tp_price=tp_price,
                sl_price=sl_price,
                entry_bar=i + 1,
                max_bar=i + 1 + hold_minutes,
                trailing_active=False,
                highest_price=entry_price
            ))

    # 남은 포지션 청산 (시간 종료 취급)
    if positions:
        final_price = closes[-1] * EXIT_MULT
        for pos in positions:
            pct = (final_price - pos.entry_price) / pos.entry_price * 100
            current_group_profit += pct - FEE_PCT
            total_profit += pct - FEE_PCT

        trades += 1
        # 성공 조건: 트레일링 스탑 OR 수익률 1.5% 이상
        if current_group_has_trailing or current_group_profit >= TIME_EXIT_MIN_PROFIT_PCT:
            wins += 1

    win_rate = (wins / trades * 100) if trades > 0 else 0
    return {'trades': trades, 'wins': wins, 'profit': total_profit, 'win_rate': win_rate}


def fetch_all_data(coin):
    """특정 코인의 전체 데이터 로드 (Upbit)"""
    engine = get_engine()
    query = f"""
        SELECT timestamp, open, high, low, close, volume, quote_volume
        FROM candles_upbit
        WHERE market = '{coin}' AND interval = '1m'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, engine)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['quote_volume'] = df['quote_volume'].astype(float)
    return df


def fetch_period_data(coin, start, end):
    """특정 기간 데이터 로드 (Upbit)"""
    engine = get_engine()
    query = f"""
        SELECT timestamp, open, high, low, close, volume, quote_volume
        FROM candles_upbit
        WHERE market = '{coin}'
          AND interval = '1m'
          AND timestamp >= '{start}'
          AND timestamp < '{end}'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, engine)
    if len(df) < 100:
        return None
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['quote_volume'] = df['quote_volume'].astype(float)
    return df


def optimize_coin(coin):
    """단일 코인 최적화 및 모델 저장"""
    print(f"\n{'='*60}")
    print(f"🔍 {coin} 최적화 시작")
    print(f"{'='*60}")

    results = []
    models = {}  # (hold_time) -> model 저장

    for hold_time in HOLD_TIMES:
        print(f"\n  ⏱️  Hold Time: {hold_time}분")

        # 1. 학습 데이터 준비 (전체 기간)
        print(f"    📥 데이터 로드 및 라벨링...")
        df_train = fetch_all_data(coin)
        if len(df_train) < 1000:
            print(f"    ⚠️  데이터 부족 ({len(df_train)} rows)")
            continue

        df_train = add_features(df_train)
        df_train = add_labels(df_train, hold_time)

        # 2. 모델 학습
        print(f"    🎯 모델 학습 중... (성공 샘플: {int(df_train['target'].sum())})")
        model = train_model(df_train)
        if model is None:
            print(f"    ⚠️  학습 실패 (성공 샘플 부족)")
            continue

        # 모델 저장 (hold_time별)
        models[hold_time] = model

        # 3. 각 Threshold로 P1-P5 백테스트
        for thr in THRESHOLDS:
            thr = round(thr, 2)
            total_profit = 0
            total_trades = 0
            total_wins = 0
            period_results = []

            for p_name, (start, end) in PERIODS.items():
                df_period = fetch_period_data(coin, start, end)
                if df_period is None:
                    period_results.append({'period': p_name, 'profit': 0, 'trades': 0})
                    continue

                df_period = add_features(df_period)

                # 예측
                valid_rows = df_period[FEATURES].notna().all(axis=1)
                if not valid_rows.any():
                    period_results.append({'period': p_name, 'profit': 0, 'trades': 0})
                    continue

                df_period['prob'] = 0.0
                probs = model.predict_proba(df_period.loc[valid_rows, FEATURES])[:, 1]
                df_period.loc[valid_rows, 'prob'] = probs

                # 백테스트
                res = backtest_with_threshold(df_period, thr, hold_time)
                total_profit += res['profit']
                total_trades += res['trades']
                total_wins += res['wins']
                period_results.append({
                    'period': p_name,
                    'profit': res['profit'],
                    'trades': res['trades'],
                    'win_rate': res['win_rate']
                })

            win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

            results.append({
                'coin': coin,
                'hold_time': hold_time,
                'threshold': thr,
                'total_profit': total_profit,
                'total_trades': total_trades,
                'win_rate': win_rate,
                'periods': period_results
            })

            print(f"      Thr {thr:.2f}: {total_profit:>7.1f}% | WR: {win_rate:>5.1f}% | Trades: {total_trades:>4}")

    if not results:
        return None, None

    # 필터링: 승률 70% 이상 + 월 2건 이상 + 총 수익률 50% 이상
    MIN_WIN_RATE = 70
    MIN_TRADES_PER_MONTH = 2
    MIN_TOTAL_TRADES = MIN_TRADES_PER_MONTH * len(PERIODS)  # 23개월 × 2 = 46
    MIN_TOTAL_PROFIT = 50  # 총 수익률 50% 이상
    MAX_MONTHLY_LOSS = -5.0  # 단월 최대 손실 -5% 이내

    def is_valid(r):
        # 승률 70% 이상
        if r['win_rate'] < MIN_WIN_RATE:
            return False
        # 거래수 월 2건 이상 (총 46건 이상)
        if r['total_trades'] < MIN_TOTAL_TRADES:
            return False
        # 총 수익률 50% 이상
        if r['total_profit'] < MIN_TOTAL_PROFIT:
            return False
        # 단월 최대 손실 -5% 이내
        min_monthly = min(p['profit'] for p in r['periods'])
        if min_monthly < MAX_MONTHLY_LOSS:
            return False
        return True

    valid_results = [r for r in results if is_valid(r)]

    if not valid_results:
        print(f"    ⚠️  조건 만족하는 설정 없음 (승률>={MIN_WIN_RATE}%, 월{MIN_TRADES_PER_MONTH}건+, 수익률>={MIN_TOTAL_PROFIT}%, 단월손실>={MAX_MONTHLY_LOSS}%)")
        # 최고 수익률 결과 보여주기
        best_profit = max(results, key=lambda x: x['total_profit'])
        # 실패 이유 분석
        reasons = []
        if best_profit['win_rate'] < MIN_WIN_RATE:
            reasons.append(f"승률 {best_profit['win_rate']:.1f}% < {MIN_WIN_RATE}%")
        if best_profit['total_trades'] < MIN_TOTAL_TRADES:
            reasons.append(f"거래수 {best_profit['total_trades']}건 < {MIN_TOTAL_TRADES}건")
        if best_profit['total_profit'] < MIN_TOTAL_PROFIT:
            reasons.append(f"수익률 {best_profit['total_profit']:.1f}% < {MIN_TOTAL_PROFIT}%")
        min_monthly = min(p['profit'] for p in best_profit['periods'])
        if min_monthly < MAX_MONTHLY_LOSS:
            reasons.append(f"단월손실 {min_monthly:.1f}% < {MAX_MONTHLY_LOSS}%")
        print(f"    📊 최고 수익률: {best_profit['total_profit']:.1f}% | WR: {best_profit['win_rate']:.1f}% | Trades: {best_profit['total_trades']} | Thr: {best_profit['threshold']}")
        print(f"    ❌ 실패 이유: {', '.join(reasons)}")
        # 적자 기간 정보
        loss_periods = [(p['period'], p['profit']) for p in best_profit['periods'] if p['profit'] < 0]
        if loss_periods:
            loss_details = [f"{p}({profit:.1f}%)" for p, profit in loss_periods]
            print(f"    📉 적자 {len(loss_periods)}개월: {', '.join(loss_details)}")
        else:
            print(f"    📈 적자 0개월 (모든 기간 흑자)")
        return None, None

    # 조건 만족하는 것 중 최고 수익률 선택
    best = max(valid_results, key=lambda x: x['total_profit'])
    best_model = models.get(best['hold_time'])

    # 통과한 경우도 적자 기간 정보 표시
    loss_periods = [(p['period'], p['profit']) for p in best['periods'] if p['profit'] < 0]
    if loss_periods:
        loss_details = [f"{p}({profit:.1f}%)" for p, profit in loss_periods]
        print(f"    📉 적자 {len(loss_periods)}개월: {', '.join(loss_details)}")
    else:
        print(f"    📈 적자 0개월 (모든 기간 흑자)")

    # 기간별 상세 결과 출력
    print(f"    📋 기간별 상세:")
    for p in best['periods']:
        profit_sign = "+" if p['profit'] >= 0 else ""
        print(f"       {p['period']}: {profit_sign}{p['profit']:.1f}% | {p['trades']}건")

    return best, best_model


def main():
    print("="*70)
    print("🚀 30분 Strict ML Parameter Optimizer")
    print("="*70)
    print(f"Hold Times: {HOLD_TIMES} (30분 고정)")
    print(f"Thresholds: {[round(t, 2) for t in THRESHOLDS]}")
    print(f"Coins: {COINS}")
    print(f"Test Periods: {list(PERIODS.keys())}")
    print(f"성공 조건: 트레일링 스탑 OR 시간종료+1.5%이상")
    print(f"실패 조건: 손절 OR 시간종료+1.5%미만")

    # 모델 저장 디렉토리
    model_dir = "models"  # 스크립트가 backtests/ 에서 실행되므로
    os.makedirs(model_dir, exist_ok=True)

    optimal_params = []
    coin_params = {}  # JSON 저장용

    for coin in COINS:
        best, best_model = optimize_coin(coin)
        if best and best_model:
            optimal_params.append(best)

            # 코인명 추출 (KRW-BTC -> BTC)
            coin_name = coin.split('-')[1]

            # 모델 저장 (Booster로 변환) - 30m_strict 접미사 추가
            model_path = f"{model_dir}/ml_model_{coin_name}_30m_strict.json"
            best_model.get_booster().save_model(model_path)
            print(f"\n  💾 모델 저장: {model_path}")

            print(f"  🏆 {coin} 최적: Hold={best['hold_time']}분, Thr={best['threshold']:.2f}")
            print(f"     수익: {best['total_profit']:.1f}%, 승률: {best['win_rate']:.1f}%, 거래: {best['total_trades']}")

            # 파라미터 저장용
            coin_params[coin] = {
                'hold_minutes': best['hold_time'],
                'threshold': best['threshold'],
                'profit': round(best['total_profit'], 1),
                'win_rate': round(best['win_rate'], 1),
                'trades': best['total_trades']
            }

    # 파라미터 JSON 저장 - 30m_strict 접미사 추가
    params_path = f"{model_dir}/coin_params_30m_strict.json"
    with open(params_path, 'w') as f:
        json.dump(coin_params, f, indent=2)
    print(f"\n💾 파라미터 저장: {params_path}")

    # 최종 결과 출력
    print("\n" + "="*70)
    print("📊 최종 결과 요약")
    print("="*70)

    header = f"{'Coin':<12} | {'Hold(분)':<8} | {'Threshold':<10} | {'Profit':<10} | {'WinRate':<8} | {'Trades':<6}"
    print(header)
    print("-" * len(header))

    for p in optimal_params:
        row = f"{p['coin']:<12} | {p['hold_time']:<8} | {p['threshold']:<10.2f} | {p['total_profit']:>8.1f}% | {p['win_rate']:>6.1f}% | {p['total_trades']:<6}"
        print(row)

    # 기간별 세부 결과
    print("\n" + "="*70)
    print("📈 기간별 세부 결과 (최적 파라미터 적용)")
    print("="*70)

    for p in optimal_params:
        print(f"\n{p['coin']} (Hold: {p['hold_time']}분, Threshold: {p['threshold']:.2f})")
        for period in p['periods']:
            wr = period.get('win_rate', 0)
            print(f"  {period['period']}: {period['profit']:>7.1f}% | WR: {wr:>5.1f}% | Trades: {period['trades']}")

    # 설정 파일 제안
    print("\n" + "="*70)
    print("⚙️  추천 설정 (ml_strategy.py)")
    print("="*70)
    print("COIN_PARAMS = {")
    for p in optimal_params:
        print(f"    '{p['coin']}': {{'hold_minutes': {p['hold_time']}, 'threshold': {p['threshold']:.2f}}},")
    print("}")


if __name__ == "__main__":
    main()
