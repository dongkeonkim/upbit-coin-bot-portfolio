import pandas as pd
import numpy as np
import pandas_ta as ta
from sqlalchemy import create_engine
import itertools
import time
import json
import os

# 설정
DB_URI = os.getenv("DATABASE_URL", "postgresql://coinbot@127.0.0.1:5432/coinbot")
PERIODS = {
    "P1": ("2024-01-01 00:00:00", "2024-03-15 00:00:00"),
    "P2": ("2024-03-15 00:00:00", "2024-11-15 00:00:00"),
    "P3": ("2025-01-15 00:00:00", "2025-04-17 00:00:00"),
}
TARGET_COINS = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
TAKE_PROFIT = 0.007
STOP_LOSS = -0.006
LOOKAHEAD = 4320

def get_db_engine():
    return create_engine(DB_URI)

def load_data(market, start, end):
    query = """
        SELECT timestamp, open, high, low, close, volume
        FROM candles
        WHERE market = %s AND interval = '1m'
          AND timestamp >= %s AND timestamp < %s
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, get_db_engine(), params=(market, start, end))
    df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
    df.set_index('timestamp', inplace=True)
    return df

def resample_data(df_1m, timeframe):
    agg_dict = {
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }
    df_resampled = df_1m.resample(timeframe).agg(agg_dict).dropna()
    return df_resampled

def calc_indicators(df):
    # RSI
    for l in [2, 5, 9, 14]:
        df[f'RSI_{l}'] = ta.rsi(df['close'], length=l)
    # MFI
    df['MFI_14'] = ta.mfi(df['high'], df['low'], df['close'], df['volume'], length=14)
    # CCI
    df['CCI_14'] = ta.cci(df['high'], df['low'], df['close'], length=14)
    # Stoch
    stoch = ta.stoch(df['high'], df['low'], df['close'])
    df['STOCH_K'] = stoch['STOCHk_14_3_3']
    # BB
    bb = ta.bbands(df['close'], length=20, std=2)
    cols = bb.columns.tolist()
    lower = [c for c in cols if c.startswith('BBL')][0]
    df['BB_LOWER'] = bb[lower]
    
    return df

def prepare_features(df_1m):
    # 1. 1m Indicators
    df_1m = calc_indicators(df_1m)
    
    # 2. Multi-Timeframe Indicators (5m, 15m, 60m)
    for tf in ['5min', '15min', '60min']:
        df_res = resample_data(df_1m, tf)
        df_res = calc_indicators(df_res)
        # Rename cols
        prefix = tf.replace('min', 'm')
        df_res.columns = [f"{prefix}_{c}" for c in df_res.columns]
        # Merge
        df_1m = df_1m.join(df_res, how='left').ffill()

    # 3. Common Confirmations (1m based)
    df_1m['SMA_20'] = ta.sma(df_1m['close'], length=20)
    df_1m['VOL_MA_20'] = ta.sma(df_1m['volume'], length=20)
    df_1m['VOL_RATIO'] = df_1m['volume'] / df_1m['VOL_MA_20']
    df_1m['DIST_SMA_20'] = (df_1m['close'] - df_1m['SMA_20']) / df_1m['SMA_20']
    df_1m['IS_GREEN'] = df_1m['close'] > df_1m['open']
    
    # Trend (60m derived from resample)
    # Check 60m SMA
    # Note: df_res already merged, need 60m_SMA calculation if not in calc_indicators
    # calc_indicators doesn't have SMA. Add manually for 60m trend.
    # Actually, let's use 60m_close vs 60m_open or just 1m SMA stacking for simplicity?
    # No, use accurate 60m trend.
    
    # Re-calculate 60m SMA properly
    df_60m_trend = resample_data(df_1m, '60min')
    df_60m_trend['SMA_20'] = ta.sma(df_60m_trend['close'], length=20)
    df_60m_trend['SMA_60'] = ta.sma(df_60m_trend['close'], length=60)
    df_60m_trend['TREND_UP'] = df_60m_trend['SMA_20'] > df_60m_trend['SMA_60']
    df_60m_trend = df_60m_trend[['TREND_UP']]
    df_60m_trend.columns = ['60m_UPTREND']
    
    df_1m = df_1m.join(df_60m_trend, how='left').ffill()
    
    return df_1m

def backtest_vectorized(df, signals):
    idxs = np.where(signals)[0]
    if len(idxs) == 0: return 0, 0
    
    wins = 0
    total = 0
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    
    for idx in idxs:
        entry = closes[idx]
        tp = entry * (1 + TAKE_PROFIT)
        sl = entry * (1 + STOP_LOSS)
        
        end = min(idx + LOOKAHEAD, n)
        if idx + 1 >= end: continue
        
        w_high = highs[idx+1:end]
        w_low = lows[idx+1:end]
        
        tp_idx = np.where(w_high >= tp)[0]
        sl_idx = np.where(w_low <= sl)[0]
        
        first_tp = tp_idx[0] if len(tp_idx) > 0 else 99999
        first_sl = sl_idx[0] if len(sl_idx) > 0 else 99999
        
        if first_tp < first_sl and first_tp < 99999:
            wins += 1
            total += 1
        elif first_sl < first_tp and first_sl < 99999:
            total += 1
            
    return wins, total

def run_ultimate_search():
    datasets = {}
    print("📂 Loading Data...")
    for p in PERIODS:
        datasets[p] = {}
        for c in TARGET_COINS:
            df = load_data(c, PERIODS[p][0], PERIODS[p][1])
            df = prepare_features(df)
            datasets[p][c] = df
            
    # --- Combination Explosion ---
    
    timeframes = ['', '5m_', '15m_', '60m_'] # '' is 1m
    
    triggers = []
    for tf in timeframes:
        # RSI
        for l in [2, 5, 9, 14]:
            for v in [10, 20, 30]:
                triggers.append((f'{tf}RSI_{l}', v))
        # MFI
        for v in [10, 20]:
            triggers.append((f'{tf}MFI_14', v))
        # Stoch
        for v in [10, 20]:
            triggers.append((f'{tf}STOCH_K', v))
        # BB Low
        triggers.append((f'{tf}BB_LOWER', 'price_cmp'))
            
    # Total Triggers approx 4 * (12+2+2+1) = 68
    
    # Confirmations
    vol_opts = [0, 2.0]
    dip_opts = [0, -0.005]
    trend_opts = [False, True]
    candle_opts = [False, True]
    
    # 68 * 2 * 2 * 2 * 2 = 1088? 
    # Let's add more RSI values to match previous scale
    # Previous RSI: 2,5,9,14 x 5,10,15,20,25,30 = 24 per TF
    # 4 TFs * 24 = 96 triggers just for RSI
    # Total comb will be huge. 
    
    combinations = list(itertools.product(triggers, vol_opts, dip_opts, trend_opts, candle_opts))
    
    print(f"🔥 Starting ULTIMATE Search on {len(combinations)} strategies...")
    
    candidates = []
    start_time = time.time()
    
    for i, (trig, vol, dip, trend, candle) in enumerate(combinations):
        if i % 100 == 0: print(f"  {i}/{len(combinations)}...", end='\r')
        
        t_col, t_val = trig
        name = f"{t_col}<{t_val} V{vol} D{dip} T{trend} C{candle}"
        
        stats = {}
        total_trades = 0
        
        for p in PERIODS:
            pw, pt = 0, 0
            for c in TARGET_COINS:
                df = datasets[p][c]
                
                # Logic
                if t_val == 'price_cmp':
                    # Special for BB Lower: Close < BB_Lower
                    # Need to handle TF prefix for price column?
                    # Simplify: compare TF's close to TF's BB
                    # BUT we are trading 1m candles.
                    # Using Higher TF BB Lower means: 1m Close < Higher TF BB Lower?
                    # Or Higher TF Close < Higher TF BB Lower?
                    # Let's assume: 1m Close < Target Column
                    cond = df['close'] < df[t_col]
                else:
                    cond = df[t_col] < t_val
                    
                if vol > 0: cond &= (df['VOL_RATIO'] > vol)
                if dip < 0: cond &= (df['DIST_SMA_20'] < dip)
                if trend: cond &= (df['60m_UPTREND'] == True)
                if candle: cond &= (df['IS_GREEN'] == True)
                
                w, t = backtest_vectorized(df, cond.values)
                pw += w
                pt += t
            
            stats[p] = {'wr': pw/pt if pt>0 else 0, 't': pt}
            total_trades += pt
            
        if total_trades < 100: continue
        
        cnt_60 = sum(1 for k in stats if stats[k]['wr'] >= 0.60)
        cnt_40 = sum(1 for k in stats if stats[k]['wr'] >= 0.40)
        
        if cnt_60 >= 2 and cnt_40 == 3:
            candidates.append({'name': name, 'stats': stats, 'total': total_trades})
            
    print(f"\n🏁 Complete. Found {len(candidates)} candidates.")
    
    if candidates:
        output_file = "strategies_ultimate.json"
        with open(output_file, 'w') as f:
            json.dump(candidates, f, indent=2)
        print(f"💾 Saved {len(candidates)} strategies to {output_file}")
        
        # Best
        best = sorted(candidates, key=lambda x: x['total'], reverse=True)[0]
        print(f"🏆 BEST: {best['name']} (Trades: {best['total']})")

if __name__ == "__main__":
    run_ultimate_search()
