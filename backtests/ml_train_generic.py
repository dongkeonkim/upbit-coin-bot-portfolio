#!/usr/bin/env python3
"""
범용 ML 모델 학습
- 40개 코인 데이터 통합 학습
- 단일 모델로 모든 코인에 적용 가능
- Grid Search로 최적 파라미터 탐색
"""
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import xgboost as xgb
from dataclasses import dataclass
import psycopg2
from sqlalchemy import create_engine
import json
import os
from itertools import product
from datetime import datetime
from numba import njit

# ==========================================
# 설정
# ==========================================
# Grid Search 범위
HOLD_TIMES = [30, 60, 90, 120, 180, 240, 360]
THRESHOLDS = np.arange(0.40, 0.52, 0.02)  # 0.40 ~ 0.50

# 전략 설정 (고정)
SL_PCT = 1.0
FEE_PCT = 0.1
ENTRY_SLIPPAGE = 0.0015
MARKET_EXIT_SLIPPAGE = 0.0005
TRAILING_START_PCT = 1.8
TRAILING_STOP_PCT = 0.3

ENTRY_MULT = 1 + ENTRY_SLIPPAGE
EXIT_MULT = 1 - MARKET_EXIT_SLIPPAGE

# 검증 기간 (월별)
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

# 30만+ 캔들 코인 (스테이블코인 제외)
COINS = [
    'KRW-BTC', 'KRW-XRP', 'KRW-ETH', 'KRW-LUNC', 'KRW-ACE', 'KRW-WIN',
    'KRW-DASH', 'KRW-APT', 'KRW-UNI', 'KRW-NEAR', 'KRW-SHIB', 'KRW-FTT',
    'KRW-ICP', 'KRW-BCH', 'KRW-ADA', 'KRW-LINK', 'KRW-DOGE', 'KRW-AVAX',
    'KRW-CHZ', 'KRW-AAVE', 'KRW-PEPE', 'KRW-USTC', 'KRW-SOL',
    'KRW-DOT', 'KRW-LUNA', 'KRW-TRX', 'KRW-LTC', 'KRW-SUI',
    'KRW-HBAR', 'KRW-RONIN', 'KRW-WIF', 'KRW-ENA', 'KRW-TAO', 'KRW-HMSTR',
    'KRW-PNUT', 'KRW-THE', 'KRW-PENGU', 'KRW-BANANAS31'
]
# EUR, PAXG 제외 (스테이블/금 연동)

# DB 설정
db_config = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "coinbot"
}

# 25개 피처 (기존과 동일)
FEATURES = [
    'volatility_atr', 'bb_width', 'squeeze', 'is_weekend', 'day_of_week',
    'dist_low_20', 'dist_ma480', 'minutes_since_midnight', 'pullback_depth', 'hour',
    'is_active_hour', 'dist_ma240', 'atr_change', 'dist_ma120', 'macd_signal',
    'dist_high_20', 'rsi60', 'qv_ma20', 'macd', 'dist_ma60',
    'vol_ma20', 'adx14', 'dist_ma30', 'macd_hist', 'obv_slope'
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

    # ADX
    plus_dm = df['high'].diff()
    minus_dm = -df['low'].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    plus_di = 100 * (plus_dm.rolling(14).mean() / df['atr14'].replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / df['atr14'].replace(0, np.nan))
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
    df['adx14'] = dx.rolling(14).mean()

    # Pullback depth
    high_5 = df['high'].rolling(5).max()
    df['pullback_depth'] = (high_5 - df['close']) / high_5

    # Volume surge
    df['vol_surge'] = (df['volume'] > df['volume'].rolling(20).mean() * 2).astype(int)

    # Bullish reversal (하락 후 양봉)
    df['bullish_reversal'] = ((df['close'].shift(1) < df['open'].shift(1)) &
                               (df['close'] > df['open'])).astype(int)

    # 시간 피처
    df['hour'] = df['timestamp'].dt.hour
    df['is_active_hour'] = df['hour'].isin([9, 10, 11, 14, 15, 16, 21, 22, 23]).astype(int)
    df['day_of_week'] = df['timestamp'].dt.dayofweek
    df['is_weekend'] = df['day_of_week'].isin([5, 6]).astype(int)
    df['minutes_since_midnight'] = df['timestamp'].dt.hour * 60 + df['timestamp'].dt.minute

    # ATR change
    df['atr_change'] = df['atr14'].pct_change(5)

    # 가격 구조 피처
    df['high_20'] = df['high'].rolling(20).max()
    df['low_20'] = df['low'].rolling(20).min()
    df['dist_high_20'] = (df['high_20'] - df['close']) / df['high_20']
    df['dist_low_20'] = (df['close'] - df['low_20']) / df['low_20'].replace(0, np.nan)
    df['higher_high'] = (df['high'] > df['high'].shift(1)).astype(int)
    df['higher_low'] = (df['low'] > df['low'].shift(1)).astype(int)
    df['price_position'] = (df['close'] - df['low_20']) / (df['high_20'] - df['low_20']).replace(0, np.nan)

    # OBV slope
    obv = (np.sign(df['close'].diff()) * df['volume']).cumsum()
    df['obv_slope'] = obv.diff(20) / obv.rolling(20).mean().replace(0, np.nan)

    # BB position
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)

    # Squeeze (BB가 좁아짐)
    df['squeeze'] = (df['bb_width'] < df['bb_width'].rolling(50).mean() * 0.8).astype(int)

    # 거래대금 피처
    if 'quote_volume' in df.columns:
        df['qv_change'] = df['quote_volume'].pct_change()
        df['qv_ma20'] = df['quote_volume'] / df['quote_volume'].rolling(20).mean().replace(0, np.nan)
    else:
        df['qv_change'] = 0
        df['qv_ma20'] = 1

    # 메모리 최적화: 피처를 float32로 변환
    for col in FEATURES:
        if col in df.columns:
            df[col] = df[col].astype(np.float32)

    return df


