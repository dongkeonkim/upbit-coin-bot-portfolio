"""
백테스트 결과 확인 스크립트
- 기간별(P1-P5), 코인별 수익률, 승률, 매수/매도 횟수 출력
"""

import os
import json
import random
import numpy as np
import pandas as pd
import xgboost as xgb
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from sqlalchemy import create_engine

# ==========================================
# Configuration
# ==========================================
SL_PCT = 1.0
FEE_PCT = 0.1
ENTRY_SLIPPAGE = 0.0015
EXIT_SLIPPAGE = 0.0005
COIN_MAX_POSITIONS = 3
TRAILING_START_PCT = 1.8
TRAILING_STOP_PCT = 0.3

ENTRY_MULT = 1 + ENTRY_SLIPPAGE
EXIT_MULT = 1 - EXIT_SLIPPAGE

# 자본 모델 설정 (trading-engine과 동일)
INITIAL_BALANCE = 1_000_000  # 초기 자본 1,000,000 KRW
INVESTMENT_RATIO = 0.10      # 전체 자산의 10%씩 투자

PERIODS = {
    'P1': ('2024-01-01', '2024-03-15'),
    'P2': ('2024-03-15', '2024-11-15'),
    'P3': ('2024-11-16', '2025-01-14'),
    'P4': ('2025-01-15', '2025-04-17'),
    'P5': ('2025-10-01', '2025-12-22'),
}

COINS = ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-ADA', 'KRW-DOGE']

# coin_params.json에서 파라미터 로드
MODEL_DIR = os.path.join(os.path.dirname(__file__), 'models')
COIN_PARAMS_PATH = os.path.join(MODEL_DIR, 'coin_params.json')
if os.path.exists(COIN_PARAMS_PATH):
    with open(COIN_PARAMS_PATH) as f:
        COIN_PARAMS = json.load(f)
else:
    # 기본값 (fallback)
    COIN_PARAMS = {
        'KRW-BTC': {'hold_minutes': 90, 'threshold': 0.4},
        'KRW-ETH': {'hold_minutes': 90, 'threshold': 0.42},
        'KRW-XRP': {'hold_minutes': 90, 'threshold': 0.4},
        'KRW-SOL': {'hold_minutes': 120, 'threshold': 0.4},
        'KRW-ADA': {'hold_minutes': 60, 'threshold': 0.4},
        'KRW-DOGE': {'hold_minutes': 90, 'threshold': 0.4},
    }

db_config = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "coinbot"
}

# Feature Importance 분석 결과 - 상위 25개 피처 (업비트 사용 가능)
# trade_count_ratio는 업비트에서 제공하지 않아 obv_slope로 대체
FEATURES = [
    'volatility_atr', 'bb_width', 'squeeze', 'is_weekend', 'day_of_week',
    'dist_low_20', 'dist_ma480', 'minutes_since_midnight', 'pullback_depth', 'hour',
    'is_active_hour', 'dist_ma240', 'atr_change', 'dist_ma120', 'macd_signal',
    'dist_high_20', 'rsi60', 'qv_ma20', 'macd', 'dist_ma60',
    'vol_ma20', 'adx14', 'dist_ma30', 'macd_hist', 'obv_slope'
]


@dataclass
class Position:
    """개별 포지션 (기존 방식 - 참고용)"""
    entry_price: float
    quantity: float
    tp_price: float
    sl_price: float
    entry_bar: int
    max_bar: int
    trailing_active: bool = False
    highest_price: float = 0.0


