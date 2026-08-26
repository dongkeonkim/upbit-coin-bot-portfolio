"""모멘텀 전략 (Go strategy/momentum.go와 동일)"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..config.params import Params, BUY_STAGE1_ALT_TOP_N
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, BuyReason, SellReason
from .state_tracker import StrategyStateTracker
from .indicators import (
    OHLCData,
    calculate_rsi,
    calculate_bollinger_bands,
    calculate_atr,
    calculate_adx,
    calculate_market_slope,
)


@dataclass
class StageAnalysis:
    """각 Stage별 분석 결과"""
    # Stage 1
    stage1_coins_count: int = 0  # 상승 조건 충족 코인 수
    stage1_required: int = 0  # 필요 코인 수
    stage1_passed: bool = False
    stage1_top_coins: List[Tuple[str, float]] = field(default_factory=list)  # [(market, change%), ...]

    # Stage 1 Alternative
    stage1_alt_coins_count: int = 0
    stage1_alt_required: int = 0
    stage1_alt_passed: bool = False
    stage1_alt_top_coins: List[Tuple[str, float]] = field(default_factory=list)  # 거래량 TOP N의 변화율

    # Stage 2
    stage2_coins_count: int = 0
    stage2_passed: bool = False
    stage2_candidates: List[Tuple[str, float]] = field(default_factory=list)  # [(market, change%), ...] - 통과한 코인
    stage2_all_candidates: List[Tuple[str, float, float, float, float]] = field(default_factory=list)  # [(market, 3분봉%, 1분봉%, open, close), ...] - 모든 Stage1 통과 코인
    stage2_required_pct: float = 0.0  # 필요 1분봉 변화율 (%)
    stage2_failures: List[Tuple[str, float, float]] = field(default_factory=list)  # [(market, 현재 1분봉%, 필요%), ...] - 탈락 사유

    # Stage 3
    stage3_coins_count: int = 0
    stage3_passed: bool = False
    stage3_candidates: List[Tuple[str, float, float]] = field(default_factory=list)  # [(market, change%, volume_ratio), ...]
    stage3_required_ratio: float = 0.0  # 필요 거래대금 배수
    stage3_failures: List[Tuple[str, float, float, float, float]] = field(default_factory=list)  # [(market, 현재거래대금, 평균, 필요, ratio), ...]

    # Stage 4 & 5
    best_coin: Optional[str] = None
    best_coin_change: float = 0.0
    best_coin_volume_ratio: float = 0.0
    tech_indicator_passed: bool = False
    tech_indicator_reason: str = ""

    # Near-miss (거의 통과할 뻔한 경우)
    near_miss_stage2: List[Tuple[str, float, float]] = field(default_factory=list)  # [(market, 현재%, 필요%), ...] - 0.5~0.65% 사이
    near_miss_stage3: List[Tuple[str, float, float]] = field(default_factory=list)  # [(market, ratio, 필요ratio), ...] - 1.5~1.9x 사이

    # 최종 결과
    final_passed: bool = False
    failure_stage: str = ""  # 실패한 단계


@dataclass
class PositionAnalysis:
    """포지션별 매도 조건 분석 결과"""
    market: str
    profit_percent: float  # 현재 수익률
    stop_loss_threshold: float  # 손절 기준선

    # 트레일링 스탑 상태
    peak_price: float = 0.0  # 최고가
    peak_profit: float = 0.0  # 최고 수익률
    profit_drop: float = 0.0  # 최고점 대비 하락률

    # Early 트레일링
    reached_early_target: bool = False  # Early 타겟 도달 여부
    early_target: float = 0.0  # Early 타겟 (%)
    early_drop: float = 0.0  # Early 고점대비 하락 (%)
    early_exit: float = 0.0  # Early 익절선 (%)

    # ANCA 트레일링
    reached_anca_target: bool = False  # ANCA 타겟 도달 여부
    anca_target: float = 0.0  # ANCA 타겟 (%)
    anca_exit: float = 0.0  # ANCA 익절선 (%)
    anca_drop: float = 0.0  # ANCA 하락폭 (%)

    # TOP10 상태
    days_outside_top10: int = 0


@dataclass
class Stage1Candidate:
    """Stage 1 후보 (Go stage1Candidate와 동일)"""

    market: str
    change_short: float
    close_short: float


@dataclass
class Stage2Candidate:
    """Stage 2 후보 (Go stage2Candidate와 동일)"""

    market: str
    change_short: float
    change_trade_check: float
    close_trade_check: float
    quote_volume_trade_check: float


@dataclass
class Stage3Candidate:
    """Stage 3 후보 (Go stage3Candidate와 동일)"""

    market: str
    change_short: float
    change_trade_check: float
    close_trade_check: float
    quote_volume_trade_check: float
    avg_quote_volume_1h: float
    volume_ratio: float


class MomentumStrategy:
    """모멘텀 전략 (Go MomentumStrategy와 동일)"""

    def __init__(self, params: Params, redis_portfolio=None):
        self.params = params

        # 캔들 데이터 (전략에서 사용)
        self.candles_1m: Dict[str, Candle] = {}
        self.candles_trade_check: Dict[str, Candle] = {}
        self.candles_short: Dict[str, Candle] = {}
        self.candles_long: Dict[str, Candle] = {}
        self.avg_quote_volumes: Dict[str, float] = {}

        # 전략 상태 추적기 (Redis 연동)
        self.state_tracker = StrategyStateTracker(params, redis_portfolio)

        # BTC 일봉 히스토리 (시장 필터용)
        self.btc_daily_history: List[float] = []
        self.current_date: str = ""

        # 기술 지표용 가격 히스토리 (코인별)
        self.price_history: Dict[str, List[float]] = {}  # RSI, BB 계산용 종가 히스토리
        self.ohlc_history: Dict[str, List[OHLCData]] = {}  # ATR, ADX 계산용 OHLC 히스토리

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        """캔들 데이터 업데이트 (Go UpdateCandles와 동일)"""
        self.candles_1m = candles_1m
        self.candles_trade_check = candles_trade_check
        self.candles_short = candles_short
        self.candles_long = candles_long
        self.avg_quote_volumes = avg_quote_volumes

        # 기술 지표용 가격 히스토리 업데이트
        self.update_price_history()

    def update_btc_history(self, current_time: datetime) -> None:
        """BTC 일봉 히스토리 업데이트 (Go UpdateBtcHistory와 동일)"""
        if not self.params.market_filter_enabled:
            return

        date_str = current_time.strftime("%Y-%m-%d")

        if date_str != self.current_date:
            btc_candle = self.candles_1m.get("KRW-BTC")
            if btc_candle is not None:
                self.btc_daily_history.append(btc_candle.close)

                # 최대 ma_days + 1개만 유지
                max_days = self.params.market_filter_ma_days + 1
                if len(self.btc_daily_history) > max_days:
                    self.btc_daily_history = self.btc_daily_history[-max_days:]

            self.current_date = date_str

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        """매수 신호 분석 (Go AnalyzeBuy와 동일)"""
        # 시장 필터 체크
        if self.params.market_filter_enabled:
            market_condition = self._check_market_condition()
            if market_condition == "bear":
                return None

        # 최소 잔고 체크 (10만원 이하일 때는 매수하지 않음)
        if portfolio.quote_balance < 100000.0:
            return None

        # Stage 1: 단기봉 상승 코인 스캔
        candidates_short = self._stage1_check()
        if len(candidates_short) == 0:
            return None

        # Stage 2: 매수 체크봉 상승 중 필터링
        candidates_trade_check = self._stage2_check(candidates_short)
        if len(candidates_trade_check) == 0:
            return None

        # Stage 3: 거래대금 필터링
        candidates_high_volume = self._stage3_check(candidates_trade_check)
        if len(candidates_high_volume) == 0:
            return None

        # Stage 4: 최대 거래대금 코인 선택
        best_coin = self._stage4_select(candidates_high_volume)

        # Stage 5: 기술 지표 체크
        ok, _ = self._check_technical_indicators(
            best_coin.market, best_coin.close_trade_check
        )
        if not ok:
            return None

        # 투자 금액 계산
        if self.params.dynamic_position_enabled:
            investment_amount = self._calculate_dynamic_position(
                portfolio.quote_balance, best_coin.change_short
            )
        else:
            investment_amount = portfolio.quote_balance * self.params.investment_ratio

        # 횡보장일 때 투자 금액 감소
        if self.params.market_filter_enabled:
            market_condition = self._check_market_condition()
            if market_condition == "sideways":
                investment_amount *= self.params.market_filter_sideways_reduction

        return Signal(
            action=SignalAction.BUY,
            market=best_coin.market,
            reason=BuyReason.MOMENTUM_SURGE,
            price=best_coin.close_trade_check,
            amount=investment_amount,
        )

    def get_stage_analysis(self) -> StageAnalysis:
        """각 Stage별 현재 상태 분석 (디버그용)"""
        analysis = StageAnalysis()

        # Stage 1 분석: 모든 코인의 변화율 수집
        all_changes: List[Tuple[str, float]] = []
        for market, candle in self.candles_short.items():
            change_percent = self._calculate_change_percent(candle.open, candle.close)
            all_changes.append((market, change_percent))

        # 변화율 기준 내림차순 정렬
        all_changes.sort(key=lambda x: x[1], reverse=True)
        analysis.stage1_top_coins = all_changes[:5]  # 상위 5개만

        # Stage 1 Original 체크
        candidates: List[Stage1Candidate] = []
        for market, candle in self.candles_short.items():
            change_percent = self._calculate_change_percent(candle.open, candle.close)
            if change_percent >= self.params.buy_stage1_change_pct:
                candidates.append(
                    Stage1Candidate(
                        market=market,
                        change_short=change_percent,
                        close_short=candle.close,
                    )
                )

        analysis.stage1_coins_count = len(candidates)
        analysis.stage1_required = self.params.buy_stage1_min_coins
        analysis.stage1_passed = len(candidates) >= self.params.buy_stage1_min_coins

        # Stage 1 Alternative 체크
        top_n_markets = self._get_top_n_markets(BUY_STAGE1_ALT_TOP_N)
        candidates_alt: List[Stage1Candidate] = []
        alt_top_changes: List[Tuple[str, float]] = []  # 거래량 TOP N의 전체 변화율
        for market in top_n_markets:
            candle = self.candles_short.get(market)
            if candle is None:
                continue
            change_percent = self._calculate_change_percent(candle.open, candle.close)
            alt_top_changes.append((market, change_percent))  # 모든 TOP N 코인 기록
            if change_percent >= self.params.buy_stage1_alt_change_pct:
                candidates_alt.append(
                    Stage1Candidate(
                        market=market,
                        change_short=change_percent,
                        close_short=candle.close,
                    )
                )

        # 변화율 기준 내림차순 정렬
        alt_top_changes.sort(key=lambda x: x[1], reverse=True)
        analysis.stage1_alt_top_coins = alt_top_changes[:5]  # 상위 5개만 로그용
        analysis.stage1_alt_coins_count = len(candidates_alt)
        analysis.stage1_alt_required = self.params.buy_stage1_alt_min_coins
        analysis.stage1_alt_passed = len(candidates_alt) >= self.params.buy_stage1_alt_min_coins

        # Stage 1 통과 여부
        if not analysis.stage1_passed and not analysis.stage1_alt_passed:
            analysis.failure_stage = "Stage1"
            return analysis

        # Stage 1 Alternative 통과 시 후보 병합
        original_markets = {c.market for c in candidates}
        if analysis.stage1_alt_passed:
            for alt_candidate in candidates_alt:
                if alt_candidate.market not in original_markets:
                    candidates.append(alt_candidate)

        # Stage 2 분석
        stage2_candidates: List[Stage2Candidate] = []
        analysis.stage2_required_pct = self.params.buy_stage2_change_pct
        near_miss_threshold_stage2 = self.params.buy_stage2_change_pct * 0.77  # 필요값의 77% 이상이면 near-miss (예: 0.65% * 0.77 = 0.5%)

        for candidate in candidates:
            candle = self.candles_trade_check.get(candidate.market)
            if candle is None:
                continue
            change_percent = self._calculate_change_percent(candle.open, candle.close)
            # 모든 Stage1 통과 코인의 1분봉 변동률 저장 (디버깅용, open/close 포함)
            analysis.stage2_all_candidates.append((candidate.market, candidate.change_short, change_percent, candle.open, candle.close))
            if change_percent >= self.params.buy_stage2_change_pct:
                stage2_candidates.append(
                    Stage2Candidate(
                        market=candidate.market,
                        change_short=candidate.change_short,
                        change_trade_check=change_percent,
                        close_trade_check=candle.close,
                        quote_volume_trade_check=candle.quote_volume,
                    )
                )
                analysis.stage2_candidates.append((candidate.market, change_percent))
            else:
                # Stage2 탈락 사유 기록
                analysis.stage2_failures.append((candidate.market, change_percent, self.params.buy_stage2_change_pct))
                # Near-miss 체크 (필요값의 77% 이상 ~ 미만)
                if change_percent >= near_miss_threshold_stage2:
                    analysis.near_miss_stage2.append((candidate.market, change_percent, self.params.buy_stage2_change_pct))

        analysis.stage2_coins_count = len(stage2_candidates)
        analysis.stage2_passed = len(stage2_candidates) > 0

        if not analysis.stage2_passed:
            analysis.failure_stage = "Stage2"
            return analysis

        # Stage 3 분석
        stage3_candidates: List[Stage3Candidate] = []
        analysis.stage3_required_ratio = self.params.buy_stage3_volume_surge
        near_miss_threshold_stage3 = self.params.buy_stage3_volume_surge * 0.79  # 필요값의 79% 이상이면 near-miss (예: 1.9 * 0.79 = 1.5)

        for candidate in stage2_candidates:
            avg_volume = self.avg_quote_volumes.get(candidate.market, 0.0)
            if avg_volume <= 0:
                continue
            required_volume = avg_volume * self.params.buy_stage3_volume_surge
            volume_ratio = candidate.quote_volume_trade_check / avg_volume if avg_volume > 0 else 0.0

            if candidate.quote_volume_trade_check >= required_volume:
                stage3_candidates.append(
                    Stage3Candidate(
                        market=candidate.market,
                        change_short=candidate.change_short,
                        change_trade_check=candidate.change_trade_check,
                        close_trade_check=candidate.close_trade_check,
                        quote_volume_trade_check=candidate.quote_volume_trade_check,
                        avg_quote_volume_1h=avg_volume,
                        volume_ratio=volume_ratio,
                    )
                )
                analysis.stage3_candidates.append(
                    (candidate.market, candidate.change_trade_check, volume_ratio)
                )
            else:
                # Stage3 탈락 사유 기록: (market, 현재거래대금, 평균거래대금, 필요거래대금, ratio)
                analysis.stage3_failures.append((
                    candidate.market,
                    candidate.quote_volume_trade_check,
                    avg_volume,
                    required_volume,
                    volume_ratio,
                ))
                # Near-miss 체크 (필요값의 79% 이상 ~ 미만)
                if volume_ratio >= near_miss_threshold_stage3:
                    analysis.near_miss_stage3.append((candidate.market, volume_ratio, self.params.buy_stage3_volume_surge))

        analysis.stage3_coins_count = len(stage3_candidates)
        analysis.stage3_passed = len(stage3_candidates) > 0

        if not analysis.stage3_passed:
            analysis.failure_stage = "Stage3"
            return analysis

        # Stage 4: 최대 거래대금 코인 선택
        best_coin = self._stage4_select(stage3_candidates)
        analysis.best_coin = best_coin.market
        analysis.best_coin_change = best_coin.change_trade_check
        analysis.best_coin_volume_ratio = best_coin.volume_ratio

        # Stage 5: 기술 지표 체크
        ok, reason = self._check_technical_indicators(
            best_coin.market, best_coin.close_trade_check
        )
        analysis.tech_indicator_passed = ok
        analysis.tech_indicator_reason = reason

        if not ok:
            analysis.failure_stage = "Stage5_TechIndicator"
            return analysis

        analysis.final_passed = True
        return analysis

    def get_position_analysis(self, portfolio: PortfolioState) -> List[PositionAnalysis]:
        """각 포지션의 매도 조건 분석 (디버그용)"""
        results: List[PositionAnalysis] = []

        for market, position in portfolio.positions.items():
            candle_1m = self.candles_1m.get(market)
            if candle_1m is None:
                continue

            current_price = candle_1m.close
            profit_percent = portfolio.get_profit_percent(market, current_price)

            # 손절 기준선 결정
            if self.params.improved_stop_loss_enabled:
                market_condition = self._check_market_condition()
                if market_condition == "bull":
                    stop_loss_threshold = self.params.improved_stop_loss_bull
                else:
                    stop_loss_threshold = self.params.improved_stop_loss_sideways
            else:
                stop_loss_threshold = self.params.stop_loss

            # 분석 객체 생성
            analysis = PositionAnalysis(
                market=market,
                profit_percent=profit_percent,
                stop_loss_threshold=stop_loss_threshold,
                early_target=self.params.trailing_target_early,
                early_drop=self.params.trailing_drop_early,
                early_exit=self.params.trailing_exit_early,
                anca_target=self.params.trailing_target,
                anca_exit=self.params.trailing_exit,
                anca_drop=self.params.trailing_drop,
            )

            # 트레일링 스탑 상태 조회
            state = self.state_tracker.get_state(market)
            if state is not None:
                analysis.peak_price = state.peak_price
                analysis.peak_profit = (
                    (state.peak_price - position.avg_buy_price) / position.avg_buy_price
                ) * 100
                analysis.profit_drop = analysis.peak_profit - profit_percent
                analysis.reached_early_target = state.reached_early_target
                analysis.reached_anca_target = state.reached_anca_target
                analysis.days_outside_top10 = state.days_outside_top10

            results.append(analysis)

        return results

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """매도 신호 분석 (Go AnalyzeSell과 동일)"""
        signals: List[Signal] = []

        for market, position in portfolio.positions.items():
            candle_1m = self.candles_1m.get(market)
            if candle_1m is None:
                continue

            current_price = candle_1m.close
            profit_percent = portfolio.get_profit_percent(market, current_price)

            # 1. 손절
            signal = self._check_stop_loss(
                market, profit_percent, current_price, position
            )
            if signal is not None:
                signals.append(signal)
                continue

            # 2. 트레일링 스탑
            signal = self._check_trailing_stop(
                market, profit_percent, current_price, position
            )
            if signal is not None:
                signals.append(signal)
                continue

            # 3. TOP10 이탈
            signal = self._check_top10_exit(market, profit_percent, current_price, position)
            if signal is not None:
                signals.append(signal)
                continue

        return signals

    def update_strategy_state(
        self,
        portfolio: PortfolioState,
        candles_1m: Dict[str, Candle],
        current_date: str,
        top10_markets: Dict[str, bool],
    ) -> None:
        """전략 상태 업데이트 (Go UpdateStrategyState와 동일)"""
        for market, position in portfolio.positions.items():
            candle = candles_1m.get(market)
            if candle is not None:
                # 트레일링 스탑 업데이트
                self.state_tracker.update_peak_price(
                    market, candle.high, position.avg_buy_price
                )

                # TOP10 순위 업데이트
                is_in_top10 = top10_markets.get(market, False)
                self.state_tracker.update_ranking(market, is_in_top10, current_date)

    def on_position_opened(self, market: str, price: float) -> None:
        """포지션 오픈 시 호출 (Go OnPositionOpened와 동일)"""
        self.state_tracker.track_position(market, price)

    def on_position_closed(self, market: str) -> None:
        """포지션 종료 시 호출 (Go OnPositionClosed와 동일)"""
        self.state_tracker.untrack_position(market)

    def on_partial_sell(self, market: str, reason: str) -> None:
        """
        부분 매도 체결 후 호출 (Go 시뮬레이터의 early_partial_sell 처리와 동일)

        Args:
            market: 마켓 심볼
            reason: 매도 사유
        """
        if reason == SellReason.EARLY_PARTIAL_SELL or reason == "early_partial_sell":
            self.state_tracker.set_early_partial_sold(market, True)

    # ===== Stage 체크 메서드들 =====

    def _stage1_check(self) -> List[Stage1Candidate]:
        """Stage 1 체크: 단기봉 상승 코인 스캔 (Go stage1Check와 동일)"""
        candidates: List[Stage1Candidate] = []

        # Stage 1 Original: 기본 조건
        for market, candle in self.candles_short.items():
            change_percent = self._calculate_change_percent(candle.open, candle.close)
            if change_percent >= self.params.buy_stage1_change_pct:
                candidates.append(
                    Stage1Candidate(
                        market=market,
                        change_short=change_percent,
                        close_short=candle.close,
                    )
                )

        stage1_pass_original = len(candidates) >= self.params.buy_stage1_min_coins

        # Stage 1 Alternative: TOP N 거래량 코인 중 대체 조건 체크
        stage1_pass_alternative = False
        original_markets = {c.market for c in candidates}

        # TOP N 거래량 마켓 가져오기
        top_n_markets = self._get_top_n_markets(BUY_STAGE1_ALT_TOP_N)
        candidates_alt: List[Stage1Candidate] = []

        for market in top_n_markets:
            candle = self.candles_short.get(market)
            if candle is None:
                continue

            change_percent = self._calculate_change_percent(candle.open, candle.close)
            if change_percent >= self.params.buy_stage1_alt_change_pct:
                candidates_alt.append(
                    Stage1Candidate(
                        market=market,
                        change_short=change_percent,
                        close_short=candle.close,
                    )
                )

        stage1_pass_alternative = (
            len(candidates_alt) >= self.params.buy_stage1_alt_min_coins
        )

        # Alternative 조건 통과 시, 원본에 없는 코인들을 추가
        if stage1_pass_alternative:
            for alt_candidate in candidates_alt:
                if alt_candidate.market not in original_markets:
                    candidates.append(alt_candidate)

        # 원본 조건 OR Alternative 조건 중 하나라도 통과해야 함
        if not stage1_pass_original and not stage1_pass_alternative:
            return []

        return candidates

    def _stage2_check(
        self, candidates: List[Stage1Candidate]
    ) -> List[Stage2Candidate]:
        """Stage 2 체크: 매수 체크봉 상승 중 필터링 (Go stage2Check와 동일)"""
        result: List[Stage2Candidate] = []

        for candidate in candidates:
            candle = self.candles_trade_check.get(candidate.market)
            if candle is None:
                continue

            change_percent = self._calculate_change_percent(candle.open, candle.close)
            if change_percent >= self.params.buy_stage2_change_pct:
                result.append(
                    Stage2Candidate(
                        market=candidate.market,
                        change_short=candidate.change_short,
                        change_trade_check=change_percent,
                        close_trade_check=candle.close,
                        quote_volume_trade_check=candle.quote_volume,
                    )
                )

        return result

    def _stage3_check(
        self, candidates: List[Stage2Candidate]
    ) -> List[Stage3Candidate]:
        """Stage 3 체크: 거래대금 필터링 (Go stage3Check와 동일)"""
        result: List[Stage3Candidate] = []

        for candidate in candidates:
            avg_volume = self.avg_quote_volumes.get(candidate.market, 0.0)
            if avg_volume <= 0:
                continue

            required_volume = avg_volume * self.params.buy_stage3_volume_surge
            if candidate.quote_volume_trade_check >= required_volume:
                volume_ratio = candidate.quote_volume_trade_check / avg_volume
                result.append(
                    Stage3Candidate(
                        market=candidate.market,
                        change_short=candidate.change_short,
                        change_trade_check=candidate.change_trade_check,
                        close_trade_check=candidate.close_trade_check,
                        quote_volume_trade_check=candidate.quote_volume_trade_check,
                        avg_quote_volume_1h=avg_volume,
                        volume_ratio=volume_ratio,
                    )
                )

        return result

    def _stage4_select(self, candidates: List[Stage3Candidate]) -> Stage3Candidate:
        """Stage 4: 최대 거래대금 코인 선택 (Go stage4Select와 동일)"""
        if len(candidates) == 0:
            raise ValueError("no candidates")

        best = candidates[0]
        for candidate in candidates[1:]:
            if candidate.quote_volume_trade_check > best.quote_volume_trade_check:
                best = candidate

        return best

    # ===== 매도 체크 메서드들 =====

    def _check_stop_loss(
        self, market: str, profit_percent: float, current_price: float, position
    ) -> Optional[Signal]:
        """손절 체크 (Go checkStopLoss와 동일)"""
        if self.params.improved_stop_loss_enabled:
            market_condition = self._check_market_condition()
            if market_condition == "bull":
                stop_loss_threshold = self.params.improved_stop_loss_bull
            else:
                stop_loss_threshold = self.params.improved_stop_loss_sideways
        else:
            stop_loss_threshold = self.params.stop_loss

        if profit_percent <= stop_loss_threshold:
            return Signal(
                action=SignalAction.SELL,
                market=market,
                reason=SellReason.STOP_LOSS,
                price=current_price,
                volume=position.volume,
                avg_buy_price=position.avg_buy_price,
                profit_percent=profit_percent,
            )

        return None

    def _check_trailing_stop(
        self, market: str, profit_percent: float, current_price: float, position
    ) -> Optional[Signal]:
        """트레일링 스탑 체크 (Go checkTrailingStop와 동일)"""
        state = self.state_tracker.get_state(market)
        if state is None:
            return None

        peak_profit = (
            (state.peak_price - position.avg_buy_price) / position.avg_buy_price
        ) * 100
        profit_drop = peak_profit - profit_percent

        # ANCA 트레일링 - 매도 이유 세분화
        if state.reached_anca_target:
            # 수익률이 exit 임계값 이하로 하락
            if profit_percent <= self.params.trailing_exit:
                return Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.ANCA_EXIT_CONDITION,
                    price=current_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_percent,
                )
            # 고점 대비 하락폭이 drop 임계값 이상
            if profit_drop >= self.params.trailing_drop:
                return Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.ANCA_DROP_CONDITION,
                    price=current_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_percent,
                )

        # EARLY 트레일링 - 부분 매도 지원
        if state.reached_early_target:
            # 부분 매도 활성화 시
            if self.params.partial_sell_enabled:
                # 1차 부분 매도: 아직 부분 매도 안 했고, drop 조건 충족
                if not state.early_partial_sold and profit_drop >= self.params.trailing_drop_early:
                    partial_volume = position.volume * self.params.early_partial_sell_ratio
                    return Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.EARLY_PARTIAL_SELL,
                        price=current_price,
                        volume=partial_volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    )

                # 이미 1차 부분 매도 완료 후: 방어선 이하로 하락 시 나머지 전량 매도
                if state.early_partial_sold:
                    if profit_percent <= self.params.early_defense_threshold:
                        return Signal(
                            action=SignalAction.SELL,
                            market=market,
                            reason=SellReason.EARLY_DEFENSE_SELL,
                            price=current_price,
                            volume=position.volume,
                            avg_buy_price=position.avg_buy_price,
                            profit_percent=profit_percent,
                        )
            else:
                # 부분 매도 비활성화: 기존 로직 (전량 매도)
                # 수익률이 exit 임계값 이하로 하락
                if profit_percent <= self.params.trailing_exit_early:
                    return Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.EARLY_EXIT_CONDITION,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    )
                # 고점 대비 하락폭이 drop 임계값 이상
                if profit_drop >= self.params.trailing_drop_early:
                    return Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.EARLY_DROP_CONDITION,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_percent,
                    )

        return None

    def _check_top10_exit(
        self, market: str, profit_percent: float, current_price: float, position
    ) -> Optional[Signal]:
        """TOP10 이탈 체크 (Go checkTop10Exit와 동일)"""
        state = self.state_tracker.get_state(market)
        if state is None:
            return None

        # 기본값 3일
        if state.days_outside_top10 >= 3:
            return Signal(
                action=SignalAction.SELL,
                market=market,
                reason=SellReason.VOLUME_RANK_EXIT,
                price=current_price,
                volume=position.volume,
                avg_buy_price=position.avg_buy_price,
                profit_percent=profit_percent,
            )

        return None

    # ===== 유틸리티 메서드들 =====

    def _calculate_change_percent(self, open_price: float, close_price: float) -> float:
        """변화율 계산 (%) (Go calculateChangePercent와 동일)"""
        if open_price == 0:
            return 0.0
        return ((close_price - open_price) / open_price) * 100.0

    def _calculate_dynamic_position(
        self, quote_balance: float, change_short: float
    ) -> float:
        """동적 포지션 크기 계산 (Go calculateDynamicPosition와 동일)"""
        if change_short >= self.params.dynamic_position_strong_threshold:
            return quote_balance * self.params.dynamic_position_strong_ratio
        elif change_short >= self.params.dynamic_position_medium_threshold:
            return quote_balance * self.params.dynamic_position_medium_ratio
        else:
            return quote_balance * self.params.dynamic_position_weak_ratio

    def _get_top_n_markets(self, n: int) -> List[str]:
        """거래량 상위 N개 마켓 반환 (Go getTopNMarkets와 동일)"""
        # (market, volume) 리스트 생성
        market_volumes = [
            (market, volume) for market, volume in self.avg_quote_volumes.items()
        ]

        # 거래량 기준 내림차순 정렬
        market_volumes.sort(key=lambda x: x[1], reverse=True)

        # 상위 N개 마켓 추출
        top_n = min(n, len(market_volumes))
        return [mv[0] for mv in market_volumes[:top_n]]

    def _check_market_condition(self) -> str:
        """시장 상태 체크 (Go checkMarketCondition와 동일)"""
        if not self.params.market_filter_enabled:
            return "bull"

        # BTC 일봉 히스토리가 충분하지 않으면 bear 반환 (보수적 접근)
        if len(self.btc_daily_history) < self.params.market_filter_ma_days:
            return "bear"

        # BTC 이동평균 기울기 계산
        slope = calculate_market_slope(
            self.btc_daily_history, self.params.market_filter_ma_days
        )

        if slope < self.params.market_filter_bear_threshold:
            return "bear"
        elif slope > self.params.market_filter_bull_threshold:
            return "bull"

        return "sideways"

    # ===== 기술 지표 메서드들 =====

    def update_price_history(self) -> None:
        """가격 히스토리 업데이트 (1분봉 기준) (Go UpdatePriceHistory와 동일)"""
        # 지표가 하나도 활성화되지 않으면 스킵
        if (
            not self.params.rsi_enabled
            and not self.params.bb_enabled
            and not self.params.atr_enabled
            and not self.params.adx_enabled
        ):
            return

        # 최대 필요 기간 계산
        max_period = 30
        if self.params.rsi_period > max_period:
            max_period = self.params.rsi_period
        if self.params.bb_period > max_period:
            max_period = self.params.bb_period
        if self.params.atr_period > max_period:
            max_period = self.params.atr_period
        if self.params.adx_period > max_period:
            max_period = self.params.adx_period

        for market, candle in self.candles_1m.items():
            # 종가 히스토리 (RSI, BB용)
            if self.params.rsi_enabled or self.params.bb_enabled:
                if market not in self.price_history:
                    self.price_history[market] = []
                self.price_history[market].append(candle.close)
                if len(self.price_history[market]) > max_period + 1:
                    self.price_history[market] = self.price_history[market][
                        -(max_period + 1) :
                    ]

            # OHLC 히스토리 (ATR, ADX용)
            if self.params.atr_enabled or self.params.adx_enabled:
                if market not in self.ohlc_history:
                    self.ohlc_history[market] = []
                self.ohlc_history[market].append(
                    OHLCData(high=candle.high, low=candle.low, close=candle.close)
                )
                if len(self.ohlc_history[market]) > max_period + 1:
                    self.ohlc_history[market] = self.ohlc_history[market][
                        -(max_period + 1) :
                    ]

    def _check_technical_indicators(
        self, market: str, current_price: float
    ) -> Tuple[bool, str]:
        """기술 지표 체크 (매수 필터) (Go checkTechnicalIndicators와 동일)"""
        # RSI 체크
        if self.params.rsi_enabled:
            prices = self.price_history.get(market, [])
            rsi = calculate_rsi(prices, self.params.rsi_period)
            # 과매수 상태면 매수 보류
            if rsi >= self.params.rsi_overbought:
                return False, "RSI 과매수"

        # 볼린저 밴드 체크
        if self.params.bb_enabled:
            prices = self.price_history.get(market, [])
            bb = calculate_bollinger_bands(
                prices, self.params.bb_period, self.params.bb_std_dev
            )
            if bb is not None:
                # 상단 밴드 돌파 시 매수 보류 (과열)
                if current_price >= bb.upper:
                    return False, "BB 상단 돌파"

        # ATR 체크 (변동성 기반 필터)
        if self.params.atr_enabled:
            ohlc = self.ohlc_history.get(market, [])
            atr = calculate_atr(ohlc, self.params.atr_period)
            if atr > 0:
                # 현재 변동성이 ATR의 2배 이상이면 과열
                candle = self.candles_1m.get(market)
                if candle is not None:
                    current_range = candle.high - candle.low
                    if current_range > atr * 2:
                        return False, "ATR 과열"

        # ADX 체크 (추세 강도)
        if self.params.adx_enabled:
            ohlc = self.ohlc_history.get(market, [])
            adx_result = calculate_adx(ohlc, self.params.adx_period)
            if adx_result is not None:
                # ADX가 임계값 미만이면 횡보장 (추세 없음)
                if adx_result.adx < self.params.adx_threshold:
                    return False, "ADX 횡보장"
                # 하락 추세면 매수 보류
                if adx_result.minus_di > adx_result.plus_di:
                    return False, "ADX 하락추세"

        return True, ""
