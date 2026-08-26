"""전략 상태 추적기 (Go strategy/strategy_state.go와 동일)"""

from dataclasses import dataclass, field
from typing import Dict, Optional, TYPE_CHECKING

from ..config.params import Params

if TYPE_CHECKING:
    from ..live.redis_portfolio import RedisPortfolio


@dataclass
class StrategyPositionState:
    """전략 포지션 상태"""

    peak_price: float = 0.0  # 고점 가격
    reached_early_target: bool = False  # EARLY 목표 도달 여부
    reached_anca_target: bool = False  # ANCA 목표 도달 여부
    early_partial_sold: bool = False  # EARLY 1차 부분 매도 완료 여부
    days_outside_top10: int = 0  # TOP10 밖에 있는 일수
    last_rank_check_date: str = ""  # 마지막 순위 체크 날짜


class StrategyStateTracker:
    """전략 상태 추적기 (Go StrategyStateTracker와 동일)"""

    def __init__(self, params: Params, redis_portfolio: Optional["RedisPortfolio"] = None):
        self.params = params
        self.states: Dict[str, StrategyPositionState] = {}
        self._redis = redis_portfolio  # Redis 연동 (선택적)

    def track_position(self, market: str, price: float) -> None:
        """포지션 추적 시작 (Go TrackPosition과 동일)"""
        # Redis에서 기존 상태 읽기
        reached_early = False
        reached_anca = False
        early_partial_sold = False
        peak_price = price
        days_outside_top10 = 0
        last_rank_check_date = ""

        if self._redis:
            reached_early = self._redis.get_trailing_flag(market, "early")
            reached_anca = self._redis.get_trailing_flag(market, "anca")
            early_partial_sold = self._redis.get_trailing_flag(market, "partial_sold")
            stored_peak = self._redis.get_highest_price(market)
            if stored_peak > 0:
                peak_price = max(price, stored_peak)
            # TOP10 상태 읽기
            days_outside_top10 = self._redis.get_days_outside_top10(market)
            last_rank_check_date = self._redis.get_last_rank_check_date(market)

        self.states[market] = StrategyPositionState(
            peak_price=peak_price,
            reached_early_target=reached_early,
            reached_anca_target=reached_anca,
            early_partial_sold=early_partial_sold,
            days_outside_top10=days_outside_top10,
            last_rank_check_date=last_rank_check_date,
        )

    def untrack_position(self, market: str) -> None:
        """포지션 추적 해제 (Go UntrackPosition과 동일)"""
        if market in self.states:
            del self.states[market]
            # Redis 트레일링 상태 초기화
            if self._redis:
                self._redis.clear_trailing_state(market)

    def get_state(self, market: str) -> Optional[StrategyPositionState]:
        """상태 조회 (Go GetState와 동일)"""
        return self.states.get(market)

    def update_peak_price(self, market: str, high_price: float, avg_buy_price: float) -> None:
        """
        고점 가격 업데이트 (Go UpdatePeakPrice와 동일)

        Args:
            market: 마켓 심볼
            high_price: 현재 고가
            avg_buy_price: 평균 매수가
        """
        state = self.states.get(market)
        if state is None:
            return

        # 고점 갱신
        if high_price > state.peak_price:
            state.peak_price = high_price
            # Redis에 저장
            if self._redis:
                self._redis.set_highest_price(market, high_price)

        # 수익률 계산
        if avg_buy_price == 0:
            return
        profit_percent = ((state.peak_price - avg_buy_price) / avg_buy_price) * 100.0

        # EARLY 목표 도달 체크
        if not state.reached_early_target and profit_percent >= self.params.trailing_target_early:
            state.reached_early_target = True
            # Redis에 저장
            if self._redis:
                self._redis.set_trailing_flag(market, "early", True)

        # ANCA 목표 도달 체크
        if not state.reached_anca_target and profit_percent >= self.params.trailing_target:
            state.reached_anca_target = True
            # Redis에 저장
            if self._redis:
                self._redis.set_trailing_flag(market, "anca", True)

    def set_early_partial_sold(self, market: str, sold: bool) -> None:
        """
        EARLY 부분 매도 완료 상태 설정

        Args:
            market: 마켓 심볼
            sold: 부분 매도 완료 여부
        """
        state = self.states.get(market)
        if state is None:
            return

        state.early_partial_sold = sold

        # Redis에 저장
        if self._redis:
            self._redis.set_trailing_flag(market, "partial_sold", sold)

    def update_ranking(self, market: str, is_in_top10: bool, current_date: str) -> None:
        """
        TOP10 순위 업데이트 (Go UpdateRanking과 동일)

        Args:
            market: 마켓 심볼
            is_in_top10: TOP10 내 여부
            current_date: 현재 날짜 (YYYY-MM-DD)
        """
        state = self.states.get(market)
        if state is None:
            return

        # 날짜가 변경되었을 때만 업데이트
        if state.last_rank_check_date != current_date:
            if not is_in_top10:
                state.days_outside_top10 += 1
            else:
                state.days_outside_top10 = 0
            state.last_rank_check_date = current_date

            # Redis에 저장
            if self._redis:
                self._redis.set_days_outside_top10(market, state.days_outside_top10)
                self._redis.set_last_rank_check_date(market, current_date)