@dataclass
class UnifiedPosition:
    """실전과 동일한 통합 포지션 관리 (자본 모델 포함)"""
    total_cost: float = 0.0      # 총 매수 비용 (KRW)
    total_qty: float = 0.0       # 총 수량
    entry_count: int = 0         # 진입 횟수
    entry_bar: int = 0           # 마지막 진입 봉 (타이머용)
    trailing_active: bool = False
    peak_price: float = 0.0      # 트레일링용 고점
    last_entry_price: float = 0.0  # 마지막 진입가 (트레일링 트리거용)

    @property
    def avg_buy_price(self) -> float:
        """평균 매수가"""
        return self.total_cost / self.total_qty if self.total_qty > 0 else 0.0

    def add_entry(self, price: float, amount: float, bar: int):
        """추가 매수 (ml_strategy.py와 동일: trailing 리셋, 타이머 리셋)

        Args:
            price: 체결가
            amount: 투자금액 (KRW)
            bar: 진입 봉 인덱스
        """
        qty = amount / price
        self.total_cost += amount
        self.total_qty += qty
        self.entry_count += 1
        self.entry_bar = bar  # 타이머 재시작
        self.trailing_active = False  # 트레일링 리셋
        self.last_entry_price = price  # 마지막 진입가 갱신 (ml_strategy.py와 동일)
        # ml_strategy.py와 동일: peak_price = max(peak_price, price)
        self.peak_price = max(self.peak_price, price) if self.peak_price > 0 else price

    def get_sell_value(self, price: float) -> float:
        """청산 시 받는 금액 (KRW)"""
        return self.total_qty * price

    def clear(self):
        """포지션 청산"""
        self.total_cost = 0.0
        self.total_qty = 0.0
        self.entry_count = 0
        self.entry_bar = 0
        self.trailing_active = False
        self.peak_price = 0.0
        self.last_entry_price = 0.0


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

    # ADX (Average Directional Index)
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

    # Pullback Depth
    df['recent_high_20'] = df['high'].rolling(window=20).max()
    df['pullback_depth'] = (df['recent_high_20'] - df['close']) / df['recent_high_20']

    # Bullish Reversal
    prev_bearish = df['close'].shift(1) < df['open'].shift(1)
    curr_bullish = df['close'] > df['open']
    df['bullish_reversal'] = (prev_bearish & curr_bullish).astype(int)

    # Volume Surge
    df['vol_surge'] = (df['vol_ma20'] > 2.0).astype(int)

    # Hour
    df['hour'] = pd.to_datetime(df['timestamp']).dt.hour

    # Is Active Hour
    active_hours = [9, 10, 11, 14, 15, 16, 21, 22, 23]
    df['is_active_hour'] = df['hour'].isin(active_hours).astype(int)

    # ATR Change
    df['atr_ma20'] = df['atr14'].rolling(window=20).mean()
    df['atr_change'] = df['atr14'] / df['atr_ma20'].replace(0, np.nan)

    df = df.drop(columns=['recent_high_20', 'atr_ma20'], errors='ignore')

    # Stochastic Oscillator
    low_14 = df['low'].rolling(window=14).min()
    high_14 = df['high'].rolling(window=14).max()
    df['stoch_k'] = 100 * (df['close'] - low_14) / (high_14 - low_14).replace(0, np.nan)
    df['stoch_d'] = df['stoch_k'].rolling(window=3).mean()

    # Williams %R
    df['williams_r'] = -100 * (high_14 - df['close']) / (high_14 - low_14).replace(0, np.nan)

    # CCI
    typical_price_cci = (df['high'] + df['low'] + df['close']) / 3
    tp_ma = typical_price_cci.rolling(window=14).mean()
    tp_std = typical_price_cci.rolling(window=14).std()
    df['cci14'] = (typical_price_cci - tp_ma) / (0.015 * tp_std).replace(0, np.nan)

    # ROC
    df['roc10'] = (df['close'] - df['close'].shift(10)) / df['close'].shift(10) * 100

    # Candle patterns
    candle_range = df['high'] - df['low']
    candle_body = abs(df['close'] - df['open'])
    df['candle_body_ratio'] = candle_body / candle_range.replace(0, np.nan)
    df['upper_wick_ratio'] = (df['high'] - df[['close', 'open']].max(axis=1)) / candle_range.replace(0, np.nan)
    df['lower_wick_ratio'] = (df[['close', 'open']].min(axis=1) - df['low']) / candle_range.replace(0, np.nan)

    # Consecutive up/down
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

    # Higher High / Higher Low
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    df['higher_high'] = (df['high'] > df['prev_high']).astype(int)
    df['higher_low'] = (df['low'] > df['prev_low']).astype(int)

    # Distance to 20-bar high/low
    high_20 = df['high'].rolling(window=20).max()
    low_20 = df['low'].rolling(window=20).min()
    df['dist_high_20'] = (high_20 - df['close']) / high_20
    df['dist_low_20'] = (df['close'] - low_20) / low_20.replace(0, np.nan)

    # Price Position
    df['price_position'] = (df['close'] - low_20) / (high_20 - low_20).replace(0, np.nan)

    # OBV Slope
    obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    df['obv_slope'] = (obv - obv.shift(10)) / 10

    # Buying Pressure
    df['buying_pressure'] = (df['close'] - df['low']) / (df['high'] - df['low']).replace(0, np.nan)

    # BB Position
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)

    # Squeeze
    bb_width_ma = df['bb_width'].rolling(window=20).mean()
    df['squeeze'] = (df['bb_width'] < bb_width_ma * 0.75).astype(int)

    # Range Ratio
    avg_range = candle_range.rolling(window=20).mean()
    df['range_ratio'] = candle_range / avg_range.replace(0, np.nan)

    # Day of Week
    df['day_of_week'] = pd.to_datetime(df['timestamp']).dt.dayofweek

    # Is Weekend
    df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)

    # Minutes Since Midnight
    df['minutes_since_midnight'] = pd.to_datetime(df['timestamp']).dt.hour * 60 + pd.to_datetime(df['timestamp']).dt.minute

    # MA Slope
    df['ma_slope_20'] = (df['ma20'] - df['ma20'].shift(5)) / df['ma20'].shift(5) * 100

    # Quote Volume Features
    qv_ma10 = df['quote_volume'].rolling(window=10).mean()
    df['qv_change'] = df['quote_volume'] / qv_ma10.replace(0, np.nan)

    qv_ma20 = df['quote_volume'].rolling(window=20).mean()
    df['qv_ma20'] = df['quote_volume'] / qv_ma20.replace(0, np.nan)

    qv_ma60 = df['quote_volume'].rolling(window=60).mean()
    df['qv_ma60'] = df['quote_volume'] / qv_ma60.replace(0, np.nan)

    df['qv_surge'] = (df['qv_ma20'] > 2.0).astype(int)

    # Average Trade Size
    df['avg_trade_size'] = df['quote_volume'] / df['trade_count'].replace(0, np.nan)
    avg_trade_ma20 = df['avg_trade_size'].rolling(window=20).mean()
    df['avg_trade_size_ratio'] = df['avg_trade_size'] / avg_trade_ma20.replace(0, np.nan)

    # Trade Count Ratio
    tc_ma20 = df['trade_count'].rolling(window=20).mean()
    df['trade_count_ratio'] = df['trade_count'] / tc_ma20.replace(0, np.nan)

    # Taker Buy Ratio
    df['taker_buy_ratio'] = df['taker_buy_volume'] / df['volume'].replace(0, np.nan)

    # Taker Buy Quote Volume Ratio
    df['taker_buy_qv_ratio'] = df['taker_buy_quote_volume'] / df['quote_volume'].replace(0, np.nan)

    # Cleanup
    df = df.drop(columns=['prev_high', 'prev_low'], errors='ignore')

    return df.dropna()


