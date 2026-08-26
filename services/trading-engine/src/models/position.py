"""포지션 및 포트폴리오 상태 (Go simulation/portfolio.go와 동일)"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from ..config.params import FEE_RATE


@dataclass
class Position:
    """개별 포지션"""

    market: str  # 마켓 심볼
    volume: float  # 보유 수량
    avg_buy_price: float  # 평균 매수가 (수수료 포함)

    def get_value(self, current_price: float) -> float:
        """현재 가치 계산"""
        return self.volume * current_price

    def get_profit_percent(self, current_price: float) -> float:
        """수익률 계산 (%)"""
        if self.avg_buy_price == 0:
            return 0.0
        return ((current_price - self.avg_buy_price) / self.avg_buy_price) * 100.0


class PortfolioState:
    """포트폴리오 상태 (Go PortfolioState와 동일)"""

    def __init__(self, initial_quote: float):
        """
        포트폴리오 상태 초기화 (Go NewPortfolioState와 동일)

        Args:
            initial_quote: 초기 잔고
        """
        self.initial_quote = initial_quote
        self.quote_balance = initial_quote
        self.positions: Dict[str, Position] = {}

    def get_total_value(self, current_prices: Dict[str, float]) -> float:
        """총 자산 가치 계산"""
        total = self.quote_balance
        for market, position in self.positions.items():
            price = current_prices.get(market, 0.0)
            total += position.get_value(price)
        return total

    def get_return_percent(self, current_prices: Dict[str, float]) -> float:
        """총 수익률 계산 (%)"""
        total_value = self.get_total_value(current_prices)
        if self.initial_quote == 0:
            return 0.0
        return ((total_value - self.initial_quote) / self.initial_quote) * 100.0

    def add_position(
        self, market: str, volume: float, price: float, fee_rate: float = FEE_RATE
    ) -> None:
        """
        포지션 추가 (매수) (Go AddPosition과 동일)

        Args:
            market: 마켓 심볼
            volume: 매수 수량
            price: 매수 가격
            fee_rate: 수수료율
        """
        if volume <= 0 or price <= 0:
            return

        # Go와 동일한 수수료 계산
        cost = volume * price
        fee = cost * fee_rate
        new_cost_with_fee = cost + fee

        # 잔고 확인
        if self.quote_balance < new_cost_with_fee:
            raise ValueError(
                f"insufficient quote balance: {self.quote_balance:.2f} < {new_cost_with_fee:.2f}"
            )

        if market in self.positions:
            # 기존 포지션이 있으면 평균 단가 계산 (수수료 포함)
            pos = self.positions[market]
            old_volume = pos.volume
            new_volume = old_volume + volume

            # 평균 단가 = (기존 총 비용 + 신규 비용) / 총 수량
            old_total_cost = old_volume * pos.avg_buy_price
            total_cost_for_avg = old_total_cost + new_cost_with_fee
            new_avg_price = total_cost_for_avg / new_volume

            # 포지션 업데이트
            pos.volume = new_volume
            pos.avg_buy_price = new_avg_price
        else:
            # 새 포지션 생성 (수수료 포함한 실제 단가)
            actual_unit_cost = new_cost_with_fee / volume

            self.positions[market] = Position(
                market=market,
                volume=volume,
                avg_buy_price=actual_unit_cost,
            )

        # 잔고 차감
        self.quote_balance -= new_cost_with_fee

    def remove_position(
        self, market: str, volume: float, price: float, fee_rate: float = FEE_RATE
    ) -> Optional[float]:
        """
        포지션 제거 (매도)

        Args:
            market: 마켓 심볼
            volume: 매도 수량
            price: 매도 가격
            fee_rate: 수수료율

        Returns:
            실현 손익 (KRW), 포지션이 없으면 None
        """
        if market not in self.positions:
            return None

        position = self.positions[market]
        if volume > position.volume:
            volume = position.volume

        # 매도 금액 계산 (수수료 차감)
        gross_amount = volume * price
        fee = gross_amount * fee_rate
        net_amount = gross_amount - fee

        # 실현 손익 계산
        cost_basis = volume * position.avg_buy_price
        realized_pnl = net_amount - cost_basis

        # 잔고 추가
        self.quote_balance += net_amount

        # 포지션 업데이트
        position.volume -= volume
        if position.volume < 1e-8:  # 부동소수점 오차 처리 (Go와 동일)
            del self.positions[market]

        return realized_pnl

    def has_position(self, market: str) -> bool:
        """포지션 보유 여부 확인"""
        return market in self.positions

    def get_position(self, market: str) -> Optional[Position]:
        """포지션 조회"""
        return self.positions.get(market)

    def get_profit_percent(self, market: str, current_price: float) -> float:
        """특정 포지션의 수익률 계산 (%)"""
        position = self.positions.get(market)
        if not position:
            return 0.0
        return position.get_profit_percent(current_price)
