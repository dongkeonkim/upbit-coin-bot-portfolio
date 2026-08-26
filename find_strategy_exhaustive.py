import pandas as pd
import numpy as np
import pandas_ta as ta
from sqlalchemy import create_engine
import itertools
import time
import json
import os

# ... (기존 설정 부분 동일) ...
# 설정 (최종 기준)
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

def prepare_features(df_1m):
    # 1. Triggers
    for l in [2, 5, 9, 14]:
        df_1m[f'RSI_{l}'] = ta.rsi(df_1m['close'], length=l)
        
    df_1m['MFI_14'] = ta.mfi(df_1m['high'], df_1m['low'], df_1m['close'], df_1m['volume'], length=14)
    df_1m['CCI_14'] = ta.cci(df_1m['high'], df_1m['low'], df_1m['close'], length=14)
    df_1m['WILLR_14'] = ta.willr(df_1m['high'], df_1m['low'], df_1m['close'], length=14)
    
    stoch = ta.stoch(df_1m['high'], df_1m['low'], df_1m['close'])
    df_1m['STOCH_K'] = stoch['STOCHk_14_3_3']
    
    bb = ta.bbands(df_1m['close'], length=20, std=2)
    cols = bb.columns.tolist()
    upper = [c for c in cols if c.startswith('BBU')][0]
    lower = [c for c in cols if c.startswith('BBL')][0]
    df_1m['BB_LOWER'] = bb[lower]
    df_1m['BB_UPPER'] = bb[upper]
    df_1m['BB_WIDTH'] = (bb[upper] - bb[lower]) / df_1m['close']
    
    # 2. Confirmations
    df_1m['SMA_20'] = ta.sma(df_1m['close'], length=20)
    df_1m['VOL_MA_20'] = ta.sma(df_1m['volume'], length=20)
    df_1m['VOL_RATIO'] = df_1m['volume'] / df_1m['VOL_MA_20']
    df_1m['DIST_SMA_20'] = (df_1m['close'] - df_1m['SMA_20']) / df_1m['SMA_20']
    df_1m['IS_GREEN'] = df_1m['close'] > df_1m['open']
    
    # 3. Trends (60m)
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

def run_exhaustive_search():
    datasets = {}
    print("📂 Loading Data...")
    for p in PERIODS:
        datasets[p] = {}
        for c in TARGET_COINS:
            df = load_data(c, PERIODS[p][0], PERIODS[p][1])
            df = prepare_features(df)
            datasets[p][c] = df
            
    # --- Generate ALL Combinations ---
    triggers = []
    # RSI
    for l in [2, 5, 9, 14]:
        for v in [5, 10, 15, 20, 25, 30]:
            triggers.append(('RSI', f'RSI_{l}', v))
    # MFI
    for v in [10, 15, 20, 25]:
        triggers.append(('MFI', 'MFI_14', v))
    # CCI
    for v in [-100, -150]:
        triggers.append(('CCI', 'CCI_14', v))
    # WillR
    for v in [-80, -90]:
        triggers.append(('WillR', 'WILLR_14', v))
    # Stoch
    for v in [10, 20]:
        triggers.append(('Stoch', 'STOCH_K', v))
    # BB
    triggers.append(('BB_Low', 'BB_LOWER', 0))
        
    vol_opts = [0, 1.5, 2.0, 3.0]
    dip_opts = [0, -0.005, -0.01]
    trend_opts = [False, True]
    candle_opts = [False, True]
    
    combinations = list(itertools.product(triggers, vol_opts, dip_opts, trend_opts, candle_opts))
    
    print(f"🔥 Starting Exhaustive Search on {len(combinations)} strategies...")
    print("   Criteria: 2 periods >= 60%, All >= 40%, Trades >= 100")
    
    candidates = []
    
    start_time = time.time()
    
    for i, (trig, vol, dip, trend, candle) in enumerate(combinations):
        if i % 100 == 0:
            print(f"  {i}/{len(combinations)}...", end='\r')
            
        t_type, t_col, t_val = trig
        
        name = f"{t_col}<{t_val}"
        if t_type == 'BB_Low': name = "Close<BB_Low"
        if vol > 0: name += f" + Vol>{vol}"
        if dip < 0: name += f" + Dip<{dip}"
        if trend: name += " + Trend"
        if candle: name += " + Green"
        
        stats = {}
        total_trades = 0
        
        for p in PERIODS:
            pw, pt = 0, 0
            for c in TARGET_COINS:
                df = datasets[p][c]
                
                if t_type == 'BB_Low':
                    cond = df['close'] < df['BB_LOWER']
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
            # Structuring for JSON
            strategy_info = {
                "name": name,
                "logic": {
                    "trigger_type": t_type,
                    "trigger_col": t_col,
                    "trigger_val": t_val,
                    "min_volume_ratio": vol,
                    "max_dip": dip,
                    "trend_filter": trend,
                    "green_candle": candle
                },
                "total_trades": total_trades,
                "periods": {}
            }
            for p in PERIODS:
                strategy_info['periods'][p] = {
                    "win_rate": round(stats[p]['wr'], 4),
                    "trades": stats[p]['t']
                }
            
            candidates.append(strategy_info)
            
    print(f"\n🏁 Search Complete in {time.time() - start_time:.1f}s")
    print(f"   Found {len(candidates)} candidates.")
    
    # Save to JSON
    if candidates:
        # Sort by total trades (desc)
        candidates.sort(key=lambda x: x['total_trades'], reverse=True)
        
        output_file = "strategies_passed_all.json"
        with open(output_file, 'w') as f:
            json.dump(candidates, f, indent=2)
        print(f"💾 Saved all candidates to {output_file}")
        
        print("\n🏆 TOP 3 PREVIEW:")
        for rank, strat in enumerate(candidates[:3]):
            print(f"#{rank+1}: {strat['name']} (Trades: {strat['total_trades']})")
    else:
        print("\n💀 Absolutely no strategy passed.")

if __name__ == "__main__":
    run_exhaustive_search()
