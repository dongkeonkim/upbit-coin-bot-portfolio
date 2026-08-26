#!/usr/bin/env python3
"""
상승추세 모델 학습 스크립트 (Trend Following Model)
- 기존 급락반등 모델과 별도로 작동
- 상승 추세 중 진입하여 트레일링 스탑으로 수익 확보
- 30분 고정 홀딩 (30m_strict와 동일)
- 청산 전략도 30m_strict와 동일

성공 조건:
  1. 트레일링 스탑 (1.8% 이상 도달 후 고점 대비 -0.3% 하락 시 매도)
  2. 시간 종료 + 수익률 1.8% 이상

실패 조건:
  1. 손절 (-1.0%)
  2. 시간 종료 + 수익률 1.8% 미만
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
# Grid Search Configuration (Trend Following)
# ==========================================
HOLD_TIMES = [30]  # 30분 고정 (30m_strict와 동일)
THRESHOLDS = np.arange(0.40, 0.56, 0.02)  # 0.40 ~ 0.54

# Strategy Settings (30m_strict와 동일)
SL_PCT = 1.0  # 손절 1.0%
TIME_EXIT_MIN_PROFIT_PCT = 1.8  # 시간 종료 시 성공 인정 최소 수익률
FEE_PCT = 0.1
ENTRY_SLIPPAGE = 0.0015
MARKET_EXIT_SLIPPAGE = 0.0005
COIN_MAX_POSITIONS = 2  # 추세 추종은 2개까지
TRAILING_START_PCT = 1.8  # 1.8% 도달 시 트레일링 활성화
TRAILING_STOP_PCT = 0.3   # 고점 대비 0.3% 하락 시 매도

# Test Periods (월별 - 2024년 1월 ~ 2025년 11월)
PERIODS = {
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

# 15개 코인 (30m_strict 모델과 동일)
COINS = [
    'KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-ADA',
    'KRW-SUI', 'KRW-XLM', 'KRW-QTUM', 'KRW-LINK', 'KRW-AQT',
    'KRW-SEI', 'KRW-ARDR', 'KRW-ETC', 'KRW-ARK', 'KRW-WAVES'
]

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

# 상승추세 모델용 피처 (21개 기존 + 4개 신규 = 25개)
FEATURES = [
    # 기존 피처 (21개) - 급락반등과 공유
    'bb_width', 'pullback_depth', 'dist_high_20', 'volatility_atr', 'squeeze',
    'price_position', 'williams_r', 'cci14', 'candle_body_ratio', 'range_ratio',
    'dist_ma5', 'stoch_k', 'dist_low_20', 'macd', 'returns',
    'higher_high', 'lower_wick_ratio', 'macd_hist', 'macd_signal',
    'higher_low', 'vol_surge',
    # 신규 피처 (4개) - 상승추세 전용
    'ma20_slope',       # MA20 기울기 (상승 추세 강도)
    'ma_alignment',     # MA 정배열 (5>20>60)
    'roc_60',           # 60분 수익률 (모멘텀)
    'adx14'             # ADX (추세 강도)
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
    """상승추세 모델용 피처 추가 (기존 21개 + 신규 4개)"""
    df = df.copy()
    df['returns'] = df['close'].pct_change()

    # MA 계산
    for window in [5, 15, 20, 30, 60, 120, 240, 480]:
        df[f'ma{window}'] = df['close'].rolling(window=window).mean()
        df[f'dist_ma{window}'] = df['close'] / df[f'ma{window}'].replace(0, np.nan) - 1

    # RSI
    def calculate_rsi(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs.fillna(0)))

    df['rsi14'] = calculate_rsi(df['close'], 14)

    # MACD
    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp12 - exp26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # ATR
    df['tr'] = np.maximum(df['high'] - df['low'],
                          np.maximum(abs(df['high'] - df['close'].shift(1)),
                                     abs(df['low'] - df['close'].shift(1))))
    df['atr14'] = df['tr'].rolling(window=14).mean()
    df['volatility_atr'] = df['atr14'] / df['close']

    # Bollinger Bands
    df['std20'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['ma20'] + (df['std20'] * 2)
    df['bb_lower'] = df['ma20'] - (df['std20'] * 2)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['close']

    # Volume
    df['vol_ma20'] = df['volume'] / df['volume'].rolling(window=20).mean().replace(0, np.nan)
    df['vol_surge'] = (df['vol_ma20'] > 2.0).astype(int)

    # ========== 신규 피처 (상승추세용) ==========

    # 1. MA20 기울기 (상승 추세 강도)
    df['ma20_slope'] = (df['ma20'] - df['ma20'].shift(20)) / df['ma20'].shift(20).replace(0, np.nan)

    # 2. MA 정배열 (5 > 20 > 60)
    df['ma_alignment'] = ((df['ma5'] > df['ma20']) & (df['ma20'] > df['ma60'])).astype(int)

    # 3. 60분 수익률 (중기 모멘텀)
    df['roc_60'] = df['close'] / df['close'].shift(60).replace(0, np.nan) - 1

    # 4. ADX (Average Directional Index) - 추세 강도
    def calculate_adx(high, low, close, period=14):
        plus_dm = high.diff()
        minus_dm = low.diff().abs() * -1

        plus_dm = plus_dm.where((plus_dm > minus_dm.abs()) & (plus_dm > 0), 0)
        minus_dm = minus_dm.abs().where((minus_dm.abs() > plus_dm) & (minus_dm < 0), 0)

        tr = np.maximum(high - low,
                       np.maximum(abs(high - close.shift(1)),
                                  abs(low - close.shift(1))))
        atr = tr.rolling(window=period).mean()

        plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr.replace(0, np.nan))

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.rolling(window=period).mean()
        return adx.fillna(0)

    df['adx14'] = calculate_adx(df['high'], df['low'], df['close'], 14)

    # ========== 기존 피처 (공유) ==========

    # Pullback Depth
    df['recent_high_20'] = df['high'].rolling(window=20).max()
    df['pullback_depth'] = (df['recent_high_20'] - df['close']) / df['recent_high_20'].replace(0, np.nan)

    # Stochastic
    low_14 = df['low'].rolling(window=14).min()
    high_14 = df['high'].rolling(window=14).max()
    df['stoch_k'] = 100 * (df['close'] - low_14) / (high_14 - low_14).replace(0, np.nan)

    # Williams %R
    df['williams_r'] = -100 * (high_14 - df['close']) / (high_14 - low_14).replace(0, np.nan)

    # CCI
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    tp_ma = typical_price.rolling(window=14).mean()
    tp_std = typical_price.rolling(window=14).std()
    df['cci14'] = (typical_price - tp_ma) / (0.015 * tp_std).replace(0, np.nan)

    # 캔들 패턴
    candle_range = df['high'] - df['low']
    candle_body = abs(df['close'] - df['open'])
    df['candle_body_ratio'] = candle_body / candle_range.replace(0, np.nan)
    df['lower_wick_ratio'] = (df[['close', 'open']].min(axis=1) - df['low']) / candle_range.replace(0, np.nan)

    # Higher High / Higher Low
    df['higher_high'] = (df['high'] > df['high'].shift(1)).astype(int)
    df['higher_low'] = (df['low'] > df['low'].shift(1)).astype(int)

    # 20봉 고저 대비 거리
    high_20 = df['high'].rolling(window=20).max()
    low_20 = df['low'].rolling(window=20).min()
    df['dist_high_20'] = (high_20 - df['close']) / high_20.replace(0, np.nan)
    df['dist_low_20'] = (df['close'] - low_20) / low_20.replace(0, np.nan)

    # Price Position
    df['price_position'] = (df['close'] - low_20) / (high_20 - low_20).replace(0, np.nan)

    # Range Ratio
    avg_range = candle_range.rolling(window=20).mean()
    df['range_ratio'] = candle_range / avg_range.replace(0, np.nan)

    # Squeeze
    bb_width_ma = df['bb_width'].rolling(window=20).mean()
    df['squeeze'] = (df['bb_width'] < bb_width_ma * 0.75).astype(int)

    # inf/nan 처리
    df = df.replace([np.inf, -np.inf], np.nan)

    # 임시 컬럼 정리
    df = df.drop(columns=['recent_high_20'], errors='ignore')

    return df.dropna()


def add_labels(df, hold_minutes):
    """상승추세 모델용 라벨링 (30m_strict와 동일한 청산 전략)

    성공(1): 트레일링 스탑 (1.8% 도달 후 -0.3%) OR 시간종료+1.8%이상
    실패(0): 손절 OR 시간종료+1.8%미만

    핵심: 상승 추세 중 진입하여 추세가 지속되면 수익 확보
    """
    TRIGGER_PCT = 0.018     # 1.8% 도달 시 트레일링 활성화
    DROP_PCT = 0.003        # 고점 대비 0.3% 하락 시 매도
    SL_PCT_LABEL = 0.010    # 손절 1.0%
    TIME_EXIT_MIN = 0.018   # 시간 종료 시 성공 인정 최소 수익률 1.8%
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
        is_success = False

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
                # 1.5% 이상이면 성공
                is_success = (final_pnl >= TIME_EXIT_MIN)

        labels[i] = 1 if is_success else 0

    df['target'] = labels
    return df


def train_model(df):
    """XGBoost 모델 학습"""
    X = df[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0)
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
    """백테스트 실행 (상승추세 모델)"""
    random.seed(42)

    positions = []
    trades = 0
    wins = 0
    total_profit = 0.0

    current_group_profit = 0.0
    current_group_count = 0
    current_group_has_trailing = False

    signals = (df['prob'] >= threshold).values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values

    n_rows = len(df)

    for i in range(n_rows - 1):
        # 평균 매수가 기준 손절 체크
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

                trades += 1
                # 손절 = 실패

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

        # 모든 포지션 청산 시 → 1건의 거래로 기록
        if len(positions) == 0 and current_group_count > 0:
            trades += 1
            if current_group_has_trailing or current_group_profit >= TIME_EXIT_MIN_PROFIT_PCT:
                wins += 1
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

    # 남은 포지션 청산
    if positions:
        final_price = closes[-1] * EXIT_MULT
        for pos in positions:
            pct = (final_price - pos.entry_price) / pos.entry_price * 100
            current_group_profit += pct - FEE_PCT
            total_profit += pct - FEE_PCT

        trades += 1
        if current_group_has_trailing or current_group_profit >= TIME_EXIT_MIN_PROFIT_PCT:
            wins += 1

    win_rate = (wins / trades * 100) if trades > 0 else 0
    return {'trades': trades, 'wins': wins, 'profit': total_profit, 'win_rate': win_rate}


def fetch_all_data(coin):
    """특정 코인의 전체 데이터 로드"""
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
    """특정 기간 데이터 로드"""
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
    """단일 코인 최적화"""
    print(f"\n{'='*60}")
    print(f"🔍 {coin} 상승추세 모델 최적화")
    print(f"{'='*60}")

    results = []
    models = {}

    for hold_time in HOLD_TIMES:
        print(f"\n  ⏱️  Hold Time: {hold_time}분")

        # 1. 학습 데이터 준비
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

        models[hold_time] = model

        # 3. 각 Threshold로 백테스트
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
                X_pred = df_period.loc[valid_rows, FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0)
                probs = model.predict_proba(X_pred)[:, 1]
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

    # 필터링 조건
    MIN_WIN_RATE = 70  # 승률 70% 이상
    MIN_TRADES_PER_MONTH = 1  # 월 1건 이상
    MIN_TOTAL_TRADES = MIN_TRADES_PER_MONTH * len(PERIODS)
    MIN_TOTAL_PROFIT = 30  # 총 수익률 30% 이상
    MAX_MONTHLY_LOSS = -8.0  # 단월 최대 손실 -8% 이내

    def is_valid(r):
        if r['win_rate'] < MIN_WIN_RATE:
            return False
        if r['total_trades'] < MIN_TOTAL_TRADES:
            return False
        if r['total_profit'] < MIN_TOTAL_PROFIT:
            return False
        min_monthly = min(p['profit'] for p in r['periods'])
        if min_monthly < MAX_MONTHLY_LOSS:
            return False
        return True

    valid_results = [r for r in results if is_valid(r)]

    if not valid_results:
        print(f"    ⚠️  조건 만족하는 설정 없음")
        best_profit = max(results, key=lambda x: x['total_profit'])
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
        print(f"    📊 최고 수익률: {best_profit['total_profit']:.1f}% | WR: {best_profit['win_rate']:.1f}%")
        print(f"    ❌ 실패 이유: {', '.join(reasons)}")
        return None, None

    # 조건 만족하는 것 중 최고 수익률 선택
    best = max(valid_results, key=lambda x: x['total_profit'])
    best_model = models.get(best['hold_time'])

    # 적자 기간 정보
    loss_periods = [(p['period'], p['profit']) for p in best['periods'] if p['profit'] < 0]
    if loss_periods:
        loss_details = [f"{p}({profit:.1f}%)" for p, profit in loss_periods]
        print(f"    📉 적자 {len(loss_periods)}개월: {', '.join(loss_details)}")
    else:
        print(f"    📈 적자 0개월 (모든 기간 흑자)")

    return best, best_model


def main():
    print("="*70)
    print("🚀 상승추세 (Trend Following) ML Parameter Optimizer")
    print("="*70)
    print(f"Hold Times: {HOLD_TIMES}")
    print(f"Thresholds: {[round(t, 2) for t in THRESHOLDS]}")
    print(f"Coins: {len(COINS)}개")
    print(f"성공 조건: 트레일링 스탑 (1.8%→-0.3%) OR 시간종료+1.8%이상")
    print(f"실패 조건: 손절 (-1.0%) OR 시간종료+1.8%미만")
    print()

    # 모델 저장 디렉토리
    model_dir = "../models"  # backtests/ 에서 실행되므로
    os.makedirs(model_dir, exist_ok=True)

    optimal_params = []
    coin_params = {}

    for coin in COINS:
        best, best_model = optimize_coin(coin)
        if best and best_model:
            optimal_params.append(best)

            coin_name = coin.split('-')[1]

            # 모델 저장
            model_path = f"{model_dir}/ml_model_{coin_name}_trend.json"
            best_model.get_booster().save_model(model_path)
            print(f"\n  💾 모델 저장: {model_path}")

            print(f"  🏆 {coin} 최적: Hold={best['hold_time']}분, Thr={best['threshold']:.2f}")
            print(f"     수익: {best['total_profit']:.1f}%, 승률: {best['win_rate']:.1f}%, 거래: {best['total_trades']}")

            coin_params[coin] = {
                'hold_minutes': best['hold_time'],
                'threshold': best['threshold'],
                'profit': round(best['total_profit'], 1),
                'win_rate': round(best['win_rate'], 1),
                'trades': best['total_trades']
            }

    # 파라미터 JSON 저장
    params_path = f"{model_dir}/coin_params_trend.json"
    with open(params_path, 'w') as f:
        json.dump(coin_params, f, indent=2)
    print(f"\n💾 파라미터 저장: {params_path}")

    # 최종 결과 출력
    print("\n" + "="*70)
    print("📊 상승추세 모델 최종 결과")
    print("="*70)

    if optimal_params:
        header = f"{'Coin':<12} | {'Hold(분)':<8} | {'Threshold':<10} | {'Profit':<10} | {'WinRate':<8} | {'Trades':<6}"
        print(header)
        print("-" * len(header))

        for p in optimal_params:
            row = f"{p['coin']:<12} | {p['hold_time']:<8} | {p['threshold']:<10.2f} | {p['total_profit']:>8.1f}% | {p['win_rate']:>6.1f}% | {p['total_trades']:<6}"
            print(row)

        print(f"\n✅ {len(optimal_params)}개 코인 모델 생성 완료")
    else:
        print("⚠️  조건을 만족하는 코인이 없습니다.")

    print("\n" + "="*70)
    print("📋 듀얼 모델 통합 사용법")
    print("="*70)
    print("""
매 캔들마다:
  1. 급락반등 모델 (30m_strict) → prob_bounce
  2. 상승추세 모델 (trend) → prob_trend

  if prob_bounce >= threshold_bounce:
      급락반등 매수 (30분 홀딩)
  elif prob_trend >= threshold_trend:
      상승추세 매수 (트레일링 청산)
""")


if __name__ == "__main__":
    main()