def fetch_period_data(coin, start, end):
    """특정 기간 데이터 로드"""
    engine = get_engine()
    query = f"""
        SELECT timestamp, open, high, low, close, volume,
               quote_asset_volume, trade_count,
               taker_buy_base_asset_volume, taker_buy_quote_asset_volume
        FROM candles
        WHERE market = '{coin}'
          AND interval = '1m'
          AND timestamp >= '{start}'
          AND timestamp < '{end}'
        ORDER BY timestamp
    """
    df = pd.read_sql(query, engine)
    if df.empty:
        return None
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    # 컬럼명 간소화
    df = df.rename(columns={
        'quote_asset_volume': 'quote_volume',
        'taker_buy_base_asset_volume': 'taker_buy_volume',
        'taker_buy_quote_asset_volume': 'taker_buy_quote_volume'
    })
    df['quote_volume'] = df['quote_volume'].astype(float)
    df['trade_count'] = df['trade_count'].astype(float)
    df['taker_buy_volume'] = df['taker_buy_volume'].astype(float)
    df['taker_buy_quote_volume'] = df['taker_buy_quote_volume'].astype(float)
    return add_features(df)


def backtest_unified_portfolio(coin_data_list, initial_balance=INITIAL_BALANCE):
    """
    trading-engine과 동일한 통합 포트폴리오 백테스트
    - 모든 코인이 공유 자본 사용
    - 신호 발생 봉 → 다음 봉 시가에 체결
    - 코인별 최대 3회 진입
    - 전체 자산의 10%씩 투자
    """
    random.seed(42)

    # 자본 모델 (trading-engine과 동일)
    quote_balance = initial_balance

    # 코인별 포지션 관리
    positions: Dict[str, UnifiedPosition] = {}
    pending_buys: Dict[str, bool] = {}  # 코인별 대기 중인 매수 신호

    # 결과 추적
    coin_stats = {}
    for coin, _, _, _ in coin_data_list:
        coin_stats[coin] = {'buys': 0, 'sells': 0, 'wins': 0}
        positions[coin] = UnifiedPosition()
        pending_buys[coin] = False

    # 모든 타임스탬프 수집 및 정렬
    all_timestamps = set()
    coin_df_map = {}

    for coin, df, model, params in coin_data_list:
        df = df.dropna(subset=FEATURES)
        if len(df) < 100:
            continue

        X = df[FEATURES]
        dmatrix = xgb.DMatrix(X)
        probs = model.predict(dmatrix)
        df = df.copy()
        df['prob'] = probs
        df['signal'] = df['prob'] >= params['threshold']

        coin_df_map[coin] = {
            'df': df,
            'params': params
        }
        all_timestamps.update(df['timestamp'].tolist())

    if not coin_df_map:
        return None, coin_stats

    sorted_timestamps = sorted(all_timestamps)

    for ts in sorted_timestamps:
        # 1. 현재 가격으로 전체 자산 계산
        total_position_value = 0
        current_prices = {}

        for coin, data in coin_df_map.items():
            df = data['df']
            row = df[df['timestamp'] == ts]
            if not row.empty:
                current_prices[coin] = row['close'].values[0]
                pos = positions[coin]
                if pos.total_qty > 0:
                    total_position_value += pos.total_qty * current_prices[coin]

        total_value = quote_balance + total_position_value
        investment_amount = total_value * INVESTMENT_RATIO
        required_balance = investment_amount * (1 + FEE_PCT / 100)

        # 2. 대기 중인 매수 신호 체결 (현재 봉 시가에 체결)
        for coin, data in coin_df_map.items():
            if not pending_buys[coin]:
                continue

            df = data['df']
            row = df[df['timestamp'] == ts]
            if row.empty:
                continue

            pos = positions[coin]
            if pos.entry_count >= COIN_MAX_POSITIONS:
                pending_buys[coin] = False
                continue

            if quote_balance >= required_balance:
                entry_price = row['open'].values[0] * ENTRY_MULT
                actual_cost = investment_amount * (1 + FEE_PCT / 100)

                # 현재 봉 인덱스 찾기
                bar_idx = df.index.get_loc(row.index[0])
                pos.add_entry(entry_price, investment_amount, bar_idx)
                quote_balance -= actual_cost
                coin_stats[coin]['buys'] += 1

                # 투자금액 재계산 (다음 매수를 위해)
                total_value = quote_balance + total_position_value
                investment_amount = total_value * INVESTMENT_RATIO
                required_balance = investment_amount * (1 + FEE_PCT / 100)

            pending_buys[coin] = False

        # 3. 각 코인별 매도 체크 및 신규 매수 신호
        for coin, data in coin_df_map.items():
            df = data['df']
            params = data['params']
            row = df[df['timestamp'] == ts]

            if row.empty:
                continue

            pos = positions[coin]
            idx = row.index[0]
            bar_idx = df.index.get_loc(idx)

            current_high = row['high'].values[0]
            current_low = row['low'].values[0]
            current_close = row['close'].values[0]
            signal = row['signal'].values[0]

            # 포지션이 있을 때 매도 체크
            if pos.entry_count > 0:
                avg_price = pos.avg_buy_price

                # 손절 체크
                sl_price = avg_price * (1 - SL_PCT / 100)
                if current_low <= sl_price:
                    exit_price = sl_price * EXIT_MULT
                    sell_value = pos.get_sell_value(exit_price)
                    sell_after_fee = sell_value * (1 - FEE_PCT / 100)
                    quote_balance += sell_after_fee
                    coin_stats[coin]['sells'] += 1
                    pos.clear()
                    pending_buys[coin] = False
                    continue

                # 트레일링 활성화 체크
                if not pos.trailing_active:
                    trigger_price = pos.last_entry_price * (1 + TRAILING_START_PCT / 100)
                    if current_high >= trigger_price:
                        pos.trailing_active = True
                        pos.peak_price = current_high

                # 트레일링 스탑 체크
                if pos.trailing_active:
                    if current_high > pos.peak_price:
                        pos.peak_price = current_high

                    drop_price = pos.peak_price * (1 - TRAILING_STOP_PCT / 100)
                    if current_low <= drop_price:
                        exit_price = drop_price * EXIT_MULT
                        pct = (exit_price - avg_price) / avg_price * 100
                        sell_value = pos.get_sell_value(exit_price)
                        sell_after_fee = sell_value * (1 - FEE_PCT / 100)
                        quote_balance += sell_after_fee
                        if pct > 0:
                            coin_stats[coin]['wins'] += 1
                        coin_stats[coin]['sells'] += 1
                        pos.clear()
                        pending_buys[coin] = False
                        continue

                # 시간 종료 체크
                bars_held = bar_idx - pos.entry_bar
                if bars_held >= params['hold_minutes']:
                    exit_price = current_close * EXIT_MULT
                    pct = (exit_price - avg_price) / avg_price * 100
                    sell_value = pos.get_sell_value(exit_price)
                    sell_after_fee = sell_value * (1 - FEE_PCT / 100)
                    quote_balance += sell_after_fee
                    if pct > 0:
                        coin_stats[coin]['wins'] += 1
                    coin_stats[coin]['sells'] += 1
                    pos.clear()
                    pending_buys[coin] = False
                    continue

            # 매수 신호 체크 → 대기열에 추가
            if signal and pos.entry_count < COIN_MAX_POSITIONS and not pending_buys[coin]:
                pending_buys[coin] = True

    # 잔여 포지션 청산
    for coin, data in coin_df_map.items():
        pos = positions[coin]
        if pos.entry_count > 0:
            df = data['df']
            final_price = df['close'].values[-1] * EXIT_MULT
            avg_price = pos.avg_buy_price
            pct = (final_price - avg_price) / avg_price * 100
            sell_value = pos.get_sell_value(final_price)
            sell_after_fee = sell_value * (1 - FEE_PCT / 100)
            quote_balance += sell_after_fee
            if pct > 0:
                coin_stats[coin]['wins'] += 1
            coin_stats[coin]['sells'] += 1

    # 최종 수익률 계산
    final_value = quote_balance
    total_profit = (final_value / initial_balance - 1) * 100

    return total_profit, coin_stats