@njit
def create_labels_numba(close_arr, high_arr, low_arr, hold_minutes,
                        entry_mult, exit_mult, sl_pct, trailing_start_pct,
                        trailing_stop_pct, fee_pct):
    """Numba 최적화된 레이블 생성"""
    n = len(close_arr)
    labels = np.zeros(n)

    for i in range(n - hold_minutes):
        entry = close_arr[i] * entry_mult
        sl = entry * (1 - sl_pct / 100)
        trailing_start = entry * (1 + trailing_start_pct / 100)

        trailing_active = False
        highest = entry
        label_set = False

        for j in range(1, hold_minutes + 1):
            high = high_arr[i + j]
            low = low_arr[i + j]

            # 손절
            if low <= sl:
                labels[i] = 0
                label_set = True
                break

            # 고점 갱신
            if high > highest:
                highest = high

            # 트레일링 활성화
            if not trailing_active and highest >= trailing_start:
                trailing_active = True

            # 트레일링 스탑 체크
            if trailing_active:
                trailing_sl = highest * (1 - trailing_stop_pct / 100)
                if low <= trailing_sl:
                    exit_price = trailing_sl * exit_mult
                    pnl = (exit_price - entry) / entry * 100 - fee_pct * 2
                    labels[i] = 1.0 if pnl > 0 else 0.0
                    label_set = True
                    break

        if not label_set:
            # 시간 종료 - 종가 청산
            exit_price = close_arr[i + hold_minutes] * exit_mult
            pnl = (exit_price - entry) / entry * 100 - fee_pct * 2
            labels[i] = 1.0 if pnl > 0 else 0.0

    return labels


def create_labels(df, hold_minutes):
    """타겟 레이블 생성 - 트레일링 스탑 시뮬레이션 (Numba 최적화)"""
    close_arr = df['close'].values.astype(np.float64)
    high_arr = df['high'].values.astype(np.float64)
    low_arr = df['low'].values.astype(np.float64)

    labels = create_labels_numba(
        close_arr, high_arr, low_arr, hold_minutes,
        ENTRY_MULT, EXIT_MULT, SL_PCT, TRAILING_START_PCT,
        TRAILING_STOP_PCT, FEE_PCT
    )

    df = df.copy()
    df['target'] = labels
    return df


