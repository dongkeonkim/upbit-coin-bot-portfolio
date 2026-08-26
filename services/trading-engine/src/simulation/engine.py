"""백테스트 엔진 (Go simulation/engine.go와 동일)"""

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Protocol, TYPE_CHECKING

# 슬리피지 (backtests/ml_optimize_per_coin.py와 동일)
ENTRY_SLIPPAGE = 0.0015  # 매수 시 0.15% 불리하게
EXIT_SLIPPAGE = 0.0005   # 매도 시 0.05% 불리하게

from ..config.params import Params, FEE_RATE
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, SellReason
from ..models.backtest_result import Trade, BacktestResult
from .time_simulator import TimeSimulator

if TYPE_CHECKING:
    from ..reporter.logger import Logger


class Strategy(Protocol):
    """전략 인터페이스 (Go Strategy와 동일)"""

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        ...

    def update_btc_history(self, current_time: datetime) -> None:
        ...

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        ...

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        ...

    def update_strategy_state(
        self,
        portfolio: PortfolioState,
        candles_1m: Dict[str, Candle],
        current_date: str,
        top10_markets: Dict[str, bool],
    ) -> None:
        ...

    def on_position_opened(self, market: str, price: float) -> None:
        ...

    def on_position_closed(self, market: str) -> None:
        ...

    def on_partial_sell(self, market: str, reason: str) -> None:
        ...