def main():
    print("=" * 80)
    print("백테스트 결과 (통합 포트폴리오) - trading-engine과 동일")
    print("=" * 80)
    print(f"설정: SL={SL_PCT}%, Trailing={TRAILING_START_PCT}%/{TRAILING_STOP_PCT}%, MaxPos={COIN_MAX_POSITIONS}/코인")
    print(f"슬리피지: Entry={ENTRY_SLIPPAGE*100:.2f}%, Exit={EXIT_SLIPPAGE*100:.2f}%")
    print(f"공유 자본: {INITIAL_BALANCE:,} KRW, 투자비율: {INVESTMENT_RATIO*100:.0f}%/회")
    print("=" * 80)

    # 코인별 모델 및 파라미터 로드
    coin_models = {}
    for coin in COINS:
        coin_symbol = coin.replace('KRW-', '')
        model_path = os.path.join(MODEL_DIR, f'ml_model_{coin_symbol}.json')

        if not os.path.exists(model_path):
            continue

        # coin_params.json에 없는 코인은 스킵 (cross-validation 실패)
        if coin not in COIN_PARAMS:
            print(f"⚠️ {coin}: cross-validation 미통과 (스킵)")
            continue

        model = xgb.Booster()
        model.load_model(model_path)
        params = COIN_PARAMS[coin]
        coin_models[coin] = {'model': model, 'params': params}
        print(f"✅ {coin}: threshold={params['threshold']}, hold={params['hold_minutes']}min")

    print(f"\n총 {len(coin_models)}개 코인 로드됨")

    # 기간별 통합 백테스트
    print("\n" + "=" * 80)
    print("기간별 통합 포트폴리오 결과")
    print("=" * 80)
    print(f"{'기간':<8} {'수익률':>10} {'총매수':>8} {'총매도':>8} {'승리':>6} {'승률':>8}")
    print("-" * 70)

    period_results = []

    for period, (start, end) in PERIODS.items():
        # 모든 코인 데이터 로드
        coin_data_list = []
        for coin, info in coin_models.items():
            df = fetch_period_data(coin, start, end)
            if df is not None and len(df) >= 500:
                coin_data_list.append((coin, df, info['model'], info['params']))

        if not coin_data_list:
            print(f"{period:<8} {'데이터 부족':>10}")
            continue

        # 통합 포트폴리오 백테스트
        profit, coin_stats = backtest_unified_portfolio(coin_data_list)

        if profit is None:
            print(f"{period:<8} {'백테스트 실패':>10}")
            continue

        total_buys = sum(s['buys'] for s in coin_stats.values())
        total_sells = sum(s['sells'] for s in coin_stats.values())
        total_wins = sum(s['wins'] for s in coin_stats.values())
        win_rate = (total_wins / total_sells * 100) if total_sells > 0 else 0

        print(f"{period:<8} {profit:>+9.1f}% {total_buys:>8} {total_sells:>8} {total_wins:>6} {win_rate:>7.1f}%")

        period_results.append({
            'period': period,
            'profit': profit,
            'buys': total_buys,
            'sells': total_sells,
            'wins': total_wins,
            'win_rate': win_rate,
            'coin_stats': coin_stats
        })

    # 전체 요약
    if period_results:
        print("-" * 70)
        total_profit = sum(r['profit'] for r in period_results)
        total_buys = sum(r['buys'] for r in period_results)
        total_sells = sum(r['sells'] for r in period_results)
        total_wins = sum(r['wins'] for r in period_results)
        total_wr = (total_wins / total_sells * 100) if total_sells > 0 else 0
        print(f"{'전체':<8} {total_profit:>+9.1f}% {total_buys:>8} {total_sells:>8} {total_wins:>6} {total_wr:>7.1f}%")

        # 코인별 상세 통계
        print("\n" + "=" * 80)
        print("코인별 상세 통계 (전체 기간)")
        print("=" * 80)
        print(f"{'코인':<12} {'매수':>8} {'매도':>8} {'승리':>6} {'승률':>8}")
        print("-" * 70)

        coin_totals = {}
        for r in period_results:
            for coin, stats in r['coin_stats'].items():
                if coin not in coin_totals:
                    coin_totals[coin] = {'buys': 0, 'sells': 0, 'wins': 0}
                coin_totals[coin]['buys'] += stats['buys']
                coin_totals[coin]['sells'] += stats['sells']
                coin_totals[coin]['wins'] += stats['wins']

        for coin, stats in sorted(coin_totals.items()):
            wr = (stats['wins'] / stats['sells'] * 100) if stats['sells'] > 0 else 0
            print(f"{coin:<12} {stats['buys']:>8} {stats['sells']:>8} {stats['wins']:>6} {wr:>7.1f}%")


if __name__ == "__main__":
    main()
