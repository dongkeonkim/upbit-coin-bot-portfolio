"""급락 후 반등 전략 (Drop Bounce Strategy)

매수 조건:
- 10분 내 최고가 대비 -2.5% 이상 하락
- 저점 대비 +0.5% 이상 반등
- 양봉 (종가 > 시가)
- 아래꼬리 >= 몸통 (강한 매수세 확인)
- 거래대금 < 60분 평균의 5배 (패닉셀링 회피)

매도 조건 (트레일링 스탑):
- 트레일링 활성화: +1.8% 도달 시
- 트레일링 익절: 고점 대비 -0.3% 하락 시
- 손절: -1.0% 도달 시

자금 관리:
- 투자 비율: 잔고의 30%
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set

from ..config.params import Params
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, BuyReason, SellReason
from ..utils.structured_logger import get_logger
from .state_tracker import StrategyStateTracker

logger = get_logger("drop_bounce")


# 전략 파라미터
LOOKBACK_MINUTES = 10  # 하락 체크 기간 (분)
DROP_THRESHOLD = 2.5   # 최소 하락률 (%)
BOUNCE_THRESHOLD = 0.5 # 최소 반등률 (%)
INVESTMENT_RATIO = 0.3 # 투자 비율 (잔고의 30%)

# 트레일링 스탑 파라미터
TRAILING_ACTIVATION = 1.8  # 트레일링 활성화 +1.8%
TRAILING_DROP = 0.3        # 고점 대비 -0.3% 하락 시 매도
STOP_LOSS = 1.0            # 손절 -1.0%

# 거래대금 필터 파라미터
VOLUME_LOOKBACK = 60       # 평균 계산 기간 (분)
MAX_VOLUME_MULT = 5.0      # 이 배수 이상이면 제외 (5배 이상 = 패닉 셀링)


@dataclass
class TrailingState:
    """트레일링 스탑 상태"""
    active: bool = False       # 트레일링 활성화 여부
    high_since_entry: float = 0.0  # 진입 후 최고가


@dataclass
class DropBounceSignal:
    """급락 후 반등 신호 상세"""
    market: str
    current_price: float
    high_10m: float      # 10분 내 최고가
    low_current: float   # 현재봉 저가
    drop_percent: float  # 하락률 (%)
    bounce_percent: float  # 반등률 (%)
    is_green: bool       # 양봉 여부
    triggered: bool = False  # 신호 발생 여부


class DropBounceStrategy:
    """급락 후 반등 전략"""

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

        # 가격 히스토리 (코인별, 최근 N분)
        self.ohlc_history: Dict[str, List[Dict]] = {}  # [{open, high, low, close}, ...]

        # 거래대금 히스토리 (코인별)
        self.volume_history: Dict[str, List[float]] = {}

        # 같은 분봉에서 반복 매수 방지
        self.signaled_markets_this_bar: Set[str] = set()
        self.last_bar_time: Optional[datetime] = None

        # 다음 봉 시가 매수를 위한 pending 신호
        self.pending_buy_markets: Set[str] = set()

        # 트레일링 스탑 상태 (마켓별)
        self.trailing_states: Dict[str, TrailingState] = {}

        # BTC 히스토리 (인터페이스 호환용)
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

        # OHLC 히스토리 업데이트
        self._update_histories()

    def _update_histories(self) -> None:
        """OHLC 및 거래대금 히스토리 업데이트"""
        for market, candle in self.candles_1m.items():
            # OHLC 히스토리
            if market not in self.ohlc_history:
                self.ohlc_history[market] = []

            self.ohlc_history[market].append({
                'open': candle.open,
                'high': candle.high,
                'low': candle.low,
                'close': candle.close,
            })

            # 최근 LOOKBACK + 여유분만 유지
            max_history = LOOKBACK_MINUTES + 5
            if len(self.ohlc_history[market]) > max_history:
                self.ohlc_history[market] = self.ohlc_history[market][-max_history:]

            # 거래대금 히스토리
            if market not in self.volume_history:
                self.volume_history[market] = []

            self.volume_history[market].append(candle.quote_volume)

            # 최근 VOLUME_LOOKBACK + 여유분만 유지
            max_vol_history = VOLUME_LOOKBACK + 5
            if len(self.volume_history[market]) > max_vol_history:
                self.volume_history[market] = self.volume_history[market][-max_vol_history:]

    def update_btc_history(self, current_time: datetime) -> None:
        """BTC 히스토리 업데이트 (인터페이스 호환용)"""
        pass

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        """매수 신호 분석 (다음 봉 시가 매수)"""
        # 분봉 변경 시 처리
        if self.last_bar_time != current_time:
            self.signaled_markets_this_bar.clear()
            self.last_bar_time = current_time

        # 1. 이전 봉에서 발생한 pending 신호 처리 (현재 봉 시가에 매수)
        if self.pending_buy_markets:
            for market in list(self.pending_buy_markets):
                if market in self.signaled_markets_this_bar:
                    continue

                candle = self.candles_1m.get(market)
                if candle is None:
                    continue

                # 최소 잔고 체크
                if portfolio.quote_balance < 100000.0:
                    logger.warning(
                        "buy_skipped_low_balance",
                        market=market,
                        balance=portfolio.quote_balance,
                    )
                    break

                # 현재 봉 시가에 매수
                entry_price = candle.open
                investment_amount = portfolio.quote_balance * INVESTMENT_RATIO

                # pending에서 제거만 하고, signaled_markets_this_bar에는 추가하지 않음
                # (이 분에서 새 신호 체크 시 같은 마켓이 다시 신호를 낼 수 있음)
                self.pending_buy_markets.discard(market)

                logger.info(
                    "buy_signal",
                    market=market,
                    price=entry_price,
                    amount=round(investment_amount),
                    balance=round(portfolio.quote_balance),
                )

                return Signal(
                    action=SignalAction.BUY,
                    market=market,
                    reason=BuyReason.MOMENTUM_SURGE,
                    price=entry_price,
                    amount=investment_amount,
                )

            # 남은 pending 신호 클리어 (잔고 부족 등으로 처리 못한 것)
            self.pending_buy_markets.clear()

        # 2. 현재 봉에서 새 신호 체크 및 상태 로깅
        coin_status = []
        for market, candle in self.candles_1m.items():
            # 이번 분봉에서 이미 처리한 마켓은 건너뜀
            if market in self.signaled_markets_this_bar:
                continue
            # 이미 pending에 있는 마켓은 건너뜀
            if market in self.pending_buy_markets:
                continue

            signal = self._check_drop_bounce(market, candle)

            # 히스토리 길이 확인
            history_len = len(self.ohlc_history.get(market, []))

            # 코인 상태 수집 (항상 로깅)
            coin_status.append({
                "m": market.replace("KRW-", ""),
                "hist": history_len,
                "drop": round(signal.drop_percent, 1),
                "bounce": round(signal.bounce_percent, 1),
                "green": "O" if signal.is_green else "X",
                "ready": "O" if history_len >= LOOKBACK_MINUTES + 1 else "X",
            })

            if signal.triggered:
                # 거래대금 필터 체크
                vol_ok = self._check_volume_filter(market)
                if not vol_ok:
                    logger.info(
                        "buy_filtered_volume",
                        market=market,
                        drop=round(signal.drop_percent, 2),
                        bounce=round(signal.bounce_percent, 2),
                    )
                    continue

                # pending에 추가 (다음 봉 시가에 매수) - 모든 신호 추가
                self.pending_buy_markets.add(market)
                self.signaled_markets_this_bar.add(market)  # 같은 봉에서 중복 체크 방지

                logger.info(
                    "buy_pending",
                    market=market,
                    drop=round(signal.drop_percent, 2),
                    bounce=round(signal.bounce_percent, 2),
                    next_bar_buy=True,
                )

        # 코인 상태 로깅 (매분)
        if coin_status:
            logger.info(
                "coin_status",
                coins=coin_status,
            )

        return None

    def _check_drop_bounce(self, market: str, candle: Candle) -> DropBounceSignal:
        """급락 후 반등 조건 체크"""
        signal = DropBounceSignal(
            market=market,
            current_price=candle.close,
            high_10m=0.0,
            low_current=candle.low,
            drop_percent=0.0,
            bounce_percent=0.0,
            is_green=candle.close > candle.open,
        )

        # 히스토리 체크 (현재봉은 이미 추가된 상태이므로 제외)
        history = self.ohlc_history.get(market, [])
        if len(history) < 2:
            return signal

        # 사용 가능한 히스토리로 최고가 계산 (현재봉 제외)
        lookback = min(LOOKBACK_MINUTES, len(history) - 1)
        recent = history[-(lookback + 1):-1]
        signal.high_10m = max(bar['high'] for bar in recent)

        # 하락률 계산 (최고가 대비 현재봉 저가)
        if signal.high_10m > 0:
            signal.drop_percent = (signal.high_10m - candle.low) / signal.high_10m * 100

        # 반등률 계산 (현재봉 저가 대비 종가)
        if candle.low > 0:
            signal.bounce_percent = (candle.close - candle.low) / candle.low * 100

        # 히스토리 부족 시 신호 발생 안 함
        if len(history) < LOOKBACK_MINUTES + 1:
            return signal

        # 조건 체크: 하락 >= 2.5%, 반등 >= 0.5%, 양봉
        if not (signal.drop_percent >= DROP_THRESHOLD and
                signal.bounce_percent >= BOUNCE_THRESHOLD and
                signal.is_green):
            return signal

        # 아래꼬리 조건: 아래꼬리 >= 몸통 (강한 매수세 확인)
        body = candle.close - candle.open  # 양봉이므로 close > open
        lower_wick = candle.open - candle.low
        if body > 0 and lower_wick < body:
            return signal  # 아래꼬리가 몸통보다 작으면 신호 무시

        signal.triggered = True
        return signal

    def _check_volume_filter(self, market: str) -> bool:
        """거래대금 필터 체크 - 평균 대비 너무 높으면 False"""
        vol_history = self.volume_history.get(market, [])

        if len(vol_history) < VOLUME_LOOKBACK + 1:
            return True  # 히스토리 부족하면 통과

        # 현재 거래대금 (마지막)
        current_volume = vol_history[-1]

        # 이전 VOLUME_LOOKBACK 기간 평균 (현재 제외)
        avg_volume = sum(vol_history[-(VOLUME_LOOKBACK + 1):-1]) / VOLUME_LOOKBACK

        if avg_volume <= 0:
            return True

        volume_ratio = current_volume / avg_volume

        # 배수 이상이면 필터링 (진입 안 함)
        return volume_ratio < MAX_VOLUME_MULT

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """매도 신호 분석 (트레일링 스탑)"""
        signals: List[Signal] = []
        position_status = []

        for market, position in portfolio.positions.items():
            candle_1m = self.candles_1m.get(market)
            if candle_1m is None:
                continue

            current_price = candle_1m.close
            high = candle_1m.high
            low = candle_1m.low

            # 트레일링 상태 가져오기
            trailing = self.trailing_states.get(market)
            if trailing is None:
                trailing = TrailingState()
                self.trailing_states[market] = trailing

            # 가격 계산
            activation_price = position.avg_buy_price * (1 + TRAILING_ACTIVATION / 100)
            sl_price = position.avg_buy_price * (1 - STOP_LOSS / 100)

            # 현재 수익률
            profit_pct = (current_price / position.avg_buy_price - 1) * 100

            # 1. 트레일링 로직
            if not trailing.active:
                # 활성화 체크: +1.8% 도달 시
                if high >= activation_price:
                    trailing.active = True
                    trailing.high_since_entry = high
                    logger.info(
                        "trailing_activated",
                        market=market,
                        high=high,
                        activation_price=round(activation_price),
                        profit_pct=round(profit_pct, 2),
                    )
            else:
                # 고점 갱신
                if high > trailing.high_since_entry:
                    trailing.high_since_entry = high

                # 트레일링 익절 체크: 고점 대비 -0.3% 하락 시
                trailing_exit_price = trailing.high_since_entry * (1 - TRAILING_DROP / 100)
                if low <= trailing_exit_price:
                    profit_percent = (trailing_exit_price / position.avg_buy_price - 1) * 100
                    logger.info(
                        "sell_signal",
                        market=market,
                        reason="trailing_stop",
                        profit_pct=round(profit_percent, 2),
                        high_since_entry=round(trailing.high_since_entry),
                        exit_price=round(trailing_exit_price),
                    )
                    signals.append(Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.TAKE_PROFIT,
                        price=trailing_exit_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    ))
                    continue

            # 2. 손절 체크 (트레일링 활성화 전에도 적용)
            if low <= sl_price:
                profit_percent = -STOP_LOSS
                logger.info(
                    "sell_signal",
                    market=market,
                    reason="stop_loss",
                    profit_pct=round(profit_percent, 2),
                    sl_price=round(sl_price),
                )
                signals.append(Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.STOP_LOSS,
                    price=sl_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_percent,
                ))
                continue

            # 포지션 상태 수집
            position_status.append({
                "m": market.replace("KRW-", ""),
                "pnl": round(profit_pct, 1),
                "trailing": "O" if trailing.active else "X",
                "to_sl": round((current_price / sl_price - 1) * 100, 1),  # 손절까지 거리
            })

        # 포지션 상태 로깅
        if position_status:
            logger.info(
                "position_status",
                positions=position_status,
            )

        return signals

    def update_strategy_state(
        self,
        portfolio: PortfolioState,
        candles_1m: Dict[str, Candle],
        current_date: str,
        top10_markets: Dict[str, bool],
    ) -> None:
        """전략 상태 업데이트 (이 전략은 추가 상태 없음)"""
        pass

    def on_position_opened(self, market: str, price: float) -> None:
        """포지션 오픈 시 호출"""
        self.state_tracker.track_position(market, price)
        # 트레일링 상태 초기화/리셋 (물타기 시에도 리셋)
        self.trailing_states[market] = TrailingState()

    def on_position_closed(self, market: str) -> None:
        """포지션 클로즈 시 호출"""
        self.state_tracker.untrack_position(market)
        # 트레일링 상태 제거
        if market in self.trailing_states:
            del self.trailing_states[market]

    def on_partial_sell(self, market: str, reason: str) -> None:
        """부분 매도 체결 후 호출"""
        pass
