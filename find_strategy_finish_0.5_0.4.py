import pandas as pd
import numpy as np
import pandas_ta as ta
from sqlalchemy import create_engine
import itertools
import time
import json
import os

# --- 1. 설정 (0.5 / -0.4) ---
DB_URI = os.getenv("DATABASE_URL", "postgresql://coinbot@127.0.0.1:5432/coinbot")
PERIODS = {
    "P1": ("2024-01-01 00:00:00", "2024-03-15 00:00:00"),
    "P2": ("2024-03-15 00:00:00", "2024-11-15 00:00:00"),
    "P3": ("2025-01-15 00:00:00", "2025-04-17 00:00:00"),
}
TARGET_COINS = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

# 목표 기준 (0.5% / -0.4%)
TAKE_PROFIT = 0.005 # +0.5%
STOP_LOSS = -0.004  # -0.4%
LOOKAHEAD = 4320
MIN_TRADES = 200

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
    return df_1m.resample(timeframe).agg(agg_dict).dropna()

def calc_indicators(df):
    for l in [2, 5, 9, 14]:
        df[f'RSI_{l}'] = ta.rsi(df['close'], length=l)
    df['MFI_14'] = ta.mfi(df['high'], df['low'], df['close'], df['volume'], length=14)
    df['CCI_14'] = ta.cci(df['high'], df['low'], df['close'], length=14)
    df['WILLR_14'] = ta.willr(df['high'], df['low'], df['close'], length=14)
    stoch = ta.stoch(df['high'], df['low'], df['close'])
    if stoch is not None: df['STOCH_K'] = stoch['STOCHk_14_3_3']
    bb = ta.bbands(df['close'], length=20, std=2)
    if bb is not None:
        cols = bb.columns.tolist()
        lower = [c for c in cols if c.startswith('BBL')][0]
        upper = [c for c in cols if c.startswith('BBU')][0]
        df['BB_LOWER'] = bb[lower]
        df['BB_UPPER'] = bb[upper]
        df['BB_WIDTH'] = (bb[upper] - bb[lower]) / df['close']
    return df

def prepare_features(df_1m):
    df_1m = calc_indicators(df_1m)
    for tf in ['5min', '15min', '60min']:
        df_res = resample_data(df_1m, tf)
        df_res = calc_indicators(df_res)
        prefix = tf.replace('min', 'm')
        df_res.columns = [f"{prefix}_{c}" for c in df_res.columns]
        df_1m = df_1m.join(df_res, how='left').ffill()

    df_1m['SMA_20'] = ta.sma(df_1m['close'], length=20)
    df_1m['VOL_MA_20'] = ta.sma(df_1m['volume'], length=20)
    df_1m['VOL_RATIO'] = df_1m['volume'] / df_1m['VOL_MA_20']
    df_1m['DIST_SMA_20'] = (df_1m['close'] - df_1m['SMA_20']) / df_1m['SMA_20']
    df_1m['IS_GREEN'] = df_1m['close'] > df_1m['open']
    
    df_60m = resample_data(df_1m[['open','high','low','close','volume']], '60min')
    df_60m['SMA_20'] = ta.sma(df_60m['close'], length=20)
    df_60m['SMA_60'] = ta.sma(df_60m['close'], length=60)
    df_60m['60m_UPTREND'] = df_60m['SMA_20'] > df_60m['SMA_60']
    
    df_1m = df_1m.join(df_60m[['60m_UPTREND']], how='left').ffill()
    return df_1m

def backtest_vectorized(df, signals):
    idxs = np.where(signals)[0]
    if len(idxs) == 0: return 0, 0
    wins, total = 0, 0
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
        
        if first_tp < first_sl and first_tp < 99999: wins += 1; total += 1
        elif first_sl < first_tp and first_sl < 99999: total += 1
            
    return wins, total

def run_finish_search():
    datasets = {}
    print("📂 Loading Data...")
    for p in PERIODS:
        datasets[p] = {}
        for c in TARGET_COINS:
            df = load_data(c, PERIODS[p][0], PERIODS[p][1])
            df = prepare_features(df)
            datasets[p][c] = df
            
    triggers = []
    for tf in ['', '5m_', '15m_', '60m_']:
        for l in [2, 5, 9, 14]:
            for v in [5, 10, 15, 20, 25, 30]: triggers.append((f'{tf}RSI_{l}', v))
        for v in [10, 15, 20, 25]: triggers.append((f'{tf}MFI_14', v))
        for v in [-100, -150]: triggers.append((f'{tf}CCI_14', v))
        for v in [-80, -90]: triggers.append((f'{tf}WILLR_14', v))
        for v in [10, 20]: triggers.append((f'{tf}STOCH_K', v))
        triggers.append((f'{tf}BB_LOWER', 'price_cmp'))
        
    vol_opts = [0, 1.5, 2.0, 3.0]
    dip_opts = [0, -0.005, -0.01]
    trend_opts = [False, True]
    candle_opts = [False, True]
    
    combinations = list(itertools.product(triggers, vol_opts, dip_opts, trend_opts, candle_opts))
    
    print(f"🔥 Starting FINISH Search on {len(combinations)} strategies...")
    print(f"   Target: Profit +0.5% / Loss -0.4%")
    
    candidates = []
    start_time = time.time()
    
    for i, (trig, vol, dip, trend, candle) in enumerate(combinations):
        if i % 100 == 0: print(f"  {i}/{len(combinations)}...", end='\r')
        
        t_col, t_val = trig
        
        stats = {}
        total_trades = 0
        
        for p in PERIODS:
            pw, pt = 0, 0
            for c in TARGET_COINS:
                df = datasets[p][c]
                
                if t_val == 'price_cmp': cond = df['close'] < df[t_col]
                else: cond = df[t_col] < t_val
                
                if vol > 0: cond &= (df['VOL_RATIO'] > vol)
                if dip < 0: cond &= (df['DIST_SMA_20'] < dip)
                if trend: cond &= (df['60m_UPTREND'] == True)
                if candle: cond &= (df['IS_GREEN'] == True)
                
                w, t = backtest_vectorized(df, cond.values)
                pw += w
                pt += t
            
            stats[p] = {'wr': pw/pt if pt>0 else 0, 't': pt}
            total_trades += pt
            
        if total_trades < MIN_TRADES: continue
        
        cnt_60 = sum(1 for k in stats if stats[k]['wr'] >= 0.60)
        cnt_40 = sum(1 for k in stats if stats[k]['wr'] >= 0.40)
        
        if cnt_60 >= 2 and cnt_40 == 3:
            name = f"{t_col}<{t_val} V{vol} D{dip} T{trend} C{candle}"
            strategy_info = {
                "name": name,
                "total_trades": total_trades,
                "periods": {k: {"wr": round(v['wr'], 4), "t": v['t']} for k, v in stats.items()}
            }
            candidates.append(strategy_info)
            
    print(f"\n🏁 Search Complete in {time.time() - start_time:.1f}s")
    
    if candidates:
        output_file = "strategies_finish_6720_0.5_0.4.json"
        with open(output_file, 'w') as f:
            json.dump(candidates, f, indent=2)
        print(f"💾 Saved results to {output_file}")
    else:
        print("\n💀 No strategy passed.")

if __name__ == "__main__":
    run_finish_search()
