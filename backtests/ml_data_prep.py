import pandas as pd
import numpy as np
import psycopg2
from sqlalchemy import create_engine
import os
from datetime import datetime

# Database connection details
db_config = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "coinbot"
}

TARGET_MARKETS = ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL', 'KRW-ADA', 'KRW-DOGE']
TP = 0.015  # +1.5%
SL = -0.010 # -1.0%
TIMEOUT_MINS = 30 # 30 minutes

def get_engine():
    return create_engine(f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}")

def fetch_data(market):
    print(f"📥 Fetching data for {market}...")
    engine = get_engine()
    query = f"""
        SELECT timestamp, open, high, low, close, volume,
               quote_asset_volume, trade_count,
               taker_buy_base_asset_volume, taker_buy_quote_asset_volume
        FROM candles
        WHERE market = '{market}' AND interval = '1m'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, engine)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    # 컬럼명 간소화
    df = df.rename(columns={
        'quote_asset_volume': 'quote_volume',
        'taker_buy_base_asset_volume': 'taker_buy_volume',
        'taker_buy_quote_asset_volume': 'taker_buy_quote_volume'
    })
    return df

def add_features(df):
    print("🛠 Adding features...")
    # Basic Price Features
    df['returns'] = df['close'].pct_change()
    
    # Moving Averages
    for window in [5, 15, 30, 60, 120, 240, 480]:
        df[f'ma{window}'] = df['close'].rolling(window=window).mean()
        df[f'dist_ma{window}'] = df['close'] / df[f'ma{window}'] - 1
        
    # RSI (Relative Strength Index)
    def calculate_rsi(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        # Avoid division by zero
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs.fillna(0)))
    
    df['rsi14'] = calculate_rsi(df['close'], 14)
    df['rsi60'] = calculate_rsi(df['close'], 60)
    
    # MACD
    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp12 - exp26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # MFI (Money Flow Index)
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']
    positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0).rolling(window=14).mean()
    negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0).rolling(window=14).mean()
    mfi_ratio = positive_flow / negative_flow.replace(0, np.nan)
    df['mfi14'] = 100 - (100 / (1 + mfi_ratio.fillna(0)))

    # ATR (Average True Range) for volatility
    df['tr'] = np.maximum(df['high'] - df['low'], 
                          np.maximum(abs(df['high'] - df['close'].shift(1)), 
                                     abs(df['low'] - df['close'].shift(1))))
    df['atr14'] = df['tr'].rolling(window=14).mean()
    df['volatility_atr'] = df['atr14'] / df['close']
    
    # Volatility (Bollinger Bands)
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['ma20'] + (df['std20'] * 2)
    df['bb_lower'] = df['ma20'] - (df['std20'] * 2)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['close']
    
    # Volume Features
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

    # 5. 평균 거래 크기 (거래대금 / 거래횟수)
    df['avg_trade_size'] = df['quote_volume'] / df['trade_count'].replace(0, np.nan)
    avg_trade_ma20 = df['avg_trade_size'].rolling(window=20).mean()
    df['avg_trade_size_ratio'] = df['avg_trade_size'] / avg_trade_ma20.replace(0, np.nan)

    # 6. 거래 횟수 변화율 (20봉 평균 대비)
    tc_ma20 = df['trade_count'].rolling(window=20).mean()
    df['trade_count_ratio'] = df['trade_count'] / tc_ma20.replace(0, np.nan)

    # 7. 테이커 매수 비율 (거래량 기준) - 매수 압력 지표
    df['taker_buy_ratio'] = df['taker_buy_volume'] / df['volume'].replace(0, np.nan)

    # 8. 테이커 매수 거래대금 비율 - 매수 압력 지표 (금액 기준)
    df['taker_buy_qv_ratio'] = df['taker_buy_quote_volume'] / df['quote_volume'].replace(0, np.nan)

    # 임시 컬럼 정리
    df = df.drop(columns=['prev_high', 'prev_low'], errors='ignore')

    return df.dropna()

def add_labels(df):
    print("🏷 Adding labels (Trailing Stop Simulation)...")
    # 트레일링 스탑 설정
    TRIGGER_PCT = 0.018     # 1.8% 도달 시 활성화
    DROP_PCT = 0.003        # 고점 대비 0.3% 하락 시 매도
    SL_PCT = 0.010          # 손절 1.0%
    
    # 현실적 비용 (라벨링 시 반영)
    ENTRY_SLIPPAGE = 0.0015 # 진입 슬리피지 0.15%
    EXIT_SLIPPAGE = 0.0005  # 청산 슬리피지 0.05%
    FEE = 0.001             # 왕복 수수료 0.1%
    
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    labels = np.zeros(n)
    
    # 타임아웃
    TIMEOUT_BARS = 30 # 30분
    
    for i in range(n - TIMEOUT_BARS):
        # 1. 진입 가격 (슬리피지 반영)
        entry_price = closes[i] * (1 + ENTRY_SLIPPAGE)
        sl_price = entry_price * (1 - SL_PCT)
        trigger_price = entry_price * (1 + TRIGGER_PCT)
        
        peak_price = 0.0
        trailing_active = False
        final_pnl = 0.0
        
        # 미래 데이터 시뮬레이션
        for j in range(1, TIMEOUT_BARS + 1):
            curr_high = highs[i + j]
            curr_low = lows[i + j]
            curr_close = closes[i + j]
            
            # SL 체크 (최우선)
            if curr_low <= sl_price:
                # 손절 (시장가 급락 반영 - 보수적)
                exit_price = min(sl_price, curr_low) * (1 - EXIT_SLIPPAGE)
                final_pnl = (exit_price - entry_price) / entry_price - FEE
                break
            
            # 트레일링 로직
            if not trailing_active:
                if curr_high >= trigger_price:
                    trailing_active = True
                    peak_price = curr_high
            else:
                # 고점 갱신
                if curr_high > peak_price:
                    peak_price = curr_high
                
                # 하락 청산 체크
                trailing_stop_price = peak_price * (1 - DROP_PCT)
                if curr_low <= trailing_stop_price:
                    exit_price = trailing_stop_price * (1 - EXIT_SLIPPAGE)
                    final_pnl = (exit_price - entry_price) / entry_price - FEE
                    break
            
            # 타임아웃
            if j == TIMEOUT_BARS:
                exit_price = curr_close * (1 - EXIT_SLIPPAGE)
                final_pnl = (exit_price - entry_price) / entry_price - FEE
        
        # 라벨링: 수수료 떼고도 이익이 났으면 1 (성공), 아니면 0
        if final_pnl > 0:
            labels[i] = 1
        else:
            labels[i] = 0
            
    df['target'] = labels
    return df

def main():
    all_data = []
    for market in TARGET_MARKETS:
        df = fetch_data(market)
        df = add_features(df)
        df = add_labels(df)
        df['market'] = market
        all_data.append(df)
        
    final_df = pd.concat(all_data)
    
    # Save to CSV for easier access in next step
    output_path = "backtests/ml_prepared_data.csv"
    print(f"💾 Saving prepared data to {output_path}...")
    final_df.to_csv(output_path, index=False)
    print("✨ Data preparation complete!")

if __name__ == "__main__":
    main()
