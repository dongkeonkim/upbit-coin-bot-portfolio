import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.metrics import classification_report, precision_score, accuracy_score
from sklearn.tree import DecisionTreeClassifier, export_text
import os

# Define periods (matching settings.py)
# Train on ALL available data to build the best possible model for deployment
PERIODS = {
    "TRAIN": ["2024-01-01", "2025-12-31"], # Full range
    "TEST": ["2025-10-01", "2025-12-31"]    # Overlap with train just for monitoring metrics
}

def load_data():
    print("📂 Loading prepared data...")
    df = pd.read_csv("backtests/ml_prepared_data_upbit.csv")
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df

def split_data(df):
    print("✂️ Splitting data into Train and Test...")
    train_mask = (df['timestamp'] >= PERIODS["TRAIN"][0]) & (df['timestamp'] <= PERIODS["TRAIN"][1])
    test_mask = (df['timestamp'] >= PERIODS["TEST"][0]) & (df['timestamp'] <= PERIODS["TEST"][1])
    
    train_df = df[train_mask].copy()
    test_df = df[test_mask].copy()
    
    return train_df, test_df

def train_model(train_df, test_df):
    features = [
        # 기존 피처 (19개)
        'returns', 'dist_ma5', 'dist_ma15', 'dist_ma30', 'dist_ma60',
        'dist_ma120', 'dist_ma240', 'dist_ma480', 'rsi14', 'rsi60',
        'macd', 'macd_signal', 'macd_hist', 'mfi14', 'volatility_atr',
        'bb_width', 'vol_change', 'vol_ma20', 'vol_ma60',
        # 1차 추가 피처 (7개)
        'adx14', 'pullback_depth', 'bullish_reversal', 'vol_surge',
        'hour', 'is_active_hour', 'atr_change',
        # 2차 추가 피처 - 모멘텀 (5개)
        'stoch_k', 'stoch_d', 'williams_r', 'cci14', 'roc10',
        # 2차 추가 피처 - 캔들 패턴 (5개)
        'candle_body_ratio', 'upper_wick_ratio', 'lower_wick_ratio',
        'consecutive_up', 'consecutive_down',
        # 2차 추가 피처 - 가격 구조 (5개)
        'higher_high', 'higher_low', 'dist_high_20', 'dist_low_20', 'price_position',
        # 2차 추가 피처 - 거래량/변동성 (6개)
        'obv_slope', 'buying_pressure', 'bb_position', 'squeeze', 'range_ratio', 'ma_slope_20',
        # 2차 추가 피처 - 시간 (3개)
        'day_of_week', 'is_weekend', 'minutes_since_midnight',
        # 3차 추가 피처 - 거래대금 (4개, Binance 전용 4개 제외)
        'qv_change', 'qv_ma20', 'qv_ma60', 'qv_surge'
    ]
    
    print(f"🚀 Training XGBoost model with {len(features)} features...")
    
    X_train = train_df[features]
    y_train = train_df['target']
    X_test = test_df[features]
    y_test = test_df['target']
    
    model = xgb.XGBClassifier(
        n_estimators=1000,
        max_depth=5,
        learning_rate=0.01,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric='logloss',
        early_stopping_rounds=100
    )
    
    # Use 10% of training data for early stopping
    from sklearn.model_selection import train_test_split
    X_t, X_v, y_t, y_v = train_test_split(X_train, y_train, test_size=0.1, random_state=42)
    
    model.fit(X_t, y_t, eval_set=[(X_v, y_v)], verbose=False)
    
    # Feature Importance
    importances = model.feature_importances_
    feat_imp = pd.Series(importances, index=features).sort_values(ascending=False)

    print("\n" + "="*70)
    print("🌟 Feature Importances (XGBoost)")
    print("="*70)
    for i, (feat, imp) in enumerate(feat_imp.items(), 1):
        print(f"{i:2d}. {feat:<30} {imp:.6f}")

    # Top 25 features
    top_25 = feat_imp.head(25)
    print("\n" + "="*70)
    print("⭐ Top 25 Features")
    print("="*70)
    print(f"FEATURES = {list(top_25.index)}")

    # Predictions
    y_pred_proba = model.predict_proba(X_test)[:, 1]
    
    # Distribution of probabilities
    print("\n📊 Probability Distribution (Test Set):")
    print(pd.Series(y_pred_proba).describe())

    # Analyze different thresholds to find high-precision zones
    # We need approx. 50 trades in P3+P4 (approx 5 months) to meet 10 trades/30 days requirement.
    thresholds = [0.4, 0.42, 0.44, 0.45, 0.46, 0.48, 0.5]
    for thr in thresholds:
        y_pred = (y_pred_proba >= thr).astype(int)
        num_trades = y_pred.sum()
        if num_trades > 0:
            precision = precision_score(y_test, y_pred)
            accuracy = accuracy_score(y_test, y_pred)
            print(f"Threshold {thr:.2f}: Precision={precision:.4f}, Trades={num_trades}, Accuracy={accuracy:.4f}")
        else:
            print(f"Threshold {thr:.2f}: No trades triggered.")

    return model, features, top_25

def extract_rules(model, train_df, features):
    print("\n🌲 Extracting simple rules using Decision Tree...")
    
    # Use XGB model to label "good" entry points in training data
    train_df['xgb_prob'] = model.predict_proba(train_df[features])[:, 1]
    
    # Lower the threshold for rule extraction if needed
    prob_threshold = train_df['xgb_prob'].quantile(0.95) # Top 5%
    print(f"Using prob threshold: {prob_threshold:.4f} (Top 5% of training samples)")
    
    train_df['is_high_prob'] = (train_df['xgb_prob'] >= prob_threshold).astype(int)
    
    dt = DecisionTreeClassifier(max_depth=5, min_samples_leaf=30)
    dt.fit(train_df[features], train_df['is_high_prob'])
    
    tree_rules = export_text(dt, feature_names=features)
    print("📋 Extracted Decision Tree Rules (Approximating high-prob XGB zones):")
    print(tree_rules)

def main():
    print("="*70)
    print("🚀 Upbit Feature Importance Analysis")
    print("="*70)

    df = load_data()
    train_df, test_df = split_data(df)

    if len(train_df) == 0 or len(test_df) == 0:
        print("❌ Error: Train or Test set is empty. Check the date ranges.")
        return

    model, features, top_25 = train_model(train_df, test_df)
    extract_rules(model, train_df, features)

    # Save model using booster to avoid sklearn wrapper issues
    model_path = "backtests/ml_model_upbit.json"
    model.get_booster().save_model(model_path)
    print(f"\n💾 Model saved to {model_path}")

    # Save top 25 features
    top_25_path = "backtests/top25_features_upbit.txt"
    with open(top_25_path, 'w') as f:
        f.write("# Top 25 Features (Upbit)\n")
        f.write(f"FEATURES = {list(top_25.index)}\n")
    print(f"💾 Top 25 features saved to {top_25_path}")

if __name__ == "__main__":
    main()
