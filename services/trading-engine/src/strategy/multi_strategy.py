import json
import os
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from typing import Dict, List, Optional, Any

from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, SellReason

class MultiStrategy:
    """
    strategies_combined.json 기반 멀티 전략 + 스마트 트레일링
    """
    def __init__(self, config_path: str = "strategies_combined.json"):
        # 프로젝트 루트 경로 찾기 (docker 환경 등 고려)
        if not os.path.exists(config_path):
            # __file__ 기반 상대 경로 (services/trading-engine/src/strategy/ 에서 프로젝트 루트)
            base_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(base_dir, "..", "..", "..", ".."))

            potential_paths = [
                config_path,
                os.path.join("/app", config_path),  # Docker
                os.path.join(os.getcwd(), config_path),
                os.path.join(os.getcwd(), "..", "..", config_path),
                os.path.join(project_root, config_path),  # 프로젝트 루트
                os.path.join(base_dir, "..", "..", "..", "..", config_path),  # 상대 경로
            ]
            for p in potential_paths:
                if os.path.exists(p):
                    config_path = p
                    break

        with open(config_path, 'r') as f:
            self.strategies = json.load(f)
            
        print(f"✅ Loaded {len(self.strategies)} strategies from {config_path}")

        # 상태 관리 (매수된 포지션이 어떤 전략으로 들어갔는지, 트레일링 상태 등)
        # Key: Market, Value: { 'strategy_id': str, 'max_price': float, 'trailing_active': bool }
        # entry_price는 portfolio.positions[market].avg_buy_price 사용
        self.position_states: Dict[str, Dict[str, Any]] = {}
        
        # 데이터 버퍼 (리스트 기반 - 빠른 append)
        self.candles_list: Dict[str, list] = {}
        # 캐시된 DataFrame (지표 계산 시만 생성)
        self._df_cache: Dict[str, pd.DataFrame] = {}
        self._df_cache_len: Dict[str, int] = {}
        # 지표 캐시 (시간당 한 번만 계산)
        self._indicators_cache: Dict[str, pd.DataFrame] = {}
        self._indicators_cache_hour: Dict[str, int] = {}

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        """캔들 데이터 업데이트 (리스트 기반 - 빠름)"""
        for market, candle in candles_1m.items():
            if market not in self.candles_list:
                self.candles_list[market] = []

            ts = candle.candle_time or candle.timestamp
            candle_data = {
                'timestamp': ts,  # candle_time 우선
                'open': candle.open,
                'high': candle.high,
                'low': candle.low,
                'close': candle.close,
                'volume': candle.volume
            }

            # 마지막 캔들이 같은 시간이면 업데이트, 아니면 추가
            if self.candles_list[market] and self.candles_list[market][-1]['timestamp'] == ts:
                self.candles_list[market][-1] = candle_data
            else:
                self.candles_list[market].append(candle_data)

            # 메모리 관리를 위해 최근 4000개만 유지 (60m SMA_60 계산에 3600개 필요)
            if len(self.candles_list[market]) > 4000:
                self.candles_list[market] = self.candles_list[market][-4000:]
                # 캐시 무효화
                if market in self._df_cache:
                    del self._df_cache[market]
                    del self._df_cache_len[market]

    def _get_dataframe(self, market: str) -> pd.DataFrame:
        """지표 계산용 DataFrame 반환 (캐시 활용)"""
        if market not in self.candles_list:
            return pd.DataFrame()

        current_len = len(self.candles_list[market])

        # 캐시가 유효하면 반환
        if market in self._df_cache and self._df_cache_len.get(market) == current_len:
            return self._df_cache[market]

        # 새로 생성
        df = pd.DataFrame(self.candles_list[market])
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

        self._df_cache[market] = df
        self._df_cache_len[market] = current_len
        return df

    def update_btc_history(self, current_time: datetime) -> None:
        pass # 사용 안 함

    def update_strategy_state(self, portfolio: PortfolioState, candles_1m: Dict[str, Candle], current_date: str, top10: Dict[str, bool]) -> None:
        pass # 별도 상태 업데이트 필요 없음

    def on_position_opened(self, market: str, price: float) -> None:
        """포지션 진입 시 호출"""
        # analyze_buy에서 이미 상태를 설정했으므로, 여기서는 누락된 경우만 처리
        if market not in self.position_states:
             # Default fallback if not set
             self.position_states[market] = {
                 'strategy_id': 'unknown',
                 'max_price': price,
                 'trailing_active': False
             }
        else:
            # 체결 가격으로 max_price 업데이트 (슬리피지 반영)
            # entry_price는 portfolio.positions[market].avg_buy_price 사용
            self.position_states[market]['max_price'] = price

    def on_position_closed(self, market: str) -> None:
        """포지션 종료 시 호출"""
        if market in self.position_states:
            del self.position_states[market]

    def on_partial_sell(self, market: str, reason: str) -> None:
        pass

    def _resample_to_60m(self, df: pd.DataFrame) -> pd.DataFrame:
        """1분봉을 60분봉으로 리샘플링"""
        agg_dict = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }
        return df.resample('60min').agg(agg_dict).dropna()

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        필요한 지표 계산 (verify_strategy_performance.py와 동일한 방식)
        - 1m 지표: MFI_14, RSI_9, SMA_20, VOL_RATIO, DIST_SMA_20
        - 60m 지표: 60m_BB_LOWER, 60m_BB_UPPER, 60m_UPTREND
        """
        if len(df) < 60: return df  # 최소 1시간 데이터 필요

        # 복사본 생성
        df = df.copy()

        # === 1분봉 지표 ===
        # 1. MFI 14
        df['MFI_14'] = ta.mfi(df['high'], df['low'], df['close'], df['volume'], length=14)

        # 2. RSI 9
        df['RSI_9'] = ta.rsi(df['close'], length=9)

        # 3. SMA 20 & Distance (1분봉 기준)
        df['SMA_20'] = ta.sma(df['close'], length=20)
        df['DIST_SMA_20'] = (df['close'] - df['SMA_20']) / df['SMA_20']

        # 4. Volume SMA 20 & Ratio (1분봉 기준)
        df['VOL_MA_20'] = ta.sma(df['volume'], length=20)
        df['VOL_RATIO'] = df['volume'] / df['VOL_MA_20']

        # === 60분봉 지표 (리샘플링 필요) ===
        # 60분봉으로 리샘플링
        df_60m = self._resample_to_60m(df[['open', 'high', 'low', 'close', 'volume']])

        if len(df_60m) >= 20:
            # 60m Bollinger Bands
            bb_60m = ta.bbands(df_60m['close'], length=20, std=2)
            if bb_60m is not None:
                cols = bb_60m.columns.tolist()
                lower_col = [c for c in cols if c.startswith('BBL')][0]
                upper_col = [c for c in cols if c.startswith('BBU')][0]
                df_60m['60m_BB_LOWER'] = bb_60m[lower_col]
                df_60m['60m_BB_UPPER'] = bb_60m[upper_col]

            # 60m SMA for UPTREND
            sma_20 = ta.sma(df_60m['close'], length=20)
            sma_60 = ta.sma(df_60m['close'], length=60)

            # pandas_ta가 None을 반환할 수 있으므로 체크
            if sma_20 is not None and sma_60 is not None:
                df_60m['60m_SMA_20'] = sma_20
                df_60m['60m_SMA_60'] = sma_60
                # NaN을 0으로 채운 후 비교 (NaN인 경우 False)
                df_60m['60m_UPTREND'] = (
                    sma_20.fillna(0) > sma_60.fillna(float('inf'))
                )
            elif sma_20 is not None:
                df_60m['60m_SMA_20'] = sma_20
                df_60m['60m_UPTREND'] = False  # SMA_60 데이터 부족
            else:
                df_60m['60m_UPTREND'] = False  # 데이터 부족

            # 60m 지표를 1m 데이터프레임에 조인 (forward fill)
            cols_to_join = ['60m_BB_LOWER', '60m_BB_UPPER', '60m_UPTREND']
            existing_cols = [c for c in cols_to_join if c in df_60m.columns]
            if existing_cols:
                df = df.join(df_60m[existing_cols], how='left')
                df[existing_cols] = df[existing_cols].ffill()

        return df

    def _get_indicators_cached(self, market: str, current_time: datetime) -> Optional[pd.DataFrame]:
        """
        지표가 계산된 DataFrame 반환
        - 1m 지표: 최근 100개만 사용 (빠름)
        - 60m 지표: 시간당 한 번만 계산 → 캐시
        """
        if market not in self.candles_list:
            return None

        candles = self.candles_list[market]
        if len(candles) < 1200:  # 60분봉 BB 계산에 최소 20시간 데이터 필요
            return None

        # === 1분봉 지표: 최근 100개만 DataFrame으로 변환 (빠름) ===
        df = pd.DataFrame(candles[-100:])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df['MFI_14'] = ta.mfi(df['high'], df['low'], df['close'], df['volume'], length=14)
        df['RSI_9'] = ta.rsi(df['close'], length=9)
        df['SMA_20'] = ta.sma(df['close'], length=20)
        df['DIST_SMA_20'] = (df['close'] - df['SMA_20']) / df['SMA_20']
        df['VOL_MA_20'] = ta.sma(df['volume'], length=20)
        df['VOL_RATIO'] = df['volume'] / df['VOL_MA_20']

        # === 60분봉 지표 (캐시 사용) ===
        current_hour = int(current_time.timestamp() // 3600)

        # 캐시 체크
        if (market in self._indicators_cache_hour and
            self._indicators_cache_hour[market] == current_hour):
            # 캐시된 60m 지표 사용 - 현재 시간에 해당하는 값만 가져옴
            cached_60m = self._indicators_cache[market]
            # 마지막 유효한 60m 지표값 사용
            if not cached_60m.empty:
                last_60m = cached_60m.iloc[-1]
                df['60m_BB_LOWER'] = last_60m.get('60m_BB_LOWER')
                df['60m_BB_UPPER'] = last_60m.get('60m_BB_UPPER')
                df['60m_UPTREND'] = last_60m.get('60m_UPTREND', False)
        else:
            # 60m 지표 새로 계산 (전체 데이터 필요)
            df_full = pd.DataFrame(candles)
            df_full['timestamp'] = pd.to_datetime(df_full['timestamp'], unit='ms')
            df_full.set_index('timestamp', inplace=True)
            df_60m = self._resample_to_60m(df_full[['open', 'high', 'low', 'close', 'volume']])

            if len(df_60m) >= 20:
                # 60m Bollinger Bands
                bb_60m = ta.bbands(df_60m['close'], length=20, std=2)
                if bb_60m is not None:
                    cols = bb_60m.columns.tolist()
                    lower_col = [c for c in cols if c.startswith('BBL')][0]
                    upper_col = [c for c in cols if c.startswith('BBU')][0]
                    df_60m['60m_BB_LOWER'] = bb_60m[lower_col]
                    df_60m['60m_BB_UPPER'] = bb_60m[upper_col]

                # 60m SMA for UPTREND
                sma_20 = ta.sma(df_60m['close'], length=20)
                sma_60 = ta.sma(df_60m['close'], length=60)
                if sma_20 is not None and sma_60 is not None:
                    df_60m['60m_UPTREND'] = sma_20.fillna(0) > sma_60.fillna(float('inf'))
                else:
                    df_60m['60m_UPTREND'] = False

                # 캐시 저장
                self._indicators_cache[market] = df_60m[['60m_BB_LOWER', '60m_BB_UPPER', '60m_UPTREND']].copy()
                self._indicators_cache_hour[market] = current_hour

                # 마지막 60m 지표값을 1m DataFrame에 적용
                last_60m = df_60m.iloc[-1]
                df['60m_BB_LOWER'] = last_60m['60m_BB_LOWER']
                df['60m_BB_UPPER'] = last_60m['60m_BB_UPPER']
                df['60m_UPTREND'] = last_60m['60m_UPTREND']

        return df

    def analyze_buy(self, portfolio: PortfolioState, current_time: datetime) -> Optional[Signal]:
        """매수 신호 분석 - 무한 포지션 허용 (verify_strategy_performance.py와 동일)"""
        # 후보 코인 선정 (KRW-BTC, KRW-ETH, KRW-XRP 만 대상)
        target_coins = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

        # 현재 분에 이미 처리된 코인 추적 (같은 분 중복 방지)
        current_minute = int(current_time.timestamp() // 60)
        if not hasattr(self, '_last_buy_minute') or self._last_buy_minute != current_minute:
            self._last_buy_minute = current_minute
            self._bought_this_minute = set()

        for market in target_coins:
            # 같은 분에 이미 매수한 코인은 스킵 (무한루프 방지)
            if market in self._bought_this_minute:
                continue

            df = self._get_indicators_cached(market, current_time)
            if df is None or len(df) == 0:
                continue

            curr = df.iloc[-1]  # 현재 캔들

            # 3가지 전략 체크 (하나만 매칭되면 해당 코인은 완료)
            for strat in self.strategies:
                cond = strat['conditions']
                matched = True

                # Main Indicator
                ind = cond['indicator']
                op = cond['operator']
                val = cond['value']

                # Check Indicator Existence
                if ind not in curr:
                    matched = False
                    continue

                # Main Check
                if op == '<':
                    if not (curr[ind] < val): matched = False
                elif op == '>':
                    if not (curr[ind] > val): matched = False
                elif op == 'price_cmp':
                    # close < BB_LOWER
                    if val == 'close':
                        if not (curr['close'] < curr[ind]): matched = False

                if not matched: continue

                # Filter Checks
                for f in cond.get('filters', []):
                    f_name = f['name']
                    f_op = f['operator']
                    f_val = f['value']

                    if f_name not in curr:
                        matched = False
                        break

                    if f_op == '<':
                        if not (curr[f_name] < f_val): matched = False
                    elif f_op == '>':
                        if not (curr[f_name] > f_val): matched = False
                    elif f_op == '==':
                        # Boolean check (for UPTREND)
                        if bool(curr[f_name]) != bool(f_val): matched = False

                    if not matched: break

                if matched:
                    # 매수 신호 발생!
                    # 잔고 체크: 10만원 이하면 투자 불가
                    balance = portfolio.quote_balance
                    if balance <= 100000:
                        continue

                    # 투자 비중: 전체 예수금의 33% (3개 분산)
                    invest_amount = balance * 0.33
                    if invest_amount < 5000: continue # 최소 주문금액

                    # 같은 분 중복 매수 방지용 기록
                    self._bought_this_minute.add(market)

                    # 포지션 상태 저장 (market 키 사용)
                    # 기존 포지션이 있으면 max_price, trailing_active 리셋 (평균 매수가 변경되므로)
                    self.position_states[market] = {
                        'strategy_id': strat['id'],
                        'max_price': curr['close'],  # 신규/추가매수 시 현재가로 리셋
                        'trailing_active': False     # 트레일링 비활성화
                    }

                    print(f"🚀 BUY {market} via {strat['name']} @ {curr['close']}")
                    return Signal(
                        market=market,
                        action=SignalAction.BUY,
                        price=curr['close'],
                        amount=invest_amount,
                        reason=strat['name']
                    )

        return None

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """매도 신호 분석 (스마트 트레일링)"""
        signals = []

        for market, pos in portfolio.positions.items():
            df = self._get_dataframe(market)
            if df.empty: continue

            # 현재 캔들 데이터 (고가/저가/종가 모두 사용)
            curr_candle = df.iloc[-1]
            curr_high = curr_candle['high']
            curr_low = curr_candle['low']
            curr_close = curr_candle['close']

            # 포지션 상태 가져오기
            state = self.position_states.get(market)
            if not state:
                # 상태가 없으면(재시작 등) 초기화
                state = {
                    'strategy_id': 'unknown',
                    'max_price': curr_high,
                    'trailing_active': False
                }
                self.position_states[market] = state

            # 전략 설정 가져오기 (해당 전략의 exit_strategy)
            strat_conf = None
            for s in self.strategies:
                if s['id'] == state['strategy_id']:
                    strat_conf = s['exit_strategy']
                    break

            # 전략 정보를 못 찾으면 기본값 적용 (안전책)
            if not strat_conf:
                strat_conf = {
                    "stop_loss": -0.007,
                    "activation_threshold": 0.008,
                    "trailing_gap": 0.002,
                    "min_profit_guarantee": 0.0015
                }

            # --- Smart Trailing Logic ---
            entry_price = pos.avg_buy_price  # portfolio에서 평균 매수가 사용

            # 1. Update Max Price (고가 사용 - verify와 동일)
            if curr_high > state['max_price']:
                state['max_price'] = curr_high

            # 2. Check Activation
            if not state['trailing_active']:
                roi = (state['max_price'] - entry_price) / entry_price
                if roi >= strat_conf['activation_threshold']:
                    state['trailing_active'] = True
                    # print(f"✨ {market} Trailing Activated! Max: {roi*100:.2f}%")

            # 3. Calculate Stop Price
            if state['trailing_active']:
                # Trailing Mode
                stop_price = state['max_price'] * (1 - strat_conf['trailing_gap'])
                # Break-even guarantee
                be_price = entry_price * (1 + strat_conf['min_profit_guarantee'])
                stop_price = max(stop_price, be_price)

                sell_reason = SellReason.TAKE_PROFIT
            else:
                # Fixed SL Mode
                stop_price = entry_price * (1 + strat_conf['stop_loss'])
                sell_reason = SellReason.STOP_LOSS

            # 4. Check Exit Condition (저가 사용 - verify와 동일)
            if curr_low <= stop_price:
                # 청산 가격은 stop_price (verify와 동일)
                exit_price = stop_price
                pnl = (exit_price - entry_price) / entry_price * 100
                print(f"👋 SELL {market} ({sell_reason}) @ {exit_price} (PnL: {pnl:.2f}%)")
                
                signals.append(Signal(
                    market=market,
                    action=SignalAction.SELL,
                    price=exit_price,
                    volume=pos.volume,
                    reason=sell_reason
                ))
                
        return signals
