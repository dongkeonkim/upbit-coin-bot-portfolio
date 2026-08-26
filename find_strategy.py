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
TAKE_PROFIT = 0.006 # +0.6%
STOP_LOSS = -0.005  # -0.5%
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

def prepare_features(df):
    # RSI
    for l in [2, 5, 14]:
        df[f'RSI_{l}'] = ta.rsi(df['close'], length=l)
        
    # Moving Averages
    df['SMA_20'] = ta.sma(df['close'], length=20)
    df['SMA_60'] = ta.sma(df['close'], length=60)
    df['SMA_120'] = ta.sma(df['close'], length=120)
    
    # Volume
    df['VOL_MA_20'] = ta.sma(df['volume'], length=20)
    df['VOL_RATIO'] = df['volume'] / df['VOL_MA_20']
    
    # Trend
    df['TREND_UP'] = (df['SMA_20'] > df['SMA_60']) & (df['SMA_60'] > df['SMA_120'])
    
    # Dip
    df['DIST_SMA_20'] = (df['close'] - df['SMA_20']) / df['SMA_20']
    
    # Candle
    df['IS_GREEN'] = df['close'] > df['open']
    
    return df

def backtest(df, signals):
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

def run_all():
    datasets = {}
    print("📂 Loading Data...")
    for p in PERIODS:
        datasets[p] = {}
        for c in TARGET_COINS:
            df = load_data(c, PERIODS[p][0], PERIODS[p][1])
            df = prepare_features(df)
            datasets[p][c] = df
            
    # Search Space
    # RSI triggers
    rsi_conds = [
        ('RSI_2', 5), ('RSI_2', 10), 
        ('RSI_5', 15), ('RSI_5', 20),
        ('RSI_14', 25), ('RSI_14', 30)
    ]
    
    # Vol triggers
    vol_conds = [0, 1.5, 2.0, 3.0]
    
    # Trend filters
    trend_conds = [False, True]
    
    # Dip filters
    dip_conds = [0, -0.005] # 0 or -0.5% dip
    
    combs = list(itertools.product(rsi_conds, vol_conds, trend_conds, dip_conds))
    print(f"🧪 Testing {len(combs)} combinations...")
    
    candidates = []
    
    for rsi_c, vol_c, trend_c, dip_c in combs:
        rsi_col, rsi_val = rsi_c
        
        stats = {}
        total_trades = 0
        
        for p in PERIODS:
            pw, pt = 0, 0
            for c in TARGET_COINS:
                df = datasets[p][c]
                cond = df[rsi_col] < rsi_val
                if vol_c > 0: cond &= (df['VOL_RATIO'] > vol_c)
                if trend_c: cond &= df['TREND_UP']
                if dip_c < 0: cond &= (df['DIST_SMA_20'] < dip_c)
                
                w, t = backtest(df, cond.values)
                pw += w
                pt += t
            
            stats[p] = {'wr': pw/pt if pt>0 else 0, 't': pt}
            total_trades += pt
            
        # Eval: At least 2 periods >= 55% (Lowered slightly for discovery) 
        # and All >= 40%
        cnt_high = sum(1 for k in stats if stats[k]['wr'] >= 0.55)
        cnt_low = sum(1 for k in stats if stats[k]['wr'] >= 0.40)
        
        if cnt_high >= 2 and cnt_low == 3 and total_trades > 50:
            name = f"{rsi_col}<{rsi_val}, Vol>{vol_c}, Trend={trend_c}, Dip<{dip_c}"
            candidates.append({'name': name, 'stats': stats, 'total': total_trades})
            print(f"✨ Found: {name}")
            for k in stats:
                print(f"  {k}: {stats[k]['wr']:.1%} ({stats[k]['t']})")
                
    if candidates:
        best = sorted(candidates, key=lambda x: x['total'], reverse=True)[0]
        print(f"\n🏆 Best Strategy: {best['name']} (Trades: {best['total']})")
    else:
        print("\n💀 No luck.")

if __name__ == "__main__":
    run_all()
