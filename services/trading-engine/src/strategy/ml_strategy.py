"""ML 기반 고승률 전략 (XGBoost + Trailing Stop) - 듀얼 모델 (급락반등 + 상승추세)"""

import os
import json
import numpy as np
import pandas as pd
import xgboost as xgb
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from typing import Dict, List, Optional
from collections import deque

from ..config.params import Params, FEE_RATE
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction

# ML Model Path (코인별 모델) - trading-engine/models 기준
TRADING_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.getenv("ML_MODEL_DIR", os.path.join(TRADING_ENGINE_ROOT, "models"))

# 급락반등 모델 (30m_strict) - 우선 적용
BOUNCE_PARAMS_PATH = os.path.join(MODEL_DIR, "coin_params_30m_strict.json")
BOUNCE_MODEL_SUFFIX = "_30m_strict"

# 상승추세 모델 (trend) - 급락반등 신호 없을 때 적용
TREND_PARAMS_PATH = os.path.join(MODEL_DIR, "coin_params_trend.json")
TREND_MODEL_SUFFIX = "_trend"

# GENERIC 모델 (코인별 모델이 없는 코인용)
GENERIC_MODEL_PATH = os.path.join(MODEL_DIR, "ml_model_GENERIC.json")
GENERIC_PARAMS_PATH = os.path.join(MODEL_DIR, "generic_params.json")
GENERIC_COINS = ['KRW-PEPE']  # GENERIC 모델 사용 대상 코인

# 기본값 (파라미터 없는 코인용)
DEFAULT_HOLD_MINUTES = 120
DEFAULT_THRESHOLD = 0.44

# 코인별 기본 파라미터 (coin_params.json 로드 실패 시 폴백)
DEFAULT_COIN_PARAMS = {
    'KRW-BTC': {'hold_minutes': 120, 'threshold': 0.44},
    'KRW-ETH': {'hold_minutes': 60, 'threshold': 0.44},
    'KRW-XRP': {'hold_minutes': 120, 'threshold': 0.42},
    'KRW-SOL': {'hold_minutes': 120, 'threshold': 0.42},
    'KRW-ADA': {'hold_minutes': 120, 'threshold': 0.40},
    'KRW-DOGE': {'hold_minutes': 120, 'threshold': 0.40},
}

# 급락반등 모델 파라미터 로드
def _load_bounce_params() -> Dict:
    """급락반등 모델 파라미터 로드 (30m_strict)"""
    if os.path.exists(BOUNCE_PARAMS_PATH):
        try:
            with open(BOUNCE_PARAMS_PATH, 'r') as f:
                params = json.load(f)
            print(f"✅ 급락반등 파라미터 로드: {BOUNCE_PARAMS_PATH} ({len(params)}개 코인)")
            return params
        except Exception as e:
            print(f"⚠️ 급락반등 파라미터 로드 실패: {e}")
    return {}

# 상승추세 모델 파라미터 로드
def _load_trend_params() -> Dict:
    """상승추세 모델 파라미터 로드 (trend)"""
    if os.path.exists(TREND_PARAMS_PATH):
        try:
            with open(TREND_PARAMS_PATH, 'r') as f:
                params = json.load(f)
            print(f"✅ 상승추세 파라미터 로드: {TREND_PARAMS_PATH} ({len(params)}개 코인)")
            return params
        except Exception as e:
            print(f"⚠️ 상승추세 파라미터 로드 실패: {e}")
    return {}

BOUNCE_PARAMS = _load_bounce_params()
TREND_PARAMS = _load_trend_params()
# 호환성 유지 (기존 코드에서 COIN_PARAMS 사용하는 부분)
COIN_PARAMS = BOUNCE_PARAMS

# 전략 설정 (고정)
SL_PCT = 1.0
INVESTMENT_RATIO = 0.10  # 전체 자산의 10%씩 투자
HISTORY_SIZE = 500  # MACD EMA 계산을 위해 여유분 필요
COIN_MAX_POSITIONS = 3  # 코인당 최대 포지션 수

# 단계별 트레일링 스탑 설정 (도달 수익률, 고점 대비 하락 허용폭)
# 수익률이 높아질수록 더 큰 여유를 줌
TRAILING_TIERS = [
    (4.0, 1.0),  # +4.0% 도달 시: 고점 대비 -1.0% 하락 허용
    (3.2, 0.7),  # +3.2% 도달 시: 고점 대비 -0.7% 하락 허용
    (2.5, 0.5),  # +2.5% 도달 시: 고점 대비 -0.5% 하락 허용
    (1.8, 0.3),  # +1.8% 도달 시: 고점 대비 -0.3% 하락 허용 (기본)
]

# 코인별 기본 파라미터 (coin_params.json 로드 실패 시 폴백)
DEFAULT_COIN_PARAMS = {
    'KRW-BTC': {'hold_minutes': 120, 'threshold': 0.44},
    'KRW-ETH': {'hold_minutes': 60, 'threshold': 0.44},
    'KRW-XRP': {'hold_minutes': 120, 'threshold': 0.42},
    'KRW-SOL': {'hold_minutes': 120, 'threshold': 0.42},
    'KRW-ADA': {'hold_minutes': 120, 'threshold': 0.40},
    'KRW-DOGE': {'hold_minutes': 120, 'threshold': 0.40},
}
DEFAULT_THRESHOLD = 0.44
DEFAULT_HOLD_MINUTES = 120

@dataclass
class MLPositionState:
    entry_time: datetime
    entry_price: float
    hold_minutes: int = DEFAULT_HOLD_MINUTES  # 코인별 hold_minutes
    trailing_active: bool = False
    peak_price: float = 0.0
    peak_profit_pct: float = 0.0  # 도달한 최고 수익률 (단계별 트레일링용)
    entry_count: int = 1  # 추가 매수 횟수 추적

