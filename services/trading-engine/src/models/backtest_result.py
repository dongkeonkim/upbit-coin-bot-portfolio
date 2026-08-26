"""백테스트 결과 (Go simulation/engine.go와 동일)"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Trade:
    """거래 기록"""

    timestamp: int  # 밀리초 타임스탬프
    market: str  # 마켓 심볼
    side: str  # "BUY" 또는 "SELL"
    price: float  # 체결 가격
    volume: float  # 거래 수량
    amount: float  # 거래 금액 (KRW)
    reason: str  # 거래 사유 (BUY: 전략명, SELL: SellReason)
    realized_pnl: Optional[float] = None  # 실현 손익 (SELL만)
    entry_strategy: Optional[str] = None  # 진입 전략명 (SELL만)

    def __repr__(self) -> str:
        pnl_str = f", PnL={self.realized_pnl:.0f}" if self.realized_pnl is not None else ""
        return f"Trade({self.side} {self.market} @ {self.price:.2f}, vol={self.volume:.8f}, amt={self.amount:.0f}{pnl_str})"


@dataclass
class BacktestResult:
    """백테스트 결과"""

    start_time: datetime
    end_time: datetime
    initial_balance: float
    final_balance: float
    total_return: float  # 절대 수익
    total_return_percent: float  # 수익률 (%)
    total_trades: int  # 총 거래 수
    buy_count: int  # 매수 횟수
    sell_count: int  # 매도 횟수
    trades: List[Trade] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        """승률 계산 (%)"""
        sell_trades = [t for t in self.trades if t.side == "SELL" and t.realized_pnl is not None]
        if not sell_trades:
            return 0.0
        wins = sum(1 for t in sell_trades if t.realized_pnl > 0)
        return (wins / len(sell_trades)) * 100.0

    @property
    def stop_loss_count(self) -> int:
        """손절 횟수"""
        return sum(1 for t in self.trades if t.reason == "stop_loss")

    @property
    def trailing_early_count(self) -> int:
        """EARLY 트레일링 스탑 횟수"""
        return sum(1 for t in self.trades if t.reason == "trailing_stop_early")

    @property
    def trailing_anca_count(self) -> int:
        """ANCA 트레일링 스탑 횟수"""
        return sum(1 for t in self.trades if t.reason == "trailing_stop_anca")

    @property
    def period_end_count(self) -> int:
        """기간 종료 매도 횟수"""
        return sum(1 for t in self.trades if t.reason == "period_end")

    @property
    def anca_exit_count(self) -> int:
        """ANCA exit 조건 매도 횟수"""
        return sum(1 for t in self.trades if t.reason == "anca_exit_condition")

    @property
    def anca_drop_count(self) -> int:
        """ANCA drop 조건 매도 횟수"""
        return sum(1 for t in self.trades if t.reason == "anca_drop_condition")

    # 매도 사유별 수익률 합계
    @property
    def stop_loss_profit(self) -> float:
        """손절 수익률 합계 (%)"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "stop_loss" and t.realized_pnl is not None)

    @property
    def trailing_early_profit(self) -> float:
        """EARLY 트레일링 스탑 수익률 합계 (%)"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "trailing_stop_early" and t.realized_pnl is not None)

    @property
    def trailing_anca_profit(self) -> float:
        """ANCA 트레일링 스탑 수익률 합계 (%) - deprecated"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "trailing_stop_anca" and t.realized_pnl is not None)

    @property
    def anca_exit_profit(self) -> float:
        """ANCA exit 조건 수익률 합계 (%)"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "anca_exit_condition" and t.realized_pnl is not None)

    @property
    def anca_drop_profit(self) -> float:
        """ANCA drop 조건 수익률 합계 (%)"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "anca_drop_condition" and t.realized_pnl is not None)

    @property
    def period_end_profit(self) -> float:
        """기간 종료 수익률 합계 (%)"""
        return sum(t.realized_pnl for t in self.trades if t.reason == "period_end" and t.realized_pnl is not None)

    def profit_summary_by_reason(self) -> str:
        """매도 사유별 수익률 요약"""
        anca_total = self.anca_exit_profit + self.anca_drop_profit
        return (
            f"SL:{self.stop_loss_profit:+.1f}% "
            f"EARLY:{self.trailing_early_profit:+.1f}% "
            f"ANCA:{anca_total:+.1f}%(exit:{self.anca_exit_profit:+.1f}/drop:{self.anca_drop_profit:+.1f})"
        )

    def summary(self) -> str:
        """결과 요약 문자열"""
        return (
            f"BacktestResult(\n"
            f"  Period: {self.start_time.date()} ~ {self.end_time.date()}\n"
            f"  Return: {self.total_return_percent:.2f}%\n"
            f"  Trades: {self.total_trades} (Buy: {self.buy_count}, Sell: {self.sell_count})\n"
            f"  Win Rate: {self.win_rate:.1f}%\n"
            f"  Balance: {self.initial_balance:,.0f} -> {self.final_balance:,.0f}\n"
            f"  📤 매도: SL:{self.stop_loss_count} EARLY:{self.trailing_early_count} ANCA(exit:{self.anca_exit_count}/drop:{self.anca_drop_count})\n"
            f"  💰 수익: {self.profit_summary_by_reason()}\n"
            f")"
        )
