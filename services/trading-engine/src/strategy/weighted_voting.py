"""가중치 투표 전략 (Weighted Voting Strategy)

백테스트 결과 기반 4개 전략 조합 (0.7%/-0.6% 테스트 기준):
- MFI(14) < 10 + Dip < -1.0% + 60m 상승추세: +2점
- RSI(5) < 5 + Dip < -1.0% + 60m 상승추세: +2점
- RSI(14) < 10 + Dip < -0.5% (추세 무관): +2점
- RSI(14) < 15 + Dip < -0.5% + 60m 상승추세: +1점

총점 4점 이상일 때 매수
매도: -0.6% 손절, +0.7% 도달 시 early 활성화, 0.5% 이하 시 익절
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from ..config.params import Params
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, BuyReason, SellReason
from .state_tracker import StrategyStateTracker
from .indicators import (
    OHLCData,
    OHLCVData,
    BollingerBands,
    calculate_rsi,
    calculate_bollinger_bands,
    calculate_mfi,
    calculate_sma,
)


# 전략 파라미터
BUY_THRESHOLD = 4  # 매수 점수 임계값
INVESTMENT_RATIO = 0.3  # 투자 비율 (잔고의 30%)

# 매도 파라미터
STOP_LOSS = -0.6  # 손절 -0.6%
EARLY_TARGET = 0.7  # Early 타겟 +0.7%
EARLY_EXIT = 0.5  # Early exit +0.5% (바닥선)
EARLY_DROP = 0.5  # Early 고점 대비 하락 +0.5% (트레일링)


@dataclass
class VotingScore:
    """투표 점수 상세"""
    market: str
    total_score: int = 0

    # 개별 조건 충족 여부
    mfi_condition: bool = False   # MFI(14) < 10 + Dip + Trend
    rsi5_condition: bool = False  # RSI(5) < 5 + Dip + Trend
    rsi14_condition: bool = False # RSI(14) < 10 + Dip (no trend)
    rsi14_15_condition: bool = False  # RSI(14) < 15 + Dip + Trend

    # 지표 값
    rsi5: float = 50.0
    rsi14: float = 50.0
    mfi14: float = 50.0
    dip_percent: float = 0.0  # SMA20 대비 낙폭 (%)
    is_uptrend: bool = False  # 60m SMA20 > SMA60

    current_price: float = 0.0


class WeightedVotingStrategy:
    """가중치 투표 전략 (0.7%/-0.6% 버전)"""

    def __init__(self, params: Params, redis_portfolio=None):
        self.params = params

        # 캔들 데이터
        self.candles_1m: Dict[str, Candle] = {}
        self.candles_trade_check: Dict[str, Candle] = {}
        self.candles_short: Dict[str, Candle] = {}
        self.candles_long: Dict[str, Candle] = {}
        self.avg_quote_volumes: Dict[str, float] = {}

        # 상태 추적기
        self.state_tracker = StrategyStateTracker(params, redis_portfolio)

        # 가격/지표 히스토리 (코인별)
        self.price_history: Dict[str, List[float]] = {}      # 1분봉 종가 (RSI용)
        self.ohlcv_history: Dict[str, List[OHLCVData]] = {}  # 1분봉 OHLCV (MFI용)
        self.price_history_60m: Dict[str, List[float]] = {}  # 60분봉 종가 (SMA용)

        # 60분봉 집계용
        self.candle_buffer_60m: Dict[str, List[Candle]] = {}

        # BTC 히스토리 (미사용이지만 인터페이스 호환)
        self.btc_daily_history: List[float] = []
        self.current_date: str = ""

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        """캔들 데이터 업데이트"""
        self.candles_1m = candles_1m
        self.candles_trade_check = candles_trade_check
        self.candles_short = candles_short
        self.candles_long = candles_long
        self.avg_quote_volumes = avg_quote_volumes

        # 히스토리 업데이트
        self._update_histories()

    def _update_histories(self) -> None:
        """가격 및 OHLCV 히스토리 업데이트"""
        for market, candle in self.candles_1m.items():
            # 1분봉 종가 히스토리 (RSI용)
            if market not in self.price_history:
                self.price_history[market] = []
            self.price_history[market].append(candle.close)
            if len(self.price_history[market]) > 100:
                self.price_history[market] = self.price_history[market][-100:]

            # 1분봉 OHLCV 히스토리 (MFI용)
            if market not in self.ohlcv_history:
                self.ohlcv_history[market] = []
            self.ohlcv_history[market].append(OHLCVData(
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume
            ))
            if len(self.ohlcv_history[market]) > 100:
                self.ohlcv_history[market] = self.ohlcv_history[market][-100:]

            # 60분봉 집계 및 히스토리
            self._aggregate_60m_candle(market, candle)

    def _aggregate_60m_candle(self, market: str, candle: Candle) -> None:
        """60분봉 집계"""
        if market not in self.candle_buffer_60m:
            self.candle_buffer_60m[market] = []

        self.candle_buffer_60m[market].append(candle)

        # 60개 모이면 60분봉으로 집계
        if len(self.candle_buffer_60m[market]) >= 60:
            if market not in self.price_history_60m:
                self.price_history_60m[market] = []

            close_60m = self.candle_buffer_60m[market][-1].close
            self.price_history_60m[market].append(close_60m)

            if len(self.price_history_60m[market]) > 100:
                self.price_history_60m[market] = self.price_history_60m[market][-100:]

            self.candle_buffer_60m[market] = []

    def update_btc_history(self, current_time: datetime) -> None:
        """BTC 히스토리 업데이트 (인터페이스 호환용)"""
        pass

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        """매수 신호 분석 (가중치 투표)"""
        # 최소 잔고 체크
        if portfolio.quote_balance < 100000.0:
            return None

        # 모든 마켓에 대해 점수 계산
        best_score: Optional[VotingScore] = None

        for market, candle in self.candles_1m.items():
            score = self._calculate_voting_score(market, candle)

            if score.total_score >= BUY_THRESHOLD:
                if best_score is None or score.total_score > best_score.total_score:
                    best_score = score

        if best_score is None:
            return None

        # 투자 금액 계산
        investment_amount = portfolio.quote_balance * INVESTMENT_RATIO

        return Signal(
            action=SignalAction.BUY,
            market=best_score.market,
            reason=BuyReason.MOMENTUM_SURGE,
            price=best_score.current_price,
            amount=investment_amount,
        )

    def _calculate_voting_score(self, market: str, candle: Candle) -> VotingScore:
        """투표 점수 계산 (0.7%/-0.6% 기준)"""
        score = VotingScore(market=market, current_price=candle.close)

        # 1. RSI 계산
        prices = self.price_history.get(market, [])
        score.rsi5 = calculate_rsi(prices, 5)
        score.rsi14 = calculate_rsi(prices, 14)

        # 2. MFI(14) 계산
        ohlcv = self.ohlcv_history.get(market, [])
        score.mfi14 = calculate_mfi(ohlcv, 14)

        # 3. Dip 계산 (SMA20 대비 현재가 하락폭)
        sma20 = calculate_sma(prices, 20)
        if sma20 > 0:
            score.dip_percent = ((candle.close - sma20) / sma20) * 100

        # 4. 60분봉 상승추세 체크 (SMA20 > SMA60)
        prices_60m = self.price_history_60m.get(market, [])
        if len(prices_60m) >= 60:
            sma20_60m = calculate_sma(prices_60m, 20)
            sma60_60m = calculate_sma(prices_60m, 60)
            score.is_uptrend = sma20_60m > sma60_60m

        # === 점수 계산 (0.7%/-0.6% 테스트 기준) ===

        # 조건 1: MFI(14) < 10 + Dip < -1.0% + 60m 상승추세 → +2점
        if score.mfi14 < 10 and score.dip_percent < -1.0 and score.is_uptrend:
            score.mfi_condition = True
            score.total_score += 2

        # 조건 2: RSI(5) < 5 + Dip < -1.0% + 60m 상승추세 → +2점
        if score.rsi5 < 5 and score.dip_percent < -1.0 and score.is_uptrend:
            score.rsi5_condition = True
            score.total_score += 2

        # 조건 3: RSI(14) < 10 + Dip < -0.5% (추세 무관) → +2점
        if score.rsi14 < 10 and score.dip_percent < -0.5:
            score.rsi14_condition = True
            score.total_score += 2

        # 조건 4: RSI(14) < 15 + Dip < -0.5% + 60m 상승추세 → +1점
        if score.rsi14 < 15 and score.dip_percent < -0.5 and score.is_uptrend:
            score.rsi14_15_condition = True
            score.total_score += 1

        return score

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """매도 신호 분석 (단순 손절/익절)"""
        signals: List[Signal] = []

        for market, position in portfolio.positions.items():
            candle_1m = self.candles_1m.get(market)
            if candle_1m is None:
                continue

            current_price = candle_1m.close
            profit_percent = portfolio.get_profit_percent(market, current_price)

            # 상태 조회
            state = self.state_tracker.get_state(market)

            # 1. 손절 체크 (-0.6%)
            if profit_percent <= STOP_LOSS:
                signals.append(Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.STOP_LOSS,
                    price=current_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_percent,
                ))
                continue

            # 2. Early 익절 체크
            if state is not None and state.reached_early_target:
                # 고점 수익률 계산
                peak_profit = ((state.peak_price - position.avg_buy_price) / position.avg_buy_price) * 100
                profit_drop = peak_profit - profit_percent

                # 2-1. EXIT_CONDITION: 수익률이 바닥선 이하로 하락
                if profit_percent <= EARLY_EXIT:
                    signals.append(Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.EARLY_EXIT_CONDITION,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    ))
                    continue

                # 2-2. DROP_CONDITION: 고점 대비 하락폭이 임계값 이상
                if profit_drop >= EARLY_DROP:
                    signals.append(Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.EARLY_DROP_CONDITION,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    ))
                    continue

        return signals

    def update_strategy_state(
        self,
        portfolio: PortfolioState,
        candles_1m: Dict[str, Candle],
        current_date: str,
        top10_markets: Dict[str, bool],
    ) -> None:
        """전략 상태 업데이트"""
        for market, position in portfolio.positions.items():
            candle = candles_1m.get(market)
            if candle is None:
                continue

            state = self.state_tracker.get_state(market)
            if state is None:
                continue

            # 피크 가격 업데이트
            if candle.high > state.peak_price:
                state.peak_price = candle.high

            # Early 타겟 도달 체크 (+0.7%)
            profit_percent = ((candle.high - position.avg_buy_price) / position.avg_buy_price) * 100
            if not state.reached_early_target and profit_percent >= EARLY_TARGET:
                state.reached_early_target = True

    def on_position_opened(self, market: str, price: float) -> None:
        """포지션 오픈 시 호출"""
        self.state_tracker.track_position(market, price)

    def on_position_closed(self, market: str) -> None:
        """포지션 클로즈 시 호출"""
        self.state_tracker.untrack_position(market)

    def on_partial_sell(self, market: str, reason: str) -> None:
        """부분 매도 체결 후 호출"""
        pass