class MLStrategy:
    """XGBoost ML + Trailing Stop 전략 - 듀얼 모델 (급락반등 + 상승추세)"""

    def __init__(self, params: Params, redis_portfolio=None, live_mode: bool = False):
        self.params = params
        self.redis_portfolio = redis_portfolio  # Redis 연동 (entry_count 저장용)
        self.live_mode = live_mode  # 실시간 모드 (True면 웹소켓 매도만 사용)
        self.state_tracker = StrategyStateTracker(params, redis_portfolio)

        # 듀얼 모델 로드
        self.bounce_models: Dict[str, xgb.Booster] = {}  # 급락반등 모델
        self.trend_models: Dict[str, xgb.Booster] = {}   # 상승추세 모델
        self._load_bounce_models()
        self._load_trend_models()

        # GENERIC 모델 로드 (PEPE 등 코인별 모델이 없는 코인용)
        self.generic_model: Optional[xgb.Booster] = None
        self.generic_params: Optional[Dict] = None
        self._load_generic_model()

        # 호환성 유지 (기존 코드)
        self.models = self.bounce_models

        # 급락반등 모델 피처 (25개)
        self.bounce_features = [
            'bb_width', 'pullback_depth', 'dist_high_20', 'volatility_atr', 'squeeze',
            'price_position', 'williams_r', 'cci14', 'candle_body_ratio', 'range_ratio',
            'dist_ma5', 'stoch_k', 'dist_low_20', 'macd', 'returns',
            'higher_high', 'lower_wick_ratio', 'macd_hist', 'dist_ma120', 'macd_signal',
            'higher_low', 'vol_surge', 'buying_pressure', 'dist_ma15', 'obv_slope'
        ]

        # 상승추세 모델 피처 (25개) - 4개 신규 피처 포함
        self.trend_features = [
            'bb_width', 'pullback_depth', 'dist_high_20', 'volatility_atr', 'squeeze',
            'price_position', 'williams_r', 'cci14', 'candle_body_ratio', 'range_ratio',
            'dist_ma5', 'stoch_k', 'dist_low_20', 'macd', 'returns',
            'higher_high', 'lower_wick_ratio', 'macd_hist', 'macd_signal',
            'higher_low', 'vol_surge',
            'ma20_slope', 'ma_alignment', 'roc_60', 'adx14'  # 신규 피처
        ]

        # 호환성 유지
        self.features = self.bounce_features

        # Deque for O(1) appends
        self.history: Dict[str, deque] = {}
        self.position_states: Dict[str, MLPositionState] = {}

    def _load_bounce_models(self):
        """급락반등 모델 로드 (30m_strict)"""
        for market in BOUNCE_PARAMS.keys():
            coin = market.split('-')[1]  # KRW-BTC -> BTC
            model_path = os.path.join(MODEL_DIR, f"ml_model_{coin}{BOUNCE_MODEL_SUFFIX}.json")

            # 절대 경로 시도 (fallback)
            if not os.path.exists(model_path):
                model_path = os.path.join(os.getcwd(), "models", f"ml_model_{coin}{BOUNCE_MODEL_SUFFIX}.json")

            if os.path.exists(model_path):
                try:
                    booster = xgb.Booster()
                    booster.load_model(model_path)
                    self.bounce_models[market] = booster
                    params = BOUNCE_PARAMS[market]
                    print(f"✅ [급락반등] {market} 모델 로드 (thr={params['threshold']})")
                except Exception as e:
                    print(f"❌ [급락반등] {market} 모델 로드 실패: {e}")

        print(f"📊 급락반등 모델: {len(self.bounce_models)}개 로드")

    def _load_trend_models(self):
        """상승추세 모델 로드 (trend)"""
        for market in TREND_PARAMS.keys():
            coin = market.split('-')[1]  # KRW-BTC -> BTC
            model_path = os.path.join(MODEL_DIR, f"ml_model_{coin}{TREND_MODEL_SUFFIX}.json")

            # 절대 경로 시도 (fallback)
            if not os.path.exists(model_path):
                model_path = os.path.join(os.getcwd(), "models", f"ml_model_{coin}{TREND_MODEL_SUFFIX}.json")

            if os.path.exists(model_path):
                try:
                    booster = xgb.Booster()
                    booster.load_model(model_path)
                    self.trend_models[market] = booster
                    params = TREND_PARAMS[market]
                    print(f"✅ [상승추세] {market} 모델 로드 (thr={params['threshold']})")
                except Exception as e:
                    print(f"❌ [상승추세] {market} 모델 로드 실패: {e}")

        print(f"📊 상승추세 모델: {len(self.trend_models)}개 로드")

    def _load_generic_model(self):
        """GENERIC 모델 로드 (PEPE 등 코인별 모델이 없는 코인용)"""
        if os.path.exists(GENERIC_MODEL_PATH) and os.path.exists(GENERIC_PARAMS_PATH):
            try:
                self.generic_model = xgb.Booster()
                self.generic_model.load_model(GENERIC_MODEL_PATH)
                with open(GENERIC_PARAMS_PATH, 'r') as f:
                    self.generic_params = json.load(f)
                print(f"✅ GENERIC 모델 로드 (hold={self.generic_params['hold_minutes']}분, thr={self.generic_params['threshold']:.2f}) - 대상: {GENERIC_COINS}")
            except Exception as e:
                print(f"⚠️ GENERIC 모델 로드 실패: {e}")

    def load_initial_history(self, candles_by_market: Dict[str, List[Candle]]) -> None:
        """초기 캔들 히스토리 로드 (Redis에서 읽은 데이터)"""
        for market, candles in candles_by_market.items():
            if market not in self.history:
                self.history[market] = deque(maxlen=HISTORY_SIZE)

            # 시간순 정렬 (오래된 것부터, candle_time 우선)
            sorted_candles = sorted(candles, key=lambda c: c.candle_time or c.timestamp)

            for candle in sorted_candles:
                self.history[market].append({
                    'o': candle.open,
                    'h': candle.high,
                    'l': candle.low,
                    'c': candle.close,
                    'v': candle.volume,
                    't': candle.candle_time or candle.timestamp,  # 시간대 피처용 (candle_time 우선)
                    'qv': candle.quote_volume,  # 거래대금
                    'tc': getattr(candle, 'trade_count', 0),  # 거래 횟수
                    'tbv': getattr(candle, 'taker_buy_volume', 0),  # 테이커 매수량
                    'tbqv': getattr(candle, 'taker_buy_quote_volume', 0)  # 테이커 매수 거래대금
                })

            print(f"✅ {market}: {len(self.history[market])}개 히스토리 로드됨")

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        """캔들 데이터 업데이트"""
        for market, candle in candles_1m.items():
            if market not in self.history:
                self.history[market] = deque(maxlen=HISTORY_SIZE)
            
            # Dictionary로 저장 (DataFrame 생성 지연)
            self.history[market].append({
                'o': candle.open,
                'h': candle.high,
                'l': candle.low,
                'c': candle.close,
                'v': candle.volume,
                't': candle.candle_time or candle.timestamp,  # 시간대 피처용 (candle_time 우선)
                'qv': candle.quote_volume,  # 거래대금
                'tc': getattr(candle, 'trade_count', 0),  # 거래 횟수
                'tbv': getattr(candle, 'taker_buy_volume', 0),  # 테이커 매수량
                'tbqv': getattr(candle, 'taker_buy_quote_volume', 0)  # 테이커 매수 거래대금
            })

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        """매수 신호 분석 - 듀얼 모델 (급락반등 우선, 상승추세 후순위)"""
        # 모델이 하나도 없으면 리턴
        if not self.bounce_models and not self.trend_models and not self.generic_model:
            return None

        # 현재가 dictionary 생성 (전체 자산 계산용)
        current_prices: Dict[str, float] = {}
        for market, dq in self.history.items():
            if len(dq) > 0:
                current_prices[market] = dq[-1]['c']

        # 전체 자산 계산
        total_value = portfolio.get_total_value(current_prices)
        investment_amount = total_value * INVESTMENT_RATIO

        # 잔고가 투자금액 + 수수료 미만이면 매수 금지
        required_balance = investment_amount * (1 + FEE_RATE)

        if portfolio.quote_balance < required_balance:
            print(f"⚠️ 잔고 부족: 필요={required_balance:,.0f} / 보유={portfolio.quote_balance:,.0f} (총자산={total_value:,.0f})")
            return None

        best_signal = None
        best_prob = 0.0
        best_model_type = None

        for market, dq in self.history.items():
            # 다중 진입 체크
            entry_count = 0
            if self.redis_portfolio:
                entry_count = self.redis_portfolio.get_entry_count(market)
                if entry_count >= COIN_MAX_POSITIONS:
                    continue
                # 추가 매수 시 3분 쿨다운 체크
                if entry_count > 0:
                    entry_time_str = self.redis_portfolio.get_entry_time(market)
                    if entry_time_str:
                        try:
                            last_entry_time = datetime.fromisoformat(entry_time_str)
                            # timezone 맞추기
                            if last_entry_time.tzinfo is None:
                                last_entry_time = last_entry_time.replace(tzinfo=timezone.utc)
                            if current_time.tzinfo is None:
                                current_time = current_time.replace(tzinfo=timezone.utc)
                            if current_time - last_entry_time < timedelta(minutes=3):
                                continue  # 3분 미경과, 스킵
                        except ValueError:
                            pass
            elif market in self.position_states:
                state = self.position_states[market]
                entry_count = state.entry_count
                if entry_count >= COIN_MAX_POSITIONS:
                    continue
                # 추가 매수 시 3분 쿨다운 체크 (백테스트)
                if entry_count > 0:
                    last_entry_time = state.entry_time
                    if last_entry_time.tzinfo is None:
                        last_entry_time = last_entry_time.replace(tzinfo=timezone.utc)
                    check_time = current_time
                    if check_time.tzinfo is None:
                        check_time = check_time.replace(tzinfo=timezone.utc)
                    if check_time - last_entry_time < timedelta(minutes=3):
                        continue  # 3분 미경과, 스킵
            elif portfolio.positions.get(market):
                continue

            if len(dq) < 480:
                continue

            closes = np.array([x['c'] for x in dq])

            # ====== 1. 급락반등 모델 체크 (우선순위 1) ======
            if market in self.bounce_models:
                try:
                    bounce_params = BOUNCE_PARAMS.get(market, {})
                    bounce_threshold = bounce_params.get('threshold', DEFAULT_THRESHOLD)

                    # 급락반등 피처 계산
                    bounce_feats = self._calculate_features_fast(dq, closes)
                    input_data = pd.DataFrame([bounce_feats], columns=self.bounce_features)
                    dmatrix = xgb.DMatrix(input_data)
                    bounce_prob = self.bounce_models[market].predict(dmatrix)[0]

                    if bounce_prob >= bounce_threshold:
                        if bounce_prob > best_prob:
                            best_prob = bounce_prob
                            best_model_type = "BOUNCE"
                            best_signal = Signal(
                                action=SignalAction.BUY,
                                market=market,
                                reason=f"BOUNCE_{bounce_prob:.3f}≥{bounce_threshold:.2f}",
                                price=closes[-1],
                                amount=investment_amount
                            )
                        continue  # 급락반등 신호 있으면 상승추세 체크 안함
                except Exception as e:
                    pass

            # ====== 2. 상승추세 모델 체크 (우선순위 2) ======
            if market in self.trend_models:
                try:
                    trend_params = TREND_PARAMS.get(market, {})
                    trend_threshold = trend_params.get('threshold', DEFAULT_THRESHOLD)

                    # 상승추세 피처 계산 (4개 추가 피처 포함)
                    trend_feats = self._calculate_trend_features(dq, closes)
                    input_data = pd.DataFrame([trend_feats], columns=self.trend_features)
                    dmatrix = xgb.DMatrix(input_data)
                    trend_prob = self.trend_models[market].predict(dmatrix)[0]

                    if trend_prob >= trend_threshold:
                        if trend_prob > best_prob:
                            best_prob = trend_prob
                            best_model_type = "TREND"
                            best_signal = Signal(
                                action=SignalAction.BUY,
                                market=market,
                                reason=f"TREND_{trend_prob:.3f}≥{trend_threshold:.2f}",
                                price=closes[-1],
                                amount=investment_amount
                            )
                except Exception as e:
                    pass

            # ====== 3. GENERIC 모델 (폴백) ======
            if market in GENERIC_COINS and self.generic_model is not None:
                try:
                    threshold = self.generic_params.get('threshold', DEFAULT_THRESHOLD)
                    feats = self._calculate_features_fast(dq, closes)
                    input_data = pd.DataFrame([feats], columns=self.bounce_features)
                    dmatrix = xgb.DMatrix(input_data)
                    prob = self.generic_model.predict(dmatrix)[0]

                    if prob >= threshold and prob > best_prob:
                        best_prob = prob
                        best_model_type = "GENERIC"
                        best_signal = Signal(
                            action=SignalAction.BUY,
                            market=market,
                            reason=f"GENERIC_{prob:.3f}≥{threshold:.2f}",
                            price=closes[-1],
                            amount=investment_amount
                        )
                except Exception as e:
                    pass

        # 매수 신호 로그 출력
        if best_signal:
            print(f"🔔 [{best_model_type}] {best_signal.market} 매수신호: {best_signal.reason}")

        return best_signal

    def _calculate_features_fast(self, dq: deque, closes: np.ndarray) -> Dict[str, float]:
        """
        Upbit 훈련 코드(ml_optimize_per_coin_upbit.py)와 100% 동일한 피처 계산
        25개 피처만 계산 (모델이 사용하는 피처)
        """
        # pandas Series로 변환 (훈련 코드와 동일하게)
        open_series = pd.Series([x['o'] for x in dq])
        close_series = pd.Series([x['c'] for x in dq])
        high_series = pd.Series([x['h'] for x in dq])
        low_series = pd.Series([x['l'] for x in dq])
        volume_series = pd.Series([x['v'] for x in dq])

        f = {}

        # ============ Upbit 훈련 코드와 100% 동일한 계산 ============

        # 1. returns (수익률)
        returns = close_series.pct_change()
        f['returns'] = returns.iloc[-1] if not pd.isna(returns.iloc[-1]) else 0

        # 2. 이동평균 대비 거리 (dist_ma5, dist_ma15, dist_ma120)
        for w in [5, 15, 120]:
            ma = close_series.rolling(window=w).mean()
            dist = close_series / ma - 1
            f[f'dist_ma{w}'] = dist.iloc[-1] if not pd.isna(dist.iloc[-1]) else 0

        # 3. MACD (훈련과 동일 - pandas EWM)
        ema12 = close_series.ewm(span=12, adjust=False).mean()
        ema26 = close_series.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - macd_signal
        f['macd'] = macd.iloc[-1]
        f['macd_signal'] = macd_signal.iloc[-1]
        f['macd_hist'] = macd_hist.iloc[-1]

        # 4. ATR & volatility_atr
        tr = np.maximum(high_series - low_series,
                       np.maximum(abs(high_series - close_series.shift(1)),
                                  abs(low_series - close_series.shift(1))))
        atr14 = tr.rolling(window=14).mean()
        volatility_atr = atr14 / close_series
        f['volatility_atr'] = volatility_atr.iloc[-1] if not pd.isna(volatility_atr.iloc[-1]) else 0

        # 5. Bollinger Bands
        ma20 = close_series.rolling(window=20).mean()
        std20 = close_series.rolling(window=20).std()
        bb_upper = ma20 + (std20 * 2)
        bb_lower = ma20 - (std20 * 2)
        bb_width = (bb_upper - bb_lower) / close_series
        f['bb_width'] = bb_width.iloc[-1] if not pd.isna(bb_width.iloc[-1]) else 0

        # 6. vol_ma20 & vol_surge
        vol_ma20 = volume_series / volume_series.rolling(window=20).mean().replace(0, np.nan)
        f['vol_surge'] = 1 if (vol_ma20.iloc[-1] > 2.0) else 0

        # 7. pullback_depth
        recent_high_20 = high_series.rolling(window=20).max()
        pullback_depth = (recent_high_20 - close_series) / recent_high_20
        f['pullback_depth'] = pullback_depth.iloc[-1] if not pd.isna(pullback_depth.iloc[-1]) else 0

        # 8. dist_high_20, dist_low_20
        high_20 = high_series.rolling(window=20).max()
        low_20 = low_series.rolling(window=20).min()
        dist_high_20 = (high_20 - close_series) / high_20
        dist_low_20 = (close_series - low_20) / low_20.replace(0, np.nan)
        f['dist_high_20'] = dist_high_20.iloc[-1] if not pd.isna(dist_high_20.iloc[-1]) else 0
        f['dist_low_20'] = dist_low_20.iloc[-1] if not pd.isna(dist_low_20.iloc[-1]) else 0

        # 9. price_position (20봉 범위 내 위치 0~1)
        price_position = (close_series - low_20) / (high_20 - low_20).replace(0, np.nan)
        f['price_position'] = price_position.iloc[-1] if not pd.isna(price_position.iloc[-1]) else 0.5

        # 10. squeeze (볼린저밴드 수축 상태)
        bb_width_ma = bb_width.rolling(window=20).mean()
        squeeze = (bb_width < bb_width_ma * 0.75).astype(int)
        f['squeeze'] = squeeze.iloc[-1] if not pd.isna(squeeze.iloc[-1]) else 0

        # 11. Stochastic Oscillator
        low_14 = low_series.rolling(window=14).min()
        high_14 = high_series.rolling(window=14).max()
        stoch_k = 100 * (close_series - low_14) / (high_14 - low_14).replace(0, np.nan)
        f['stoch_k'] = stoch_k.iloc[-1] if not pd.isna(stoch_k.iloc[-1]) else 50.0

        # 12. Williams %R
        williams_r = -100 * (high_14 - close_series) / (high_14 - low_14).replace(0, np.nan)
        f['williams_r'] = williams_r.iloc[-1] if not pd.isna(williams_r.iloc[-1]) else -50.0

        # 13. CCI (Commodity Channel Index)
        typical_price = (high_series + low_series + close_series) / 3
        tp_ma = typical_price.rolling(window=14).mean()
        tp_std = typical_price.rolling(window=14).std()
        cci14 = (typical_price - tp_ma) / (0.015 * tp_std).replace(0, np.nan)
        f['cci14'] = cci14.iloc[-1] if not pd.isna(cci14.iloc[-1]) else 0

        # 14. 캔들 패턴 - 몸통/꼬리 비율
        candle_range = high_series - low_series
        candle_body = abs(close_series - open_series)
        candle_body_ratio = candle_body / candle_range.replace(0, np.nan)
        f['candle_body_ratio'] = candle_body_ratio.iloc[-1] if not pd.isna(candle_body_ratio.iloc[-1]) else 0.5

        lower_wick_ratio = (pd.concat([close_series, open_series], axis=1).min(axis=1) - low_series) / candle_range.replace(0, np.nan)
        f['lower_wick_ratio'] = lower_wick_ratio.iloc[-1] if not pd.isna(lower_wick_ratio.iloc[-1]) else 0

        # 15. Range Ratio (변동폭 비율)
        avg_range = candle_range.rolling(window=20).mean()
        range_ratio = candle_range / avg_range.replace(0, np.nan)
        f['range_ratio'] = range_ratio.iloc[-1] if not pd.isna(range_ratio.iloc[-1]) else 1.0

        # 16. Higher High / Higher Low
        prev_high = high_series.shift(1)
        prev_low = low_series.shift(1)
        higher_high = (high_series > prev_high).astype(int)
        higher_low = (low_series > prev_low).astype(int)
        f['higher_high'] = higher_high.iloc[-1] if not pd.isna(higher_high.iloc[-1]) else 0
        f['higher_low'] = higher_low.iloc[-1] if not pd.isna(higher_low.iloc[-1]) else 0

        # 17. Buying Pressure (매수 압력)
        buying_pressure = (close_series - low_series) / (high_series - low_series).replace(0, np.nan)
        f['buying_pressure'] = buying_pressure.iloc[-1] if not pd.isna(buying_pressure.iloc[-1]) else 0.5

        # 18. OBV Slope (기울기)
        obv = (np.sign(close_series.diff()) * volume_series).fillna(0).cumsum()
        obv_slope = (obv - obv.shift(10)) / 10
        f['obv_slope'] = obv_slope.iloc[-1] if not pd.isna(obv_slope.iloc[-1]) else 0.0

        return f

    def _calculate_trend_features(self, dq: deque, closes: np.ndarray) -> Dict[str, float]:
        """
        상승추세 모델용 피처 계산 (21개 기존 + 4개 신규)
        신규 피처: ma20_slope, ma_alignment, roc_60, adx14
        """
        # 기존 피처 계산 (공통 부분)
        open_series = pd.Series([x['o'] for x in dq])
        close_series = pd.Series([x['c'] for x in dq])
        high_series = pd.Series([x['h'] for x in dq])
        low_series = pd.Series([x['l'] for x in dq])
        volume_series = pd.Series([x['v'] for x in dq])

        f = {}

        # ===== 기존 피처 (21개) =====
        # returns
        returns = close_series.pct_change()
        f['returns'] = returns.iloc[-1] if not pd.isna(returns.iloc[-1]) else 0

        # dist_ma5
        ma5 = close_series.rolling(window=5).mean()
        dist_ma5 = close_series / ma5 - 1
        f['dist_ma5'] = dist_ma5.iloc[-1] if not pd.isna(dist_ma5.iloc[-1]) else 0

        # MACD
        ema12 = close_series.ewm(span=12, adjust=False).mean()
        ema26 = close_series.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - macd_signal
        f['macd'] = macd.iloc[-1]
        f['macd_signal'] = macd_signal.iloc[-1]
        f['macd_hist'] = macd_hist.iloc[-1]

        # ATR & volatility_atr
        tr = np.maximum(high_series - low_series,
                       np.maximum(abs(high_series - close_series.shift(1)),
                                  abs(low_series - close_series.shift(1))))
        atr14 = tr.rolling(window=14).mean()
        volatility_atr = atr14 / close_series
        f['volatility_atr'] = volatility_atr.iloc[-1] if not pd.isna(volatility_atr.iloc[-1]) else 0

        # Bollinger Bands
        ma20 = close_series.rolling(window=20).mean()
        std20 = close_series.rolling(window=20).std()
        bb_upper = ma20 + (std20 * 2)
        bb_lower = ma20 - (std20 * 2)
        bb_width = (bb_upper - bb_lower) / close_series
        f['bb_width'] = bb_width.iloc[-1] if not pd.isna(bb_width.iloc[-1]) else 0

        # vol_surge
        vol_ma20 = volume_series / volume_series.rolling(window=20).mean().replace(0, np.nan)
        f['vol_surge'] = 1 if (vol_ma20.iloc[-1] > 2.0) else 0

        # pullback_depth
        recent_high_20 = high_series.rolling(window=20).max()
        pullback_depth = (recent_high_20 - close_series) / recent_high_20
        f['pullback_depth'] = pullback_depth.iloc[-1] if not pd.isna(pullback_depth.iloc[-1]) else 0

        # dist_high_20, dist_low_20
        high_20 = high_series.rolling(window=20).max()
        low_20 = low_series.rolling(window=20).min()
        dist_high_20 = (high_20 - close_series) / high_20
        dist_low_20 = (close_series - low_20) / low_20.replace(0, np.nan)
        f['dist_high_20'] = dist_high_20.iloc[-1] if not pd.isna(dist_high_20.iloc[-1]) else 0
        f['dist_low_20'] = dist_low_20.iloc[-1] if not pd.isna(dist_low_20.iloc[-1]) else 0

        # price_position
        price_position = (close_series - low_20) / (high_20 - low_20).replace(0, np.nan)
        f['price_position'] = price_position.iloc[-1] if not pd.isna(price_position.iloc[-1]) else 0.5

        # squeeze
        bb_width_ma = bb_width.rolling(window=20).mean()
        squeeze = (bb_width < bb_width_ma * 0.75).astype(int)
        f['squeeze'] = squeeze.iloc[-1] if not pd.isna(squeeze.iloc[-1]) else 0

        # Stochastic
        low_14 = low_series.rolling(window=14).min()
        high_14 = high_series.rolling(window=14).max()
        stoch_k = 100 * (close_series - low_14) / (high_14 - low_14).replace(0, np.nan)
        f['stoch_k'] = stoch_k.iloc[-1] if not pd.isna(stoch_k.iloc[-1]) else 50.0

        # Williams %R
        williams_r = -100 * (high_14 - close_series) / (high_14 - low_14).replace(0, np.nan)
        f['williams_r'] = williams_r.iloc[-1] if not pd.isna(williams_r.iloc[-1]) else -50.0

        # CCI
        typical_price = (high_series + low_series + close_series) / 3
        tp_ma = typical_price.rolling(window=14).mean()
        tp_std = typical_price.rolling(window=14).std()
        cci14 = (typical_price - tp_ma) / (0.015 * tp_std).replace(0, np.nan)
        f['cci14'] = cci14.iloc[-1] if not pd.isna(cci14.iloc[-1]) else 0

        # 캔들 패턴
        candle_range = high_series - low_series
        candle_body = abs(close_series - open_series)
        candle_body_ratio = candle_body / candle_range.replace(0, np.nan)
        f['candle_body_ratio'] = candle_body_ratio.iloc[-1] if not pd.isna(candle_body_ratio.iloc[-1]) else 0.5

        lower_wick_ratio = (pd.concat([close_series, open_series], axis=1).min(axis=1) - low_series) / candle_range.replace(0, np.nan)
        f['lower_wick_ratio'] = lower_wick_ratio.iloc[-1] if not pd.isna(lower_wick_ratio.iloc[-1]) else 0

        # Range Ratio
        avg_range = candle_range.rolling(window=20).mean()
        range_ratio = candle_range / avg_range.replace(0, np.nan)
        f['range_ratio'] = range_ratio.iloc[-1] if not pd.isna(range_ratio.iloc[-1]) else 1.0

        # Higher High / Higher Low
        prev_high = high_series.shift(1)
        prev_low = low_series.shift(1)
        higher_high = (high_series > prev_high).astype(int)
        higher_low = (low_series > prev_low).astype(int)
        f['higher_high'] = higher_high.iloc[-1] if not pd.isna(higher_high.iloc[-1]) else 0
        f['higher_low'] = higher_low.iloc[-1] if not pd.isna(higher_low.iloc[-1]) else 0

        # ===== 신규 피처 (4개) - 상승추세 전용 =====

        # 1. ma20_slope (MA20 기울기 - 상승 추세 강도)
        ma20_slope = (ma20 - ma20.shift(20)) / ma20.shift(20).replace(0, np.nan)
        f['ma20_slope'] = ma20_slope.iloc[-1] if not pd.isna(ma20_slope.iloc[-1]) else 0

        # 2. ma_alignment (MA 정배열: 5 > 20 > 60)
        ma60 = close_series.rolling(window=60).mean()
        ma_alignment = ((ma5 > ma20) & (ma20 > ma60)).astype(int)
        f['ma_alignment'] = ma_alignment.iloc[-1] if not pd.isna(ma_alignment.iloc[-1]) else 0

        # 3. roc_60 (60분 수익률 - 중기 모멘텀)
        roc_60 = close_series / close_series.shift(60).replace(0, np.nan) - 1
        f['roc_60'] = roc_60.iloc[-1] if not pd.isna(roc_60.iloc[-1]) else 0

        # 4. adx14 (Average Directional Index - 추세 강도)
        plus_dm = high_series.diff()
        minus_dm = low_series.diff().abs() * -1
        plus_dm = plus_dm.where((plus_dm > minus_dm.abs()) & (plus_dm > 0), 0)
        minus_dm = minus_dm.abs().where((minus_dm.abs() > plus_dm) & (minus_dm < 0), 0)
        plus_di = 100 * (plus_dm.rolling(window=14).mean() / atr14.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(window=14).mean() / atr14.replace(0, np.nan))
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
        adx14 = dx.rolling(window=14).mean()
        f['adx14'] = adx14.iloc[-1] if not pd.isna(adx14.iloc[-1]) else 0

        return f

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """
        매도 신호 분석

        - 실시간 모드(live_mode=True): 웹소켓(price_monitor.py)에서 bid1 기준으로 처리
        - 백테스트 모드(live_mode=False): 캔들 기반으로 처리
          - 손절: -1.0%
          - 트레일링: +1.8% 활성화 → 고점 대비 -0.3% 하락 시 매도
        """
        # 실시간 모드에서는 웹소켓 매도만 사용 (price_monitor.py)
        if self.live_mode:
            return []

        signals = []

        for market, position in portfolio.positions.items():
            # Deque의 마지막 요소(현재 봉) 가져오기
            last_candle = self.history[market][-1]
            current_price = last_candle['c']
            high_price = last_candle['h']
            low_price = last_candle['l']

            # 캔들 타임스탬프 사용 (백테스트/라이브 모두 정확)
            # 중요: 피처 계산과 동일하게 UTC 사용 (일관성 유지)
            candle_time = last_candle.get('t')
            if candle_time is None:
                current_time = datetime.now(timezone.utc)
            elif hasattr(candle_time, 'tzinfo'):
                current_time = candle_time
            else:
                # candle_time은 밀리초 타임스탬프이므로 unit='ms' 필수!
                current_time = pd.to_datetime(candle_time, unit='ms', utc=True)
            
            # 상태 복구 또는 초기화
            if market not in self.position_states:
                # Redis에서 entry_time 복구 시도
                restored_entry_time = current_time
                if self.redis_portfolio:
                    entry_time_str = self.redis_portfolio.get_entry_time(market)
                    if entry_time_str:
                        try:
                            restored_entry_time = datetime.fromisoformat(entry_time_str)
                        except ValueError:
                            pass  # 파싱 실패 시 현재 시간 사용

                self.position_states[market] = MLPositionState(
                    entry_time=restored_entry_time,
                    entry_price=position.avg_buy_price,
                    peak_price=position.avg_buy_price  # 초기값은 진입가 (trailing 활성화 전까지 사용 안함)
                )

            state = self.position_states[market]

            # 수익률 계산 (고가 기준 - 캔들 내 최고점)
            high_profit_pct = ((high_price - position.avg_buy_price) / position.avg_buy_price) * 100

            # 최고 수익률 및 고점 갱신
            if high_profit_pct > state.peak_profit_pct:
                state.peak_profit_pct = high_profit_pct
            if high_price > state.peak_price:
                state.peak_price = high_price

            # 1. 손절 체크 - 평균 매수가 기준 -1.0%
            sl_price = position.avg_buy_price * (1 - SL_PCT / 100)
            if low_price <= sl_price:
                signals.append(self._create_sell_signal(market, position, sl_price, "ML_SL_HIT"))
                continue

            # 2. 단계별 트레일링 스탑 체크
            trailing_drop_pct = None
            for tier_pct, drop_pct in TRAILING_TIERS:
                if state.peak_profit_pct >= tier_pct:
                    trailing_drop_pct = drop_pct
                    state.trailing_active = True
                    break

            if state.trailing_active and trailing_drop_pct is not None:
                trailing_stop_price = state.peak_price * (1 - trailing_drop_pct / 100)
                if low_price <= trailing_stop_price:
                    reason = f"ML_TRAILING_STOP(tier:{state.peak_profit_pct:.1f}%,drop:{trailing_drop_pct}%)"
                    signals.append(self._create_sell_signal(market, position, trailing_stop_price, reason))
                    continue

            # 3. 시간 종료 - 비활성화 (ML 학습의 30분은 성공/실패 판단용, 실매매에서는 불필요)
            # if market in GENERIC_COINS and self.generic_params:
            #     hold_minutes = self.generic_params.get('hold_minutes', DEFAULT_HOLD_MINUTES)
            # else:
            #     coin_params = COIN_PARAMS.get(market, {})
            #     hold_minutes = coin_params.get('hold_minutes', DEFAULT_HOLD_MINUTES)
            # holding_time = current_time - state.entry_time
            # if holding_time >= timedelta(minutes=hold_minutes):
            #     signals.append(self._create_sell_signal(market, position, current_price, "ML_TIME_EXIT"))
            #     continue

        return signals

    def _create_sell_signal(self, market, position, price, reason):
        if market in self.position_states:
            del self.position_states[market]
        return Signal(
            action=SignalAction.SELL,
            market=market,
            reason=reason,
            price=price,
            volume=position.volume,
            avg_buy_price=position.avg_buy_price,
            profit_percent=(price - position.avg_buy_price)/position.avg_buy_price*100
        )

    def on_position_opened(self, market: str, price: float) -> None:
        self.state_tracker.track_position(market, price)

        # 캔들 타임스탬프 사용 (백테스트/라이브 모두 정확)
        # 중요: 피처 계산과 동일하게 UTC 사용 (일관성 유지)
        candle_entry_time = datetime.now(timezone.utc)  # 기본값 (UTC)
        if market in self.history and len(self.history[market]) > 0:
            candle_time = self.history[market][-1].get('t')
            if candle_time is not None:
                if hasattr(candle_time, 'tzinfo'):
                    candle_entry_time = candle_time
                else:
                    # candle_time은 밀리초 타임스탬프이므로 unit='ms' 필수!
                    candle_entry_time = pd.to_datetime(candle_time, unit='ms', utc=True)

        # Redis에서 기존 포지션 확인 (entry_time 또는 entry_count가 있으면 기존 포지션)
        is_existing_position = False
        existing_entry_time = None
        existing_entry_count = 0

        if self.redis_portfolio:
            try:
                entry_time_str = self.redis_portfolio.get_entry_time(market)
                existing_entry_count = self.redis_portfolio.get_entry_count(market)

                # entry_time이 있거나 entry_count > 0이면 기존 포지션
                if entry_time_str or existing_entry_count > 0:
                    is_existing_position = True
                    if entry_time_str:
                        try:
                            existing_entry_time = datetime.fromisoformat(entry_time_str)
                        except ValueError:
                            pass
            except Exception:
                pass  # Redis 연결 문제 시 신규 포지션으로 처리

        if is_existing_position and market not in self.position_states:
            # 재시작 후 기존 포지션 복구 (entry_count 증가하지 않음)
            self.position_states[market] = MLPositionState(
                entry_time=existing_entry_time or candle_entry_time,
                entry_price=price,
                hold_minutes=self.get_hold_minutes(market),  # 코인별 hold_minutes
                peak_price=price,
                entry_count=existing_entry_count if existing_entry_count > 0 else 1
            )
            return

        # 실제 신규 매수 또는 추가 매수
        if self.redis_portfolio:
            new_count = self.redis_portfolio.increment_entry_count(market)
        else:
            # 백테스트 모드: 메모리에서 entry_count 증가
            if market in self.position_states:
                new_count = self.position_states[market].entry_count + 1
            else:
                new_count = 1

        if market in self.position_states:
            # 추가 매수 시 상태 업데이트
            state = self.position_states[market]
            state.entry_count = new_count
            state.entry_price = price  # 마지막 진입가로 갱신
            state.peak_price = max(state.peak_price, price)
            # 추가 매수 시 트레일링은 다시 트리거를 기다리도록 리셋
            state.trailing_active = False
            # 추가 매수 시 entry_time 갱신 (타이머 재시작) - 캔들 타임스탬프 사용
            entry_time = candle_entry_time
            state.entry_time = entry_time
            if self.redis_portfolio:
                self.redis_portfolio.set_entry_time(market, entry_time.isoformat())
        else:
            # 신규 진입 - 캔들 타임스탬프 사용
            entry_time = candle_entry_time
            self.position_states[market] = MLPositionState(
                entry_time=entry_time,
                entry_price=price,
                hold_minutes=self.get_hold_minutes(market),  # 코인별 hold_minutes
                peak_price=price,
                entry_count=new_count
            )
            # Redis에 entry_time 저장 (신규 진입 시에만)
            if self.redis_portfolio:
                self.redis_portfolio.set_entry_time(market, entry_time.isoformat())

    def get_hold_minutes(self, market: str) -> int:
        """코인별 hold_minutes 반환"""
        coin_params = COIN_PARAMS.get(market, {})
        return coin_params.get('hold_minutes', DEFAULT_HOLD_MINUTES)

    def on_position_closed(self, market: str) -> None:
        self.state_tracker.untrack_position(market)
        if market in self.position_states:
            del self.position_states[market]
        # Redis에서 entry_count 삭제 (clear_trailing_state에서 처리됨)

    def on_partial_sell(self, market: str, reason: str) -> None:
        pass

    def update_strategy_state(self, *args, **kwargs) -> None:
        pass

    def check_holding_buy_signals(self, portfolio: PortfolioState) -> Dict[str, bool]:
        """
        보유 중인 코인들에 대해 현재 매수 신호 발생 여부 체크

        매 분마다 호출되어 보유 코인이 현재 시점에서도 매수 신호가
        발생하는지 확인하고 Redis에 플래그를 저장합니다.

        Args:
            portfolio: 현재 포트폴리오 상태

        Returns:
            {market: has_buy_signal} 딕셔너리
        """
        results: Dict[str, bool] = {}

        if not portfolio.positions:
            return results

        for market in portfolio.positions.keys():
            has_signal = False

            # 모델 선택: 개별 모델 우선, 없으면 GENERIC
            if market in self.models:
                model = self.models[market]
                is_generic = False
            elif self.generic_model is not None:
                model = self.generic_model
                is_generic = True
            else:
                results[market] = False
                if self.redis_portfolio:
                    self.redis_portfolio.set_buy_signal_flag(market, False)
                continue

            # 히스토리 데이터 확인
            if market not in self.history or len(self.history[market]) < 480:
                results[market] = False
                if self.redis_portfolio:
                    self.redis_portfolio.set_buy_signal_flag(market, False)
                continue

            try:
                dq = self.history[market]

                # 파라미터 가져오기 (개별 또는 GENERIC)
                if is_generic:
                    coin_params = COIN_PARAMS.get("GENERIC", {})
                else:
                    coin_params = COIN_PARAMS.get(market, {})
                threshold = coin_params.get('threshold', DEFAULT_THRESHOLD)

                # Numpy 배열 추출
                closes = np.array([x['c'] for x in dq])

                # 피처 계산
                feats = self._calculate_features_fast(dq, closes)

                # 예측
                input_data = pd.DataFrame([feats], columns=self.features)
                dmatrix = xgb.DMatrix(input_data)
                prob = model.predict(dmatrix)[0]

                # Threshold 체크
                has_signal = prob >= threshold

                # 로그 (디버그용)
                model_type = "GENERIC" if is_generic else "ML"
                print(f"[{model_type}] {market} 보유중 신호체크: prob={prob:.4f}, thr={threshold:.2f} → {'✓ 매수신호' if has_signal else '✗'}")

            except Exception as e:
                print(f"[ML] {market} 신호 체크 실패: {e}")
                has_signal = False

            results[market] = has_signal

            # Redis에 플래그 저장
            if self.redis_portfolio:
                self.redis_portfolio.set_buy_signal_flag(market, has_signal)

        return results

    def update_btc_history(self, current_time: datetime) -> None:
        pass

# Import StrategyStateTracker locally to avoid circular import if needed
from .state_tracker import StrategyStateTracker