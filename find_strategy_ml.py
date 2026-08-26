import pandas as pd
import numpy as np
import pandas_ta as ta
from sqlalchemy import create_engine
from sklearn.tree import DecisionTreeClassifier, _tree
import itertools
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

def add_indicators(df):
    # Basic Indicators for ML
    df['RSI_14'] = ta.rsi(df['close'], length=14)
    df['RSI_2'] = ta.rsi(df['close'], length=2)
    
    # Bollinger Bands
    bb = ta.bbands(df['close'], length=20, std=2)
    if bb is not None:
        cols = bb.columns.tolist()
        upper = [c for c in cols if c.startswith('BBU')][0]
        lower = [c for c in cols if c.startswith('BBL')][0]
        df['BB_WIDTH'] = (bb[upper] - bb[lower]) / df['close']
        df['BB_PCT'] = (df['close'] - bb[lower]) / (bb[upper] - bb[lower])

    # Volume
    df['VOL_MA_20'] = ta.sma(df['volume'], length=20)
    df['VOL_RATIO'] = df['volume'] / df['VOL_MA_20']
    
    # Trend (SMA Distance)
    df['SMA_20'] = ta.sma(df['close'], length=20)
    df['DIST_SMA_20'] = (df['close'] - df['SMA_20']) / df['SMA_20']
    
    # Candle
    df['CHANGE_1M'] = df['close'].pct_change()
    
    return df

def label_data(df):
    labels = []
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    
    for i in range(n):
        entry_price = closes[i]
        tp = entry_price * (1 + TAKE_PROFIT)
        sl = entry_price * (1 + STOP_LOSS)
        
        end_search = min(i + LOOKAHEAD, n)
        if i + 1 >= end_search:
            labels.append(0) # Void -> 0 for training
            continue

        window_highs = highs[i+1 : end_search]
        window_lows = lows[i+1 : end_search]
        
        tp_hits = np.where(window_highs >= tp)[0]
        sl_hits = np.where(window_lows <= sl)[0]
        
        first_tp = tp_hits[0] if len(tp_hits) > 0 else 99999
        first_sl = sl_hits[0] if len(sl_hits) > 0 else 99999
        
        if first_tp < first_sl and first_tp < 99999:
            labels.append(1) # Win
        else:
            labels.append(0) # Loss or Void
            
    df['target'] = labels
    return df

def extract_rules(clf, feature_names):
    tree_ = clf.tree_
    feature_name = [
        feature_names[i] if i != _tree.TREE_UNDEFINED else "undefined!"
        for i in tree_.feature
    ]
    
    paths = []
    
    def recurse(node, path):
        if tree_.feature[node] != _tree.TREE_UNDEFINED:
            name = feature_name[node]
            threshold = tree_.threshold[node]
            p1, p2 = list(path), list(path)
            p1.append(f"{name} <= {threshold:.4f}")
            recurse(tree_.children_left[node], p1)
            p2.append(f"{name} > {threshold:.4f}")
            recurse(tree_.children_right[node], p2)
        else:
            # Leaf node
            value = tree_.value[node][0]
            samples = tree_.n_node_samples[node]
            # value = [Class 0, Class 1]
            success_prob = value[1] / samples if samples > 0 else 0
            
            # Filter: Win Rate >= 55% AND Samples >= 100
            if success_prob >= 0.55 and samples >= 100:
                paths.append((path, success_prob, samples))

    recurse(0, [])
    return sorted(paths, key=lambda x: x[1], reverse=True)

def run_ml_search():
    print("🚀 Running Machine Learning Search (Decision Tree)...")
    
    train_dfs = []
    for period in PERIODS:
        for coin in TARGET_COINS:
            # Load a subset to save time/memory, or full if possible
            # Let's load full P1 for training
            if period == "P1": 
                print(f"  Loading Training Data: {period} {coin}")
                df = load_data(coin, PERIODS[period][0], PERIODS[period][1])
                df = add_indicators(df)
                df = label_data(df)
                df.dropna(inplace=True)
                train_dfs.append(df)
    
    full_df = pd.concat(train_dfs)
    print(f"  Training Samples: {len(full_df)}")
    print(f"  Base Win Rate: {full_df['target'].mean():.2%}")
    
    features = ['RSI_14', 'RSI_2', 'BB_WIDTH', 'BB_PCT', 'VOL_RATIO', 'DIST_SMA_20', 'CHANGE_1M']
    X = full_df[features]
    y = full_df['target']
    
    # Train
    # Allow deeper tree since we have loose stop loss
    clf = DecisionTreeClassifier(max_depth=6, min_samples_leaf=100, class_weight='balanced', random_state=42)
    clf.fit(X, y)
    
    print("🔍 Extracting Rules...")
    rules = extract_rules(clf, features)
    
    print(f"  Found {len(rules)} potential rules.")
    
    for i, (path, prob, samples) in enumerate(rules[:5]):
        print(f"\nRule #{i+1} (Train WR: {prob:.1%}, Samples: {samples})")
        print(" AND ".join(path))

if __name__ == "__main__":
    run_ml_search()