def load_coin_data(coin):
    """코인 데이터 로드"""
    engine = get_engine()
    query = f"""
        SELECT timestamp, open, high, low, close, volume, quote_asset_volume as quote_volume
        FROM candles
        WHERE market = '{coin}' AND interval = '1m'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, engine)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['market'] = coin

    # 메모리 최적화: float32로 변환
    for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
        if col in df.columns:
            df[col] = df[col].astype(np.float32)

    return df


def simulate_period(df, model, threshold, hold_minutes):
    """한 기간 시뮬레이션"""
    if len(df) < 500:
        return 0, 0, 0

    initial_balance = 1_000_000
    balance = initial_balance
    positions = []
    wins = 0
    total_trades = 0

    # 피처가 있는지 확인
    missing = [f for f in FEATURES if f not in df.columns]
    if missing:
        return 0, 0, 0

    X = df[FEATURES].values

    # NaN 처리
    X = np.nan_to_num(X, nan=0.0)

    try:
        probs = model.predict_proba(X)[:, 1]
    except:
        return 0, 0, 0

    for i in range(len(df) - hold_minutes):
        row = df.iloc[i]

        # 진입 조건
        if probs[i] >= threshold and len(positions) < 3:
            invest = balance * 0.1
            entry_price = row['close'] * ENTRY_MULT
            quantity = invest / entry_price

            pos = Position(
                entry_price=entry_price,
                quantity=quantity,
                tp_price=entry_price * 1.05,  # 미사용
                sl_price=entry_price * (1 - SL_PCT / 100),
                entry_bar=i,
                max_bar=i + hold_minutes,
                trailing_active=False,
                highest_price=entry_price
            )
            positions.append(pos)
            balance -= invest

        # 포지션 관리
        new_positions = []
        for pos in positions:
            high = row['high']
            low = row['low']

            # 손절
            if low <= pos.sl_price:
                exit_price = pos.sl_price * EXIT_MULT
                pnl = (exit_price - pos.entry_price) / pos.entry_price
                balance += pos.quantity * exit_price
                total_trades += 1
                continue

            # 고점 갱신
            if high > pos.highest_price:
                pos.highest_price = high

            # 트레일링 활성화
            trailing_start = pos.entry_price * (1 + TRAILING_START_PCT / 100)
            if not pos.trailing_active and pos.highest_price >= trailing_start:
                pos.trailing_active = True

            # 트레일링 스탑
            if pos.trailing_active:
                trailing_sl = pos.highest_price * (1 - TRAILING_STOP_PCT / 100)
                if low <= trailing_sl:
                    exit_price = trailing_sl * EXIT_MULT
                    pnl = (exit_price - pos.entry_price) / pos.entry_price
                    balance += pos.quantity * exit_price
                    total_trades += 1
                    if pnl > 0:
                        wins += 1
                    continue

            # 시간 종료
            if i >= pos.max_bar:
                exit_price = row['close'] * EXIT_MULT
                pnl = (exit_price - pos.entry_price) / pos.entry_price
                balance += pos.quantity * exit_price
                total_trades += 1
                if pnl > 0:
                    wins += 1
                continue

            new_positions.append(pos)

        positions = new_positions

    # 잔여 포지션 청산
    if positions:
        last_close = df.iloc[-1]['close'] * EXIT_MULT
        for pos in positions:
            pnl = (last_close - pos.entry_price) / pos.entry_price
            balance += pos.quantity * last_close
            total_trades += 1
            if pnl > 0:
                wins += 1

    final_return = (balance - initial_balance) / initial_balance * 100
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0

    return final_return, win_rate, total_trades


def process_coin_for_training(coin, hold_minutes):
    """코인별로 학습 데이터 생성 후 반환 (메모리 효율화)"""
    try:
        df = load_coin_data(coin)
        if len(df) < 10000:
            return None, None, None

        df = add_features(df)
        df = create_labels(df, hold_minutes)
        df = df.dropna(subset=FEATURES + ['target'])

        # 학습용 피처/레이블만 추출
        X = df[FEATURES].values.astype(np.float32)
        y = df['target'].values.astype(np.float32)

        # 검증용 데이터 (timestamp 포함)
        val_data = df[['timestamp', 'close', 'high', 'low'] + FEATURES + ['target']].copy()

        return X, y, val_data
    except Exception as e:
        print(f"  ❌ {coin}: {e}")
        return None, None, None


def main():
    import gc

    print("=" * 60)
    print("범용 ML 모델 학습 시작 (메모리 최적화 버전)")
    print(f"대상 코인: {len(COINS)}개")
    print("=" * 60)

    best_score = -999
    best_params = None
    best_model = None
    results = []

    print("\n🔍 Grid Search 시작...")
    print(f"   Hold times: {list(HOLD_TIMES)}")
    print(f"   Thresholds: {[f'{t:.2f}' for t in THRESHOLDS]}")

    for hold_idx, hold_minutes in enumerate(HOLD_TIMES):
        print(f"\n⏱️ [{hold_idx+1}/{len(HOLD_TIMES)}] Hold time: {hold_minutes}분")

        # 코인별 순차 처리로 학습 데이터 수집
        X_list = []
        y_list = []
        val_data_list = []

        for i, coin in enumerate(COINS):
            X, y, val_data = process_coin_for_training(coin, hold_minutes)
            if X is not None:
                X_list.append(X)
                y_list.append(y)
                val_data_list.append((coin, val_data))
                print(f"  ✅ [{i+1}/{len(COINS)}] {coin}: {len(X):,} 샘플")
            gc.collect()

        if not X_list:
            print("  ⚠️ 유효한 데이터 없음")
            continue

        # 통합 (numpy로 효율적 처리)
        X_combined = np.vstack(X_list)
        y_combined = np.concatenate(y_list)

        # 메모리 해제
        del X_list, y_list
        gc.collect()

        print(f"   통합 데이터: {len(X_combined):,} 샘플")

        # 모델 학습
        from sklearn.model_selection import train_test_split
        X_t, X_v, y_t, y_v = train_test_split(X_combined, y_combined, test_size=0.1, random_state=42)

        model = xgb.XGBClassifier(
            n_estimators=500,
            max_depth=5,
            learning_rate=0.01,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric='logloss',
            early_stopping_rounds=50
        )
        model.fit(X_t, y_t, eval_set=[(X_v, y_v)], verbose=False)

        # 학습 데이터 해제
        del X_combined, y_combined, X_t, X_v, y_t, y_v
        gc.collect()

        # Threshold별 검증
        for threshold in THRESHOLDS:
            period_returns = []
            period_trades = []
            period_wins = []

            for period_name, (start, end) in PERIODS.items():
                for coin, val_df in val_data_list:
                    df_period = val_df[(val_df['timestamp'] >= start) & (val_df['timestamp'] < end)]
                    if len(df_period) < 500:
                        continue
                    ret, win, trades = simulate_period(df_period, model, threshold, hold_minutes)
                    if trades > 0:
                        period_returns.append(ret)
                        period_trades.append(trades)
                        period_wins.append(win)

            if len(period_returns) > 0:
                avg_return = np.mean(period_returns)
                avg_trades = np.mean(period_trades)
                avg_win = np.mean(period_wins)

                trade_factor = min(avg_trades / 5, 1.0)
                score = avg_return * trade_factor

                results.append({
                    'hold': hold_minutes,
                    'threshold': threshold,
                    'return': avg_return,
                    'trades': avg_trades,
                    'win_rate': avg_win,
                    'score': score
                })

                print(f"   thr={threshold:.2f}: 수익={avg_return:+.1f}%, 거래={avg_trades:.1f}/월, 승률={avg_win:.1f}%")

                if score > best_score:
                    best_score = score
                    best_params = (hold_minutes, threshold)
                    # 최고 모델 저장
                    best_model = model

        # 검증 데이터 해제 (best가 아니면)
        if best_params and best_params[0] != hold_minutes:
            del val_data_list
            gc.collect()

    # 3. 결과 출력
    print("\n" + "=" * 60)
    print("🏆 최적 파라미터 탐색 완료")
    print("=" * 60)

    if best_params:
        print(f"\n✅ 최적 파라미터:")
        print(f"   Hold time: {best_params[0]}분")
        print(f"   Threshold: {best_params[1]:.2f}")
        print(f"   Score: {best_score:.2f}")

        # 모델 저장
        os.makedirs("backtests/models", exist_ok=True)
        model_path = "backtests/models/ml_model_GENERIC.json"
        best_model.get_booster().save_model(model_path)
        print(f"\n💾 모델 저장: {model_path}")

        # 파라미터 저장
        params = {
            "hold_minutes": int(best_params[0]),
            "threshold": float(best_params[1])
        }
        params_path = "backtests/models/generic_params.json"
        with open(params_path, 'w') as f:
            json.dump(params, f, indent=2)
        print(f"💾 파라미터 저장: {params_path}")

        # 상위 결과 출력
        results_df = pd.DataFrame(results)
        results_df = results_df.sort_values('score', ascending=False)
        print("\n📊 상위 10개 조합:")
        print(results_df.head(10).to_string(index=False))
    else:
        print("❌ 유효한 결과 없음")


if __name__ == "__main__":
    main()
