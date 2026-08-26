"""실시간 트레이딩 루프"""

import asyncio
import json
import signal
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Union

import redis

from ..clients.slack_client import SlackClient
from ..config.params import Params, LONG_TIMEFRAME_MINUTES
from ..config.settings import Settings
from ..models.position import PortfolioState
from ..models.signal import SellReason
from ..strategy.drop_bounce import DropBounceStrategy
from ..utils import get_logger, trading_logger
from .candle_buffer import CandleBuffer
from .redis_publisher import RedisPublisher
from .redis_portfolio import RedisPortfolio
from .redis_candle_reader import RedisCandleReader

# 구조화된 로거
logger = get_logger(__name__)

# 상수
MAX_MARKETS = 20  # 최대 모니터링 마켓 수
MAX_HOLDINGS = 10  # 최대 보유 코인 수
TOP_N_MARKETS = 5  # Top N 거래대금 마켓
MIN_LISTING_DAYS = 90  # 최소 상장일 (신규 코인 필터링)
MARKET_METADATA_KEY = "market:metadata"  # market-data-service가 저장하는 메타데이터
STALE_DATA_THRESHOLD_MS = 30 * 60 * 1000  # 데이터 신선도 임계값 (30분, candle_time 기준)


class TradingLoop:
    """
    실시간 트레이딩 루프

    1분마다 실행되며:
    1. Upbit API에서 최신 1분봉 조회
    2. CandleBuffer 업데이트
    3. MultiStrategy로 매수/매도 분석 (strategies_combined.json 기반)
    4. Redis Pub/Sub으로 신호 발행
    """

    def __init__(
        self,
        params: Params,
        settings: Settings,
        initial_balance: float = 1_000_000.0,
        markets: Optional[List[str]] = None,
        live_mode: bool = False,
        user_id: str = "default_user",
        strategy_type: str = "drop-bounce",
    ):
        """
        트레이딩 루프 초기화

        Args:
            params: 전략 파라미터 (trailing_target_early, trailing_exit_early, stop_loss 포함)
            settings: 환경 설정
            initial_balance: 초기 잔고 (시뮬레이션용, live_mode=False일 때만 사용)
            markets: 모니터링할 마켓 목록 (None이면 전체 KRW 마켓)
            live_mode: True면 Redis에서 실제 포트폴리오 읽기
            user_id: 사용자 ID (live_mode=True일 때 사용)
            strategy_type: 전략 타입 (drop-bounce, multi)
        """
        self.strategy_type = strategy_type
        self.params = params
        self.settings = settings
        self.initial_balance = initial_balance
        self._markets = markets
        self.live_mode = live_mode
        self.user_id = user_id

        # 웹소켓 모니터 (실시간 트레일링 스톱)
        self._ws_monitor = None
        self._ws_thread = None
        self._ws_loop = None  # 웹소켓 스레드의 이벤트 루프

        # 컴포넌트 초기화
        self.buffer = CandleBuffer(params)
        self.publisher = RedisPublisher(
            host=settings.redis_host,
            port=settings.redis_port,
            user_id=user_id,  # trade-service 연동용 user_id 전달
        )

        # Redis 캔들 리더 (market-data-service 연동)
        self.candle_reader = RedisCandleReader(
            host=settings.redis_host,
            port=settings.redis_port,
            user_id=user_id,
        )

        # Slack 알림 클라이언트
        self.slack = SlackClient(
            webhook_url=settings.slack_webhook_url,
            service_name="trading-engine",
        )

        # Redis 포트폴리오 (모의투자/실거래 모두 사용)
        # - 포트폴리오 조회
        # - 트레일링 상태 동기화
        self.redis_portfolio = RedisPortfolio(
            host=settings.redis_host,
            port=settings.redis_port,
            user_id=user_id,
        )

        # 전략 초기화
        if strategy_type == "drop-bounce":
            self.strategy = DropBounceStrategy(params, self.redis_portfolio)
            logger.info("strategy_initialized", strategy="DropBounceStrategy")
        elif strategy_type == "ml":
            from ..strategy.ml_strategy import MLStrategy
            self.strategy = MLStrategy(params, self.redis_portfolio, live_mode=live_mode)
            logger.info("strategy_initialized", strategy="MLStrategy (코인별 모델, 25개 피처)", live_mode=live_mode)
        else:
            # MultiStrategy는 pandas/pandas_ta 의존성이 있어 lazy import
            from ..strategy.multi_strategy import MultiStrategy
            self.strategy = MultiStrategy()
            logger.info("strategy_initialized", strategy="MultiStrategy")

        # 상태
        self._running = False
        self._initialized = False
        self._top10_markets: Set[str] = set()
        self._current_date: str = ""
        self._valid_markets: Set[str] = set()  # 신규 코인 필터링된 유효 마켓

        # 허용된 마켓 목록 (설정되면 이 마켓들만 거래)
        self._allowed_markets: Set[str] = set()
        if settings.allowed_markets:
            self._allowed_markets = set(
                m.strip().upper() for m in settings.allowed_markets.split(",") if m.strip()
            )
            logger.info(
                "allowed_markets_configured",
                markets=list(self._allowed_markets),
                count=len(self._allowed_markets),
            )

        # Redis 연결 (캐시용)
        self._redis: Optional[redis.Redis] = None

        # Leader Election 지원
        self._leader_stop_event: Optional[threading.Event] = None
        self._ha_mode: bool = False  # HA 모드 여부 (signal handler 등록 제어)

    def _connect_redis_cache(self) -> None:
        """Redis 캐시 연결"""
        if self._redis is None:
            self._redis = redis.Redis(
                host=self.settings.redis_host,
                port=self.settings.redis_port,
                decode_responses=True,
            )
            self._redis.ping()

    def _init_valid_markets(self) -> None:
        """
        유효 마켓 초기화 (신규 코인 필터링)

        market-data-service가 Redis에 저장한 메타데이터(market:metadata)를 읽어서
        상장일 기준으로 필터링

        메타데이터가 없으면 수집될 때까지 대기 (신규 코인 거래 방지)
        """
        # Redis에서 메타데이터 로드
        self._connect_redis_cache()

        max_wait_seconds = 120  # 최대 2분 대기
        poll_interval = 5  # 5초마다 확인
        waited = 0

        while waited < max_wait_seconds:
            try:
                metadata_raw = self._redis.get(MARKET_METADATA_KEY)
                if metadata_raw:
                    metadata: Dict[str, int] = json.loads(metadata_raw)

                    # 상장일 기준 필터링
                    valid_markets = {
                        market
                        for market, listing_days in metadata.items()
                        if listing_days >= MIN_LISTING_DAYS
                    }

                    self._valid_markets = valid_markets

                    filtered_count = len(metadata) - len(valid_markets)
                    logger.info(
                        "valid_markets_loaded",
                        total_markets=len(metadata),
                        valid_count=len(valid_markets),
                        filtered_count=filtered_count,
                        min_listing_days=MIN_LISTING_DAYS,
                        waited_seconds=waited,
                    )
                    trading_logger.markets_loaded(len(self._valid_markets), "redis_metadata")
                    return

                # 메타데이터 없음 - 대기
                if waited == 0:
                    logger.warning(
                        "waiting_for_market_metadata",
                        key=MARKET_METADATA_KEY,
                        max_wait_seconds=max_wait_seconds,
                        hint="market-data-service가 메타데이터를 수집 중입니다. 대기합니다.",
                    )

                time.sleep(poll_interval)
                waited += poll_interval

            except Exception as e:
                logger.error("failed_to_load_market_metadata", error=str(e), waited_seconds=waited)
                time.sleep(poll_interval)
                waited += poll_interval

        # 최대 대기 시간 초과
        logger.error(
            "market_metadata_timeout",
            max_wait_seconds=max_wait_seconds,
            hint="메타데이터 수집 대기 시간 초과. 빈 세트로 진행하면 신규 코인 거래 위험이 있습니다.",
        )
        trading_logger.markets_loaded(0, "metadata_timeout")

    def _wait_for_fresh_candles(self) -> None:
        """
        캔들 데이터가 준비될 때까지 대기

        market-data-service가 Redis에 신선한 캔들을 발행할 때까지 대기.
        stale 데이터로 시작하면 모든 마켓이 제외되어 거래가 불가능해짐.
        """
        max_wait_seconds = 120  # 최대 2분 대기
        poll_interval = 5  # 5초마다 확인
        waited = 0

        # 타겟 마켓 키에서 마켓 리스트 조회
        target_key = f"market:target:{self.user_id}"

        logger.info(
            "waiting_for_fresh_candles",
            max_wait_seconds=max_wait_seconds,
            hint="market-data-service가 캔들 데이터를 발행할 때까지 대기합니다.",
        )

        while waited < max_wait_seconds:
            try:
                # 타겟 마켓 조회
                target_data = self._redis.get(target_key)
                if not target_data:
                    logger.debug("target_markets_not_found", waited_seconds=waited)
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue

                markets = json.loads(target_data)
                if not markets:
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue

                # 첫 번째 마켓의 캔들 신선도 확인
                sample_market = markets[0]
                candle = self.candle_reader.get_latest_candle(sample_market)

                if candle:
                    now_ts = int(datetime.now().timestamp() * 1000)
                    candle_time_ms = candle.candle_time
                    delay_ms = now_ts - candle_time_ms
                    delay_sec = delay_ms / 1000

                    if delay_ms <= STALE_DATA_THRESHOLD_MS:
                        logger.info(
                            "fresh_candles_ready",
                            sample_market=sample_market,
                            delay_sec=round(delay_sec, 1),
                            waited_seconds=waited,
                        )
                        return

                    logger.debug(
                        "candles_still_stale",
                        sample_market=sample_market,
                        delay_sec=round(delay_sec, 1),
                        threshold_sec=STALE_DATA_THRESHOLD_MS / 1000,
                        waited_seconds=waited,
                    )
                else:
                    logger.debug("no_candle_found", sample_market=sample_market, waited_seconds=waited)

                time.sleep(poll_interval)
                waited += poll_interval

            except Exception as e:
                logger.error("failed_to_check_candle_freshness", error=str(e), waited_seconds=waited)
                time.sleep(poll_interval)
                waited += poll_interval

        # 최대 대기 시간 초과 - 경고만 하고 진행 (기존 로직이 stale 처리함)
        logger.warning(
            "fresh_candles_timeout",
            max_wait_seconds=max_wait_seconds,
            hint="캔들 데이터 대기 시간 초과. stale 데이터로 시작할 수 있습니다.",
        )

    def _load_initial_candles(self) -> None:
        """초기 캔들 데이터 로드 (Redis에서 읽기)"""
        logger.info(
            "candles_loading",
            market_count=len(self._markets),
        )

        # 장기 윈도우 크기만큼 데이터 필요
        candles_needed = LONG_TIMEFRAME_MINUTES + 10

        loaded_count = 0
        for i, market in enumerate(self._markets):
            try:
                # Redis에서 캔들 읽기 (market-data-service가 저장한 데이터)
                candles = self.candle_reader.get_candles(market, count=min(candles_needed, 500))

                if candles:
                    added = self.buffer.add_candles(candles)
                    loaded_count += 1
                else:
                    logger.warning("No candles in Redis", market=market)

                if (i + 1) % 10 == 0:
                    logger.debug(
                        f"Loaded {i + 1}/{len(self._markets)} markets",
                        loaded=i + 1,
                        total=len(self._markets),
                    )

            except Exception as e:
                logger.warning("Failed to load candles", market=market, error=str(e))

        logger.info(
            "candles_loaded",
            loaded_count=loaded_count,
            total_markets=len(self._markets),
        )

    def _update_candles(self) -> Dict[str, bool]:
        """
        최신 캔들 업데이트 (Redis에서 읽기)

        Returns:
            {market: updated} 딕셔너리
        """
        updated = {}
        for market in self._markets:
            try:
                # Redis에서 최근 2개 조회 (market-data-service가 저장한 데이터)
                candles = self.candle_reader.get_candles(market, count=2)

                for candle in candles:
                    if self.buffer.add_candle(candle):
                        updated[market] = True

            except Exception as e:
                logger.warning("Failed to update candle", market=market, error=str(e))
                updated[market] = False

        return updated

    def _check_data_freshness(self) -> bool:
        """
        데이터 신선도 검증 및 stale 마켓 필터링

        Redis 캔들 데이터가 STALE_DATA_THRESHOLD_MS(2분) 이상 지연되면 해당 마켓만 제외.
        거래량이 낮은 코인은 캔들이 업데이트되지 않을 수 있음.

        Returns:
            True: 거래 가능한 마켓이 있음
            False: 모든 마켓이 stale함 (에러)
        """
        now_ts = int(datetime.now().timestamp() * 1000)
        stale_markets = []
        fresh_markets = []

        for market in self._markets:
            candle = self.candle_reader.get_latest_candle(market)
            if candle:
                # candle_time 기준으로 stale 체크 (30분 이상 오래되면 stale)
                candle_time_ms = candle.candle_time
                delay_ms = now_ts - candle_time_ms
                if delay_ms > STALE_DATA_THRESHOLD_MS:
                    delay_sec = delay_ms / 1000
                    stale_markets.append((market, delay_sec))
                else:
                    fresh_markets.append(market)
            else:
                # 캔들이 없는 경우도 stale로 처리
                stale_markets.append((market, -1))

        if stale_markets:
            # 구조화된 로그 (10분 이상이면 error, 미만이면 warning)
            trading_logger.stale_data(stale_markets)

            # 10분(600초) 이상 지연된 마켓이 있으면 Slack 알림
            max_delay = max((d for _, d in stale_markets), default=0)
            if max_delay >= 600:
                self._send_stale_data_alert(stale_markets, critical=True)

            # stale 마켓 제외하고 계속 진행
            stale_market_names = [m for m, _ in stale_markets]
            self._markets = [m for m in self._markets if m not in stale_market_names]

            logger.info(
                "stale_markets_excluded",
                excluded=stale_market_names,
                remaining_count=len(self._markets),
            )

        # 모든 마켓이 stale인 경우에만 False
        if not self._markets:
            self._send_stale_data_alert(stale_markets)
            return False

        return True

    def _send_stale_data_alert(self, stale_markets: list, critical: bool = False) -> None:
        """
        Stale 데이터 알림 발송

        Args:
            stale_markets: [(market, delay_sec), ...] 리스트
            critical: True면 10분 이상 지연 (심각), False면 전체 마켓 stale (전략 중단)
        """
        max_delay = max((d for _, d in stale_markets), default=0)

        if critical:
            # 10분 이상 지연 - 심각한 경고
            status_msg = f"⚠️ 10분 이상 지연 (max: {max_delay:.0f}초)"
        else:
            # 전체 마켓 stale - 전략 중단
            status_msg = "⛔ 전략 실행 중단됨. market-data-service 상태 확인 필요."

        logger.warning(
            "stale_data_alert",
            mode="LIVE" if self.live_mode else "SIMULATION",
            user_id=self.user_id,
            stale_market_count=len(stale_markets),
            max_delay_sec=max_delay,
            critical=critical,
        )

        # Slack 알림 전송
        self.slack.send_stale_data_alert(
            stale_markets=stale_markets,
            mode="LIVE" if self.live_mode else "SIMULATION",
            user_id=self.user_id,
            status_msg=status_msg,
        )

    def _log_filtered_candles(self) -> None:
        """필터링된 마켓의 캔들 정보 로그 출력"""
        candles_data = []

        for market in self._markets:
            candle = self.candle_reader.get_latest_candle(market)
            if candle:
                candles_data.append({
                    "market": candle.market,
                    "timestamp": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": round(candle.volume, 8),
                    "quote_volume": round(candle.quote_volume, 2),
                    "interval": candle.timeframe,
                })

        if candles_data:
            # 구조화된 로그 (info: 요약, debug: 상세)
            logger.info(
                "filtered_candles",
                candle_count=len(candles_data),
                markets=[c["market"] for c in candles_data],
            )
            logger.debug(
                "filtered_candles_detail",
                candles=candles_data,
            )

    def _update_top10(self) -> None:
        """거래대금 Top 10 마켓 업데이트"""
        try:
            # 평균 거래대금 기준 정렬
            avg_volumes = self.buffer.get_avg_quote_volumes(self._markets)
            sorted_markets = sorted(
                avg_volumes.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            self._top10_markets = set(m for m, _ in sorted_markets[:10])
        except Exception as e:
            logger.warning("Failed to update top10", error=str(e))

    def _get_dynamic_markets(self) -> List[str]:
        """
        동적 마켓 리스트 조회 (Redis에서 읽기 + 신규 코인 필터링)

        market-data-service가 설정한 대상 마켓을 읽은 후,
        신규 코인(상장 90일 미만)을 필터링합니다.

        Returns:
            모니터링할 마켓 리스트 (Top 10 + 보유 코인)
        """
        # Redis에서 대상 마켓 읽기 (market-data-service가 설정)
        markets = self.candle_reader.get_target_markets()

        if not markets:
            logger.error(
                "no_markets_from_redis",
                hint="market-data-service가 Redis에 마켓 정보를 저장하지 않았을 수 있음",
            )
            return []

        logger.debug(
            "Target markets from market-data-service",
            markets=markets,
            count=len(markets),
        )

        # 신규 코인 필터링 적용
        if self._valid_markets:
            filtered_markets = [m for m in markets if m in self._valid_markets]
            filtered_count = len(markets) - len(filtered_markets)
            if filtered_count > 0:
                logger.info(
                    "new_coins_filtered",
                    filtered_count=filtered_count,
                    min_listing_days=MIN_LISTING_DAYS,
                )
        else:
            # 화이트리스트가 없으면 필터링 없이 진행
            filtered_markets = markets

        # ALLOWED_MARKETS 설정 시: 허용된 마켓만 투자 대상
        if self._allowed_markets:
            top_markets = [m for m in filtered_markets if m in self._allowed_markets]
            logger.info(
                "allowed_markets_selected",
                allowed=list(self._allowed_markets),
                matched=top_markets,
            )
        else:
            # ALLOWED 없으면 Top N 마켓 선택
            top_markets = filtered_markets[:TOP_N_MARKETS]

        # 보유 코인 별도 추가 (Top N에 없어도 모니터링 필요)
        held_markets: List[str] = []
        portfolio = self._get_portfolio()
        if portfolio and portfolio.positions:
            for market in portfolio.positions.keys():
                # 신규 코인 제외, Top N에 없는 것만 추가
                if market not in top_markets:
                    if not self._valid_markets or market in self._valid_markets:
                        held_markets.append(market)

        # 병합 (Top N + 보유 코인)
        final_markets = top_markets + held_markets

        logger.info(
            "markets_selected",
            top_count=len(top_markets),
            held_count=len(held_markets),
            total_count=len(final_markets),
        )

        return final_markets

    def _can_buy_more(self) -> bool:
        """
        추가 매수 가능 여부 확인 (최대 보유 코인 수 제한)

        Returns:
            매수 가능하면 True
        """
        portfolio = self._get_portfolio()
        current_holdings = len(portfolio.positions)

        if current_holdings >= MAX_HOLDINGS:
            logger.debug(
                "Max holdings reached",
                current_holdings=current_holdings,
                max_holdings=MAX_HOLDINGS,
            )
            return False
        return True

    def _get_portfolio(self) -> PortfolioState:
        """현재 포트폴리오 조회 (항상 Redis에서 조회)"""
        # 모의투자/실거래 모두 Redis 포트폴리오 사용
        # asset-service가 trade:completed 이벤트로 Redis 업데이트
        portfolio = self.redis_portfolio.get_portfolio()
        if portfolio is None:
            logger.warning("Failed to get portfolio from Redis, using empty portfolio")
            return PortfolioState(0)
        return portfolio

    def _run_strategy(self) -> None:
        """전략 실행"""
        current_time = datetime.now()

        # 포트폴리오 조회
        portfolio = self._get_portfolio()

        # 현재가 조회 (Slack 알림용)
        current_prices = self.buffer.get_current_prices()

        # 캔들 데이터 조회
        candles_1m, candles_trade_check, candles_short, candles_long = self.buffer.get_candles()
        avg_quote_volumes = self.buffer.get_avg_quote_volumes()

        # 전략 업데이트
        self.strategy.update_candles(
            candles_1m,
            candles_trade_check,
            candles_short,
            candles_long,
            avg_quote_volumes,
        )

        # 디버그: Stage 분석 출력 (MomentumStrategy 전용)
        if hasattr(self.strategy, 'get_stage_analysis'):
            self._print_stage_analysis()

        # 디버그: 포지션 매도 조건 출력 (MomentumStrategy 전용)
        if hasattr(self.strategy, 'get_position_analysis'):
            self._print_position_analysis()

        # 매수 신호 분석 (최대 보유 코인 수 확인)
        if self._can_buy_more():
            buy_signal = self.strategy.analyze_buy(portfolio, current_time)
            if buy_signal:
                # 신호 발행 (trade-service가 실제 주문 실행)
                # trade-service → trade:completed → asset-service → Redis 업데이트
                self.publisher.publish_buy_signal(buy_signal)

                # 포트폴리오 정보 계산 (매수 반영 후)
                portfolio_value = portfolio.get_total_value(current_prices)

                # Slack 알림 (포트폴리오 정보 포함)
                self.slack.send_buy_signal(
                    market=buy_signal.market,
                    price=buy_signal.price,
                    amount=buy_signal.amount,
                    reason=buy_signal.reason,
                    mode="LIVE" if self.live_mode else "SIMULATION",
                    portfolio_value=portfolio_value,
                )

                self.strategy.on_position_opened(buy_signal.market, buy_signal.price)

        # 매도 신호 분석
        sell_signals = self.strategy.analyze_sell(portfolio)
        for sell_signal in sell_signals:
            # 신호 발행 (trade-service가 실제 주문 실행)
            # trade-service → trade:completed → asset-service → Redis 업데이트
            self.publisher.publish_sell_signal(sell_signal)

            # 포트폴리오 정보 계산 (매도 반영 후)
            portfolio_value = portfolio.get_total_value(current_prices)

            # Slack 알림 (포트폴리오 정보 포함)
            self.slack.send_sell_signal(
                market=sell_signal.market,
                price=sell_signal.price,
                volume=sell_signal.volume,
                reason=sell_signal.reason,
                mode="LIVE" if self.live_mode else "SIMULATION",
                avg_buy_price=sell_signal.avg_buy_price,
                portfolio_value=portfolio_value,
            )

            # 부분 매도 vs 전량 매도 처리
            if sell_signal.reason == SellReason.EARLY_PARTIAL_SELL or sell_signal.reason == "early_partial_sell":
                # 부분 매도: 포지션 유지, 부분 매도 상태만 업데이트
                self.strategy.on_partial_sell(sell_signal.market, sell_signal.reason)
            else:
                # 전량 매도: 포지션 종료
                self.strategy.on_position_closed(sell_signal.market)

        # 전략 상태 업데이트
        current_date = current_time.strftime("%Y-%m-%d")
        top10_dict = {m: True for m in self._top10_markets}
        self.strategy.update_strategy_state(
            portfolio,
            candles_1m,
            current_date,
            top10_dict,
        )

        # 보유 코인에 대한 매수 신호 체크 (ML 전략 전용)
        if hasattr(self.strategy, 'check_holding_buy_signals'):
            self.strategy.check_holding_buy_signals(portfolio)

    def _publish_heartbeat(self) -> None:
        """하트비트 발행"""
        current_prices = self.buffer.get_current_prices()
        portfolio = self._get_portfolio()
        portfolio_value = portfolio.get_total_value(current_prices)

        self.publisher.publish_heartbeat(
            portfolio_value=portfolio_value,
            position_count=len(portfolio.positions),
            market_count=len(self._markets),
        )

    def _refresh_markets(self) -> None:
        """마켓 리스트 갱신 (Redis에서 읽기)"""
        logger.info("market_refresh_started")
        new_markets = self._get_dynamic_markets()

        if self._markets:
            # 제거된 마켓 버퍼에서 정리
            removed_markets = set(self._markets) - set(new_markets)
            if removed_markets:
                logger.info(
                    "markets_removed",
                    removed_count=len(removed_markets),
                    markets=list(removed_markets),
                )
                for market in removed_markets:
                    self.buffer.clear(market)

            # 새로 추가된 마켓의 캔들 로드 (Redis에서)
            added_markets = set(new_markets) - set(self._markets)
        else:
            added_markets = set(new_markets)

        if added_markets:
            logger.info(
                "markets_added",
                added_count=len(added_markets),
            )
            candles_needed = LONG_TIMEFRAME_MINUTES + 10
            for market in added_markets:
                try:
                    # Redis에서 캔들 읽기 (market-data-service가 저장한 데이터)
                    candles = self.candle_reader.get_candles(market, count=min(candles_needed, 500))
                    if candles:
                        self.buffer.add_candles(candles)
                    else:
                        logger.warning("No candles in Redis", market=market)
                except Exception as e:
                    logger.warning("Failed to load candles", market=market, error=str(e))

        self._markets = new_markets

    def _initialize(self) -> None:
        """
        트레이딩 루프 초기화 (연결, 데이터 로드 등)

        HA 모드에서는 Leader가 되었을 때 호출됨
        """
        logger.info(
            "trading_loop_starting",
            mode="LIVE" if self.live_mode else "SIMULATION",
            max_markets=MAX_MARKETS,
            max_holdings=MAX_HOLDINGS,
            min_listing_days=MIN_LISTING_DAYS,
            ha_mode=self._ha_mode,
        )

        # 연결
        self.publisher.connect()
        self.candle_reader.connect()  # Redis 캔들 리더 연결
        self.redis_portfolio.connect()  # 트레일링 상태 동기화에 필요

        # 신규 코인 필터링 초기화 (Redis 캐시 활용)
        # market-data-service가 이미 필터링했으므로 캐시만 확인
        # [임시 비활성화] 신규 코인 필터링 없이 수익률 테스트
        # self._init_valid_markets()

        # Redis 캐시 연결 (_wait_for_fresh_candles에서 사용)
        self._connect_redis_cache()

        # 캔들 데이터가 준비될 때까지 대기
        self._wait_for_fresh_candles()

        # 마켓 목록 조회 및 초기 캔들 로드 (Top 10 + 보유 마켓)
        self._refresh_markets()
        self._initialized = True

        # MLStrategy 초기 히스토리 로드 (Redis에서 전체 캔들 읽기)
        if hasattr(self.strategy, 'load_initial_history'):
            candles_by_market = self.candle_reader.get_all_candles(
                self._markets, count=500
            )
            self.strategy.load_initial_history(candles_by_market)
            logger.info(
                "ml_history_loaded",
                market_count=len(candles_by_market),
            )

        # 기존 포지션에 대한 전략 상태 초기화 (Redis에서 상태 복원)
        portfolio = self._get_portfolio()
        if portfolio and portfolio.positions:
            for market, position in portfolio.positions.items():
                self.strategy.on_position_opened(market, position.avg_buy_price)
                logger.info(
                    "existing_position_tracked",
                    market=market,
                    avg_buy_price=position.avg_buy_price,
                )

        self._running = True

        # 시그널 핸들러 설정 (HA 모드가 아닐 때만 - HA 모드에서는 LeaderElector가 관리)
        if not self._ha_mode:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(
            "trading_loop_started",
            strategy_timeframe=self.params.short_timeframe_minutes,
            market_count=len(self._markets),
            initial_balance=self.initial_balance,
        )

        # Slack 서비스 시작 알림
        self.slack.send_service_started(
            mode="LIVE" if self.live_mode else "SIMULATION",
            user_id=self.user_id,
            ha_mode=self._ha_mode,
            strategy=self.strategy_type,
        )

        # 웹소켓 모니터 시작 (실시간 트레일링 스톱)
        # Live/시뮬레이션 모두 활성화 (Redis 포트폴리오 사용)
        self._start_websocket_monitor()

    def _run_main_loop(self) -> None:
        """
        트레이딩 메인 루프 (분리된 메서드)

        HA 모드에서 Leader 상실 시 _leader_stop_event로 중지됨
        """
        iteration = 0
        last_minute = -1

        while self._running:
            # Leader Election 중지 신호 체크
            if self._leader_stop_event and self._leader_stop_event.is_set():
                logger.info("stop_event_received", reason="leader_election")
                break

            try:
                now = datetime.now()

                # 매 분 정각에 실행
                if now.minute != last_minute:
                    last_minute = now.minute
                    iteration += 1

                    # 이터레이션 로깅 (debug)
                    trading_logger.iteration(iteration, len(self._markets))

                    # 캔들 업데이트
                    updated = self._update_candles()
                    updated_count = sum(1 for v in updated.values() if v)
                    trading_logger.candles_updated(updated_count, len(self._markets))

                    # 필터링된 마켓 캔들 로그 출력
                    self._log_filtered_candles()

                    # 마켓 리스트 갱신 (freshness 체크 전에 수행하여 stale 복구 가능)
                    self._refresh_markets()

                    # 데이터 신선도 검증 (stale 데이터면 전략 실행 중단)
                    if not self._check_data_freshness():
                        logger.warning(
                            "strategy_stopped",
                            reason="stale_data",
                        )
                        # 포트폴리오 상태만 출력하고 다음 iteration으로
                        current_prices = self.buffer.get_current_prices()
                        portfolio = self._get_portfolio()
                        portfolio_value = portfolio.get_total_value(current_prices)
                        trading_logger.portfolio_update(
                            portfolio_value,
                            len(portfolio.positions),
                        )
                        continue

                    # Top 10 갱신
                    self._update_top10()

                    # 전략 실행
                    self._run_strategy()

                    # 하트비트 발행
                    self._publish_heartbeat()

                    # 포트폴리오 상태 로깅
                    current_prices = self.buffer.get_current_prices()
                    portfolio = self._get_portfolio()
                    portfolio_value = portfolio.get_total_value(current_prices)
                    trading_logger.portfolio_update(
                        portfolio_value,
                        len(portfolio.positions),
                    )

                # 다음 체크까지 대기
                time.sleep(1)

            except Exception as e:
                logger.error(
                    "trading_loop_error",
                    error=str(e),
                    exc_info=True,
                )
                time.sleep(5)

    def start(self) -> None:
        """
        트레이딩 루프 시작 (단독 실행 모드)

        HA 모드에서는 start_for_ha()를 사용
        """
        self._ha_mode = False
        self._initialize()
        self._run_main_loop()
        self._cleanup()

    def start_for_ha(self, stop_event: threading.Event) -> None:
        """
        트레이딩 루프 시작 (HA 모드)

        Leader Election에 의해 호출되며, stop_event가 set되면 종료

        Args:
            stop_event: Leader 상실 시 set되는 Event
        """
        self._ha_mode = True
        self._leader_stop_event = stop_event
        self._initialize()
        self._run_main_loop()
        self._cleanup()

    def stop(self) -> None:
        """트레이딩 루프 중지 (외부에서 호출)"""
        logger.info("trading_loop_stop_requested")
        self._running = False
        if self._leader_stop_event:
            self._leader_stop_event.set()

    def _signal_handler(self, signum, frame) -> None:
        """시그널 핸들러 (Ctrl+C)"""
        signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info(
            "trading_loop_stopping",
            signal=signum,
        )

        # Slack 서비스 종료 알림
        self.slack.send_service_stopping(reason=f"graceful shutdown ({signal_name})")

        self._running = False

    def _print_stage_analysis(self) -> None:
        """Stage별 분석 결과 출력"""
        analysis = self.strategy.get_stage_analysis()

        # 상위 코인 변화율 포맷팅 (변화율 TOP 5) - 3분봉 기준
        top_coins_str = ", ".join(
            f"{m}:{c:.2f}%" for m, c in analysis.stage1_top_coins
        ) if analysis.stage1_top_coins else "none"

        # 거래량 TOP 10의 변화율 포맷팅 (Alt 조건 대상) - 3분봉 기준
        alt_top_coins_str = ", ".join(
            f"{m}:{c:.2f}%" for m, c in analysis.stage1_alt_top_coins
        ) if analysis.stage1_alt_top_coins else "none"

        # Stage2 후보 (1분봉 변동률) 포맷팅 - 통과한 코인만
        stage2_candidates_str = ", ".join(
            f"{m}:{c:.2f}%" for m, c in analysis.stage2_candidates
        ) if analysis.stage2_candidates else "none"

        # Stage1 통과 코인들의 1분봉 변동률 (short_tf봉%, 1분봉%, open, close) - Stage2 탈락 원인 분석용
        short_tf = self.params.short_timeframe_minutes
        stage2_all_str = ", ".join(
            f"{m}({short_tf}m:{s:.2f}%,1m:{t:.2f}%,o:{o:.0f},c:{c:.0f})" for m, s, t, o, c in analysis.stage2_all_candidates
        ) if analysis.stage2_all_candidates else "none"

        # Stage2 실패 사유 포맷팅 (1분봉 현재값 vs 필요값)
        stage2_failures_str = ", ".join(
            f"{m}(1m:{cur:.2f}%<{req:.2f}%)" for m, cur, req in analysis.stage2_failures
        ) if analysis.stage2_failures else "none"

        # Stage3 후보 (거래량 비율) 포맷팅
        stage3_candidates_str = ", ".join(
            f"{m}:{c:.2f}%/vol:{v:.1f}x" for m, c, v in analysis.stage3_candidates
        ) if analysis.stage3_candidates else "none"

        # Stage3 실패 사유 포맷팅 (거래대금: 현재/평균/필요, ratio)
        stage3_failures_str = ", ".join(
            f"{m}(vol:{cur/1e6:.1f}M/avg:{avg/1e6:.1f}M/need:{need/1e6:.1f}M,ratio:{ratio:.2f}x<{analysis.stage3_required_ratio:.1f}x)"
            for m, cur, avg, need, ratio in analysis.stage3_failures
        ) if analysis.stage3_failures else "none"

        # Near-miss 포맷팅 (거의 통과할 뻔한 경우)
        near_miss_stage2_str = ", ".join(
            f"{m}(1m:{cur:.2f}%→need:{req:.2f}%)" for m, cur, req in analysis.near_miss_stage2
        ) if analysis.near_miss_stage2 else "none"

        near_miss_stage3_str = ", ".join(
            f"{m}(ratio:{ratio:.2f}x→need:{req:.1f}x)" for m, ratio, req in analysis.near_miss_stage3
        ) if analysis.near_miss_stage3 else "none"

        # 구조화된 스테이지 분석 로깅
        trading_logger.stage_analysis(
            market="ALL",
            stage1_passed=analysis.stage1_passed,
            stage2_passed=analysis.stage2_passed,
            stage3_passed=analysis.stage3_passed,
            all_passed=analysis.final_passed,
            stage1_coins_count=analysis.stage1_coins_count,
            stage1_required=analysis.stage1_required,
            stage1_alt_passed=analysis.stage1_alt_passed,
            stage1_alt_coins_count=analysis.stage1_alt_coins_count,
            stage1_alt_required=analysis.stage1_alt_required,
            stage2_coins_count=analysis.stage2_coins_count,
            stage2_candidates=stage2_candidates_str,
            stage2_all_candidates=stage2_all_str,
            stage2_required_pct=analysis.stage2_required_pct,
            stage2_failures=stage2_failures_str,
            stage3_coins_count=analysis.stage3_coins_count,
            stage3_candidates=stage3_candidates_str,
            stage3_required_ratio=analysis.stage3_required_ratio,
            stage3_failures=stage3_failures_str,
            failure_stage=analysis.failure_stage,
            best_coin=analysis.best_coin,
            best_coin_change=analysis.best_coin_change,
            best_coin_volume_ratio=analysis.best_coin_volume_ratio,
            tech_indicator_reason=analysis.tech_indicator_reason,
            top_coins=top_coins_str,
            alt_top_coins=alt_top_coins_str,
            near_miss_stage2=near_miss_stage2_str,
            near_miss_stage3=near_miss_stage3_str,
        )

    def _print_position_analysis(self) -> None:
        """포지션별 매도 조건 분석 출력 (디버그용)"""
        portfolio = self._get_portfolio()
        if not portfolio.positions:
            return

        positions = self.strategy.get_position_analysis(portfolio)
        if not positions:
            return

        # 구조화된 포지션 분석 로깅 (debug 레벨)
        positions_data = []
        for pos in positions:
            pos_data = {
                "market": pos.market,
                "profit_pct": pos.profit_percent,
                "stop_loss_threshold": pos.stop_loss_threshold,
                "peak_profit": pos.peak_profit,
                "reached_anca_target": pos.reached_anca_target,
                "reached_early_target": pos.reached_early_target,
                "days_outside_top10": pos.days_outside_top10,
            }
            if pos.reached_anca_target:
                pos_data["trailing_type"] = "anca"
                pos_data["profit_drop"] = pos.profit_drop
            elif pos.reached_early_target:
                pos_data["trailing_type"] = "early"
                pos_data["profit_drop"] = pos.profit_drop
                pos_data["early_drop"] = pos.early_drop
            else:
                pos_data["trailing_type"] = "waiting"
                pos_data["to_early_target"] = pos.early_target - pos.profit_percent
                pos_data["to_anca_target"] = pos.anca_target - pos.profit_percent
            positions_data.append(pos_data)

        logger.debug(
            "position_analysis",
            position_count=len(positions),
            positions=positions_data,
        )

    def _start_websocket_monitor(self) -> None:
        """웹소켓 모니터 시작 (별도 스레드)"""
        from .price_monitor import PriceMonitor

        logger.info(
            "websocket_monitor_starting",
            stop_loss=self.params.stop_loss,
            early_target=self.params.trailing_target_early,
            early_drop=self.params.trailing_drop_early,
            early_exit=self.params.trailing_exit_early,
            anca_target=self.params.trailing_target,
            anca_drop=self.params.trailing_drop,
        )

        self._ws_monitor = PriceMonitor(
            params=self.params,
            settings=self.settings,
            user_id=self.user_id,
            strategy_type=self.strategy_type,
            live_mode=self.live_mode,
        )

        def run_monitor():
            """별도 스레드에서 asyncio 이벤트 루프 실행"""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop  # 이벤트 루프 저장 (스레드-세이프 stop 호출용)
            try:
                loop.run_until_complete(self._ws_monitor.start())
            except Exception as e:
                logger.error("websocket_monitor_error", error=str(e))
            finally:
                loop.close()
                self._ws_loop = None

        # daemon=False: 메인 프로세스 종료 전 스레드 정리 보장
        self._ws_thread = threading.Thread(target=run_monitor, daemon=False)
        self._ws_thread.start()
        logger.info("websocket_monitor_started")

    def _stop_websocket_monitor(self) -> None:
        """웹소켓 모니터 중지"""
        if self._ws_monitor and self._ws_loop:
            # 스레드-세이프하게 stop 코루틴 실행
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._ws_monitor.stop(),
                    self._ws_loop
                )
                # 최대 10초 대기
                future.result(timeout=10)
            except Exception as e:
                logger.warning("websocket_monitor_stop_error", error=str(e))
            finally:
                self._ws_monitor = None
            logger.info("websocket_monitor_stopped")

        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=10)
            if self._ws_thread.is_alive():
                logger.warning("websocket_thread_still_alive_after_timeout")
            self._ws_thread = None

    def _cleanup(self) -> None:
        """정리"""
        current_prices = self.buffer.get_current_prices()
        portfolio = self._get_portfolio()
        portfolio_value = portfolio.get_total_value(current_prices)

        # 최종 포지션 데이터 구성
        positions_data = []
        for market, position in portfolio.positions.items():
            price = current_prices.get(market, 0)
            profit = position.get_profit_percent(price)
            positions_data.append({
                "market": market,
                "volume": position.volume,
                "avg_buy_price": position.avg_buy_price,
                "current_price": price,
                "profit_pct": profit,
            })

        # 구조화된 최종 상태 로깅
        logger.info(
            "trading_loop_final_status",
            portfolio_value=portfolio_value,
            position_count=len(portfolio.positions),
            positions=positions_data,
        )

        # 웹소켓 모니터 중지
        if self._ws_monitor:
            self._stop_websocket_monitor()

        # 연결 해제
        self.publisher.disconnect()
        self.candle_reader.disconnect()
        self.redis_portfolio.disconnect()
        if self._redis:
            self._redis.close()
        self.slack.close()

        logger.info("trading_loop_stopped")

    def stop(self) -> None:
        """트레이딩 루프 중지"""
        self._running = False
