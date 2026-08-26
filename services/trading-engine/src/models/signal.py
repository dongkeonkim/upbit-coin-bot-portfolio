"""거래 신호 (Go simulation/engine.go Signal과 동일)"""

from dataclasses import dataclass
from typing import Optional
from enum import Enum


class SignalAction(str, Enum):
    """신호 유형"""

    BUY = "BUY"
    SELL = "SELL"


class SellReason(str, Enum):
    """매도 사유"""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"  # 목표가 도달 익절
    TRAILING_STOP = "trailing_stop"  # 트레일링 스탑 (Voting240m용)
    HOLD_TIMEOUT = "hold_timeout"  # 홀딩 타임아웃 (Voting240m용)
    # EARLY 트레일링 (ANCA와 동일 구조)
    EARLY_EXIT_CONDITION = "early_exit_condition"  # EARLY 수익률 exit 조건
    EARLY_DROP_CONDITION = "early_drop_condition"  # EARLY 고점대비 하락 조건
    TRAILING_STOP_EARLY = "trailing_stop_early"  # deprecated: 하위호환성 유지
    # EARLY 부분 매도 (partial_sell_enabled일 때 사용)
    EARLY_PARTIAL_SELL = "early_partial_sell"  # EARLY 1차 부분 매도 (50%)
    EARLY_DEFENSE_SELL = "early_defense_sell"  # EARLY 방어 매도 (나머지 전량)
    # ANCA 트레일링
    TRAILING_STOP_ANCA = "trailing_stop_anca"  # deprecated: 하위호환성 유지
    ANCA_EXIT_CONDITION = "anca_exit_condition"  # ANCA 수익률 exit 조건
    ANCA_DROP_CONDITION = "anca_drop_condition"  # ANCA 고점대비 하락 조건
    VOLUME_RANK_EXIT = "volume_rank_exit"
    PERIOD_END = "period_end"


class BuyReason(str, Enum):
    """매수 사유"""

    MOMENTUM_SURGE = "momentum_surge"


@dataclass
class Signal:
    """거래 신호"""

    action: SignalAction  # BUY 또는 SELL
    market: str  # 마켓 심볼
    reason: str  # 신호 사유
    price: float  # 현재 가격
    amount: Optional[float] = None  # BUY: 투자 금액 (KRW)
    volume: Optional[float] = None  # SELL: 매도 수량
    # SELL 전용: 수익/손실 정보
    avg_buy_price: Optional[float] = None  # 평균 매수가
    profit_percent: Optional[float] = None  # 수익률 (%)

    @classmethod
    def buy(cls, market: str, price: float, amount: float, reason: str = BuyReason.MOMENTUM_SURGE) -> "Signal":
        """매수 신호 생성"""
        return cls(
            action=SignalAction.BUY,
            market=market,
            reason=reason,
            price=price,
            amount=amount,
        )

    @classmethod
    def sell(cls, market: str, price: float, volume: float, reason: str) -> "Signal":
        """매도 신호 생성"""
        return cls(
            action=SignalAction.SELL,
            market=market,
            reason=reason,
            price=price,
            volume=volume,
        )

    def __repr__(self) -> str:
        if self.action == SignalAction.BUY:
            return f"Signal(BUY {self.market} @ {self.price:.2f}, amount={self.amount:.0f}, reason={self.reason})"
        else:
            return f"Signal(SELL {self.market} @ {self.price:.2f}, volume={self.volume:.8f}, reason={self.reason})"
