import pandas as pd
import numpy as np
import pandas_ta as ta
from sqlalchemy import create_engine
import itertools
import time
import os

# 설정 (변경된 기준)
DB_URI = os.getenv("DATABASE_URL", "postgresql://coinbot@127.0.0.1:5432/coinbot")
PERIODS = {
    "P1": ("2024-01-01 00:00:00", "2024-03-15 00:00:00"),
    "P2": ("2024-03-15 00:00:00", "2024-11-15 00:00:00"),
    "P3": ("2025-01-15 00:00:00", "2025-04-17 00:00:00"),
}
TARGET_COINS = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
TAKE_PROFIT = 0.007 # +0.7%
STOP_LOSS = -0.006  # -0.6%
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

def prepare_features(df_1m):
    # 1. 1m Basic
    for l in [2, 5, 9, 14]:
        df_1m[f'RSI_{l}'] = ta.rsi(df_1m['close'], length=l)
        
    df_1m['MFI_14'] = ta.mfi(df_1m['high'], df_1m['low'], df_1m['close'], df_1m['volume'], length=14)
    df_1m['CCI_14'] = ta.cci(df_1m['high'], df_1m['low'], df_1m['close'], length=14)
    df_1m['WILLR_14'] = ta.willr(df_1m['high'], df_1m['low'], df_1m['close'], length=14)
    
    stoch = ta.stoch(df_1m['high'], df_1m['low'], df_1m['close'])
    df_1m['STOCH_K'] = stoch['STOCHk_14_3_3']
    
    # BB
    bb = ta.bbands(df_1m['close'], length=20, std=2)
    cols = bb.columns.tolist()
    upper = [c for c in cols if c.startswith('BBU')][0]
    lower = [c for c in cols if c.startswith('BBL')][0]
    df_1m['BB_LOWER'] = bb[lower]
    df_1m['BB_UPPER'] = bb[upper]
    df_1m['BB_WIDTH'] = (bb[upper] - bb[lower]) / df_1m['close']
    
    # MA & Vol
    df_1m['SMA_20'] = ta.sma(df_1m['close'], length=20)
    df_1m['SMA_60'] = ta.sma(df_1m['close'], length=60)
    df_1m['SMA_120'] = ta.sma(df_1m['close'], length=120)
    
    df_1m['VOL_MA_20'] = ta.sma(df_1m['volume'], length=20)
    df_1m['VOL_RATIO'] = df_1m['volume'] / df_1m['VOL_MA_20']
    
    # Dip
    df_1m['DIST_SMA_20'] = (df_1m['close'] - df_1m['SMA_20']) / df_1m['SMA_20']
    
    # 2. Multi-Timeframe (60m Trend)
    df_60m = resample_data(df_1m, '60min')
    df_60m['60m_SMA_20'] = ta.sma(df_60m['close'], length=20)
    df_60m['60m_SMA_60'] = ta.sma(df_60m['close'], length=60)
    df_60m['60m_UPTREND'] = df_60m['60m_SMA_20'] > df_60m['60m_SMA_60']
    
    # Merge
    df_60m = df_60m[['60m_UPTREND']]
    df_1m = df_1m.join(df_60m, how='left').ffill()
    
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

def run_total_war():
    datasets = {}
    print("📂 Loading Data...")
    for p in PERIODS:
        datasets[p] = {}
        for c in TARGET_COINS:
            df = load_data(c, PERIODS[p][0], PERIODS[p][1])
            df = prepare_features(df)
            datasets[p][c] = df
            
    # --- Strategy Generation ---
    strategies = []
    
    # 1. Simple Indicators (Re-test with new StopLoss)
    for rsi in ['RSI_2', 'RSI_14']:
        for val in [5, 10, 15, 20, 25, 30]:
            strategies.append(lambda df, r=rsi, v=val: df[r] < v)
            
    for mfi in [10, 15, 20, 25]:
        strategies.append(lambda df, v=mfi: df['MFI_14'] < v)
        
    for cci in [-100, -150, -200]:
        strategies.append(lambda df, v=cci: df['CCI_14'] < v)
        
    # 2. Bollinger Bands
    # Lower Breakout
    strategies.append(lambda df: df['close'] < df['BB_LOWER'])
    # Squeeze Breakout (Upper)
    strategies.append(lambda df: (df['BB_WIDTH'] < 0.01) & (df['close'] > df['BB_UPPER']))
    
    # 3. Combinations (Brute Force 2.0)
    # Trigger x Confirmation x Trend
    triggers = [
        ('RSI_2', 5), ('RSI_2', 10),
        ('RSI_14', 20), ('RSI_14', 30),
        ('CCI_14', -100),
        ('STOCH_K', 20)
    ]
    
    confirmations = [
        ('VOL', 0), ('VOL', 1.5), ('VOL', 2.0), ('VOL', 3.0),
        ('DIP', -0.005), ('DIP', -0.01)
    ]
    
    trends = [False, True] # 60m Trend
    
    combinations = list(itertools.product(triggers, confirmations, trends))
    
    for trig, conf, tr in combinations:
        t_col, t_val = trig
        c_type, c_val = conf
        
        def make_strat(t_c, t_v, c_t, c_v, tr_on):
            def strat_func(df):
                cond = df[t_c] < t_v
                if c_t == 'VOL' and c_v > 0:
                    cond &= (df['VOL_RATIO'] > c_v)
                elif c_t == 'DIP':
                    cond &= (df['DIST_SMA_20'] < c_v)
                
                if tr_on:
                    cond &= (df['60m_UPTREND'] == True)
                return cond
            return strat_func
            
        strategies.append(make_strat(t_col, t_val, c_type, c_val, tr))

    print(f"🧪 Testing {len(strategies)} total strategies...")
    
    candidates = []
    
    for i, strat_func in enumerate(strategies):
        if i % 100 == 0: print(f"  {i}/{len(strategies)}...", end='\r')
        
        stats = {}
        total_trades = 0
        
        for p in PERIODS:
            pw, pt = 0, 0
            for c in TARGET_COINS:
                df = datasets[p][c]
                try:
                    signals = strat_func(df)
                except:
                    signals = pd.Series([False]*len(df), index=df.index)
                    
                w, t = backtest_vectorized(df, signals.values)
                pw += w
                pt += t
            
            stats[p] = {'wr': pw/pt if pt>0 else 0, 't': pt}
            total_trades += pt
            
        # Criteria: 2 periods >= 60%, All >= 40%
        cnt_60 = sum(1 for k in stats if stats[k]['wr'] >= 0.60)
        cnt_40 = sum(1 for k in stats if stats[k]['wr'] >= 0.40)
        
        if cnt_60 >= 2 and cnt_40 == 3 and total_trades >= 100: # Min trades
            # Generate Name description
            desc = f"Strat #{i}"
            candidates.append({'desc': desc, 'stats': stats, 'total': total_trades})
            print(f"\n🎉 FOUND: {desc} (Total: {total_trades})")
            for k in stats:
                print(f"  {k}: {stats[k]['wr']:.1%} ({stats[k]['t']})")
                
    if candidates:
        best = sorted(candidates, key=lambda x: x['total'], reverse=True)[0]
        print(f"\n🏆 BEST OF ALL: {best['desc']}")
        print(f"  Total Trades: {best['total']}")
        for k in best['stats']:
            print(f"  {k}: {best['stats'][k]['wr']:.1%} ({best['stats'][k]['t']})")
    else:
        print("\n💀 No strategy found even with exhaustive search.")

if __name__ == "__main__":
    run_total_war()