class BacktestEngine:
    """백테스트 엔진 (Go BacktestEngine와 동일)"""

    def __init__(self, strategy: Strategy, initial_balance: float, logger: Optional["Logger"] = None):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.trades: List[Trade] = []
        self.logger = logger
        # 실전과 동일: 시그널 발생(bar i) → 다음 봉 시가(bar i+1)에 체결
        self.pending_buy_signals: List[Signal] = []

    def run_with_data(
        self,
        markets: List[str],
        candles_1m: Dict[str, List[Candle]],
        start_time: datetime,
        end_time: datetime,
        params: Params,
    ) -> BacktestResult:
        """백테스트 실행 (사전 로드된 데이터 사용) (Go RunWithData와 동일)"""
        # 1. 시간 시뮬레이터 초기화
        time_sim = TimeSimulator(start_time, end_time, candles_1m, params)

        # 2. 포트폴리오 초기화
        portfolio = PortfolioState(self.initial_balance)

        # 3. 백테스트 루프 (1분마다 실행)
        last_rank_check_date = ""
        top10_markets: Dict[str, bool] = {}

        while not time_sim.is_finished():
            current_ts = time_sim.get_current_timestamp_ms()

            # 현재 시간의 캔들 데이터 가져오기
            (
                candles_1m_now,
                candles_trade_check_now,
                candles_short_now,
                candles_long_now,
            ) = time_sim.get_candles_at_time(current_ts)

            # 평균 거래대금 계산 (전략에서 필요한 마켓만 계산)
            avg_quote_volumes: Dict[str, float] = {}
            for market in portfolio.positions:
                if market in candles_1m_now:
                    avg_quote_volumes[market] = time_sim.get_avg_quote_volume(market)
            for market in top10_markets:
                if market in candles_1m_now and market not in avg_quote_volumes:
                    avg_quote_volumes[market] = time_sim.get_avg_quote_volume(market)

            # 전략에 캔들 데이터 업데이트
            self.strategy.update_candles(
                candles_1m_now,
                candles_trade_check_now,
                candles_short_now,
                candles_long_now,
                avg_quote_volumes,
            )

            # 현재 시간
            current_time = time_sim.get_current_time()

            # BTC 일봉 히스토리 업데이트 (시장 필터용)
            self.strategy.update_btc_history(current_time)

            # 현재 가격 (포트폴리오 평가용)
            current_prices = {
                market: candle.close for market, candle in candles_1m_now.items()
            }

            # 전략 상태 업데이트 (트레일링 스탑, TOP10 순위 등)
            current_date = current_time.strftime("%Y-%m-%d")
            if last_rank_check_date != current_date:
                # 날짜 변경 시 진행 상황 출력
                trade_count = len(self.trades)
                sell_count = sum(1 for t in self.trades if t.side == "SELL")
                # 포트폴리오 총 가치 계산 (잔고 + 포지션 평가액)
                total_value = portfolio.quote_balance
                for market, pos in portfolio.positions.items():
                    if market in current_prices:
                        total_value += pos.volume * current_prices[market]
                pnl_pct = (total_value / self.initial_balance - 1) * 100
                print(f"   📅 {current_date} | Trades: {trade_count} (Sells: {sell_count}) | PnL: {pnl_pct:+.1f}%", flush=True)

                # 코인별 통계 출력
                coin_stats: Dict[str, Dict] = defaultdict(lambda: {"sells": 0, "pnl": 0.0})
                for t in self.trades:
                    if t.side == "SELL" and t.realized_pnl is not None:
                        coin_stats[t.market]["sells"] += 1
                        coin_stats[t.market]["pnl"] += t.realized_pnl

                if coin_stats:
                    # PnL 순으로 정렬
                    sorted_coins = sorted(coin_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
                    coin_summary = " | ".join([
                        f"{m.replace('KRW-', '')}:{s['sells']}건/{s['pnl']/1000:+.1f}K"
                        for m, s in sorted_coins
                    ])
                    print(f"      {coin_summary}", flush=True)

                last_rank_check_date = current_date

            # 매분 TOP 10 갱신 (24시간 누적 거래대금 기준, Go와 동일)
            top10_markets = time_sim.get_top10_markets()
            self.strategy.update_strategy_state(
                portfolio, candles_1m_now, current_date, top10_markets
            )

            # 1. 대기 중인 매수 시그널 체결 (현재 봉 시가에 체결 - 실전과 동일)
            # 이전 봉에서 발생한 시그널을 현재 봉 시가에 체결
            if self.pending_buy_signals:
                for pending_signal in self.pending_buy_signals:
                    if pending_signal.market in candles_1m_now:
                        # 현재 봉 시가로 체결
                        open_price = candles_1m_now[pending_signal.market].open
                        self._execute_buy_at_price(pending_signal, portfolio, current_ts, open_price)
                self.pending_buy_signals = []

            # 2. 매도 신호 분석 (체결된 포지션 TP/SL 체크)
            sell_signals = self.strategy.analyze_sell(portfolio)
            for signal in sell_signals:
                self._execute_sell(signal, portfolio, current_ts)

            # 3. 새로운 매수 신호 분석 → 대기열에 추가 (다음 봉 시가에 체결)
            # 한 번에 하나의 매수 신호만 생성 (무한 루프 방지)
            buy_signal = self.strategy.analyze_buy(portfolio, current_time)
            if buy_signal is not None:
                self.pending_buy_signals.append(buy_signal)

            # 시간 진행 (1분)
            time_sim.advance(1)

        # 4. 결과 생성
        # 마지막 캔들 데이터로 현재 가격 설정
        current_prices = {}
        for market, candles in candles_1m.items():
            if len(candles) > 0:
                current_prices[market] = candles[-1].close

        final_value = portfolio.get_total_value(current_prices)
        total_return = final_value - self.initial_balance
        total_return_percent = (total_return / self.initial_balance) * 100

        buy_count = sum(1 for t in self.trades if t.side == "BUY")
        sell_count = sum(1 for t in self.trades if t.side == "SELL")

        return BacktestResult(
            start_time=start_time,
            end_time=end_time,
            initial_balance=self.initial_balance,
            final_balance=final_value,
            total_return=total_return,
            total_return_percent=total_return_percent,
            total_trades=len(self.trades),
            buy_count=buy_count,
            sell_count=sell_count,
            trades=self.trades,
        )

    def _execute_buy_at_price(
        self, signal: Signal, portfolio: PortfolioState, timestamp: int, base_price: float
    ) -> None:
        """매수 실행 - 지정된 가격(봉 시가)에 체결 (실전과 동일)"""
        if signal.amount is None:
            raise ValueError("buy signal amount is None")

        amount = signal.amount
        # 슬리피지 적용: 매수 시 더 높은 가격에 체결
        execution_price = base_price * (1 + ENTRY_SLIPPAGE)
        volume = amount / execution_price

        # 잔고 체크 (수수료 포함)
        total_cost = amount * (1 + FEE_RATE)
        if portfolio.quote_balance < total_cost:
            return  # 잔고 부족 시 스킵

        portfolio.add_position(signal.market, volume, signal.price, FEE_RATE)

        trade = Trade(
            timestamp=timestamp,
            market=signal.market,
            side="BUY",
            price=execution_price,
            volume=volume,
            amount=amount,
            reason=signal.reason.value if hasattr(signal.reason, "value") else str(signal.reason),
        )

        self.trades.append(trade)

        # 로거에 거래 기록
        if self.logger:
            self.logger.print_trade({
                "timestamp": timestamp,
                "market": signal.market,
                "side": "BUY",
                "price": execution_price,
                "volume": volume,
                "amount": amount,
                "reason": signal.reason.value if hasattr(signal.reason, "value") else str(signal.reason),
            })

        self.strategy.on_position_opened(signal.market, execution_price)

    def _execute_buy(
        self, signal: Signal, portfolio: PortfolioState, timestamp: int
    ) -> None:
        """매수 실행 (Go executeBuy와 동일) - 기존 호환용"""
        self._execute_buy_at_price(signal, portfolio, timestamp, signal.price)

    def _execute_sell(
        self, signal: Signal, portfolio: PortfolioState, timestamp: int
    ) -> None:
        """매도 실행 (Go executeSell와 동일)"""
        position = portfolio.positions.get(signal.market)
        if position is None:
            raise ValueError(f"no position for {signal.market}")

        volume = signal.volume if signal.volume is not None else position.volume

        # 슬리피지 적용: 매도 시 더 낮은 가격에 체결
        execution_price = signal.price * (1 - EXIT_SLIPPAGE)

        realized_pnl = portfolio.remove_position(
            signal.market, volume, execution_price, FEE_RATE
        )

        # 진입 전략명 찾기: 해당 마켓의 가장 최근 BUY 거래
        entry_strategy = None
        for t in reversed(self.trades):
            if t.market == signal.market and t.side == "BUY":
                entry_strategy = t.reason
                break

        amount = volume * execution_price
        trade = Trade(
            timestamp=timestamp,
            market=signal.market,
            side="SELL",
            price=execution_price,
            volume=volume,
            amount=amount,
            reason=signal.reason.value if hasattr(signal.reason, "value") else str(signal.reason),
            realized_pnl=realized_pnl,
            entry_strategy=entry_strategy,
        )

        self.trades.append(trade)

        # 로거에 거래 기록
        if self.logger:
            self.logger.print_trade({
                "timestamp": timestamp,
                "market": signal.market,
                "side": "SELL",
                "price": execution_price,
                "volume": volume,
                "amount": amount,
                "reason": signal.reason.value if hasattr(signal.reason, "value") else str(signal.reason),
                "realized_pnl": realized_pnl,
            })

        # 부분 매도 콜백 호출 (Go executeSell와 동일)
        reason_str = signal.reason.value if hasattr(signal.reason, "value") else str(signal.reason)
        if reason_str == SellReason.EARLY_PARTIAL_SELL.value:
            self.strategy.on_partial_sell(signal.market, reason_str)

        # 포지션이 완전히 종료되었는지 확인
        remaining_pos = portfolio.positions.get(signal.market)
        if remaining_pos is None or remaining_pos.volume < 1e-8:
            self.strategy.on_position_closed(signal.market)

    # _get_top10_markets 메서드는 TimeSimulator.get_top10_markets()로 대체됨 (Go와 동일)
