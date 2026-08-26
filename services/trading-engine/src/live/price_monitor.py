"""실시간 가격 모니터 (웹소켓 기반 익절/손절)"""

import asyncio
import json
import signal
import time
from dataclasses import dataclass
from typing import Dict, Optional

import redis.asyncio as aioredis

from ..clients.slack_client import SlackClient
from ..config.params import Params
from ..config.settings import Settings
from ..models.position import Position
from ..models.signal import Signal, SellReason
from ..utils import get_logger
from .redis_portfolio import RedisPortfolio
from .redis_publisher import RedisPublisher
from .upbit_websocket import UpbitWebSocket

logger = get_logger(__name__)

# 포트폴리오 갱신 주기 (초)
PORTFOLIO_REFRESH_INTERVAL = 5

# 매도 쿨다운 (동일 마켓 중복 신호 방지)
SELL_COOLDOWN_SEC = 10

# trade:completed 채널 (trade-service가 발행)
TRADE_COMPLETED_CHANNEL = "trade:completed"


# ML 전략 파라미터 (ml_strategy.py와 동일)
ML_SL_PCT = 1.0                # 손절 기준 (%)
ML_DEFAULT_HOLD_MINUTES = 120  # 기본 보유 시간 (분)

# 단계별 트레일링 스탑 설정 (도달 수익률, 고점 대비 하락 허용폭)
# 수익률이 높아질수록 더 큰 여유를 줌
TRAILING_TIERS = [
    (4.0, 1.0),  # +4.0% 도달 시: 고점 대비 -1.0% 하락 허용
    (3.2, 0.7),  # +3.2% 도달 시: 고점 대비 -0.7% 하락 허용
    (2.5, 0.5),  # +2.5% 도달 시: 고점 대비 -0.5% 하락 허용
    (1.8, 0.3),  # +1.8% 도달 시: 고점 대비 -0.3% 하락 허용 (기본)
]

# 코인별 hold_minutes (ml_strategy.py COIN_PARAMS와 동일)
COIN_HOLD_MINUTES = {
    'KRW-BTC': 120,
    'KRW-ETH': 60,
    'KRW-XRP': 120,
    'KRW-SOL': 120,
    'KRW-ADA': 120,
    'KRW-DOGE': 120,
}

# 시간 종료 체크 주기 (초)
TIME_EXIT_CHECK_INTERVAL = 10


@dataclass
class MonitorTarget:
    """모니터링 대상 포지션 (ML 전략 전용)"""
    market: str
    avg_buy_price: float
    volume: float
    # 시간 종료
    entry_time: float = 0           # 진입 시간 (Unix timestamp)
    hold_minutes: int = ML_DEFAULT_HOLD_MINUTES  # 보유 시간 (분)
    # 상태
    last_sell_time: float = 0       # 마지막 매도 신호 시간
    peak_price: float = 0           # 트레일링용 최고가 (bid1 기준)
    peak_profit_pct: float = 0      # 도달한 최고 수익률 (단계별 트레일링용)
    trailing_active: bool = False   # 트레일링 모드 활성화 여부
    last_price: float = 0           # 마지막 bid1 가격 (시간 종료 매도용)


class PriceMonitor:
    """
    실시간 가격 모니터

    웹소켓으로 호가(orderbook) 데이터를 수신하여 보유 포지션의 익절/손절을 실시간 체크합니다.
    bid1(매수1호가)을 기준으로 판단합니다 - 실제 매도 시 받을 수 있는 가격.
    """

    def __init__(
        self,
        params: Params,
        settings: Settings,
        user_id: str = "default_user",
        strategy_type: str = "drop-bounce",
        live_mode: bool = False,
    ):
        """
        가격 모니터 초기화

        Args:
            params: 전략 파라미터
                - stop_loss: 손절 기준
                - trailing_target_early, trailing_drop_early, trailing_exit_early: Early 트레일링
                - trailing_target, trailing_drop: ANCA 트레일링
            settings: 환경 설정
            user_id: 사용자 ID
            strategy_type: 전략 타입
            live_mode: 실거래 모드 여부
        """
        self.params = params
        self.settings = settings
        self.user_id = user_id
        self.strategy_type = strategy_type
        self.live_mode = live_mode

        # 모니터링 대상 (market -> MonitorTarget)
        self._targets: Dict[str, MonitorTarget] = {}

        # Redis 연결
        self._redis_portfolio = RedisPortfolio(
            host=settings.redis_host,
            port=settings.redis_port,
            user_id=user_id,
        )
        self._publisher = RedisPublisher(
            host=settings.redis_host,
            port=settings.redis_port,
            user_id=user_id,
        )

        # Async Redis (trade:completed 구독용)
        self._async_redis: Optional[aioredis.Redis] = None

        # Slack 알림
        self._slack = SlackClient(
            webhook_url=settings.slack_webhook_url,
            service_name="price-monitor",
        )

        # 웹소켓
        self._websocket: Optional[UpbitWebSocket] = None

        # 상태
        self._running = False

    def _on_orderbook(self, market: str, bid1_price: float) -> None:
        """
        호가 데이터 수신 콜백 (ML 전략과 동일한 익절/손절 로직)

        매도 우선순위:
        1. 손절: -1.0%
        2. 트레일링 스탑: +1.8% 도달 시 활성화, 고점 대비 -0.3% 하락 시 매도

        단, buy_signal 플래그가 true면 매도하지 않음 (모멘텀 유지 중)

        Args:
            market: 마켓 코드 (예: KRW-BTC)
            bid1_price: 매수1호가 (실제 매도 시 받을 수 있는 가격)
        """
        # 모니터링 대상인지 확인
        target = self._targets.get(market)
        if not target:
            return

        # 수익률 계산 (bid1 기준)
        profit_pct = ((bid1_price - target.avg_buy_price) / target.avg_buy_price) * 100

        # 쿨다운 체크 (중복 신호 방지)
        now = time.time()
        if now - target.last_sell_time < SELL_COOLDOWN_SEC:
            return

        # 매수 신호 플래그 체크 - true면 매도하지 않음 (모멘텀 유지 중)
        try:
            if self._redis_portfolio.get_buy_signal_flag(market):
                # 고점은 계속 갱신 (트레일링 준비)
                if bid1_price > target.peak_price:
                    target.peak_price = bid1_price
                target.last_price = bid1_price
                return
        except Exception as e:
            logger.warning("buy_signal_flag_check_failed", market=market, error=str(e))

        # 1. 손절 체크 (최우선) - ML 전략과 동일: -1.0%
        if profit_pct <= -ML_SL_PCT:
            self._trigger_sell(
                target=target,
                price=bid1_price,
                profit_pct=profit_pct,
                reason="ML_SL_HIT",
            )
            target.last_sell_time = now
            return

        # 2. 최고 수익률 및 고점 갱신
        if profit_pct > target.peak_profit_pct:
            target.peak_profit_pct = profit_pct
        if bid1_price > target.peak_price:
            target.peak_price = bid1_price

        # 3. 단계별 트레일링 스탑 체크
        # 도달한 최고 수익률에 따라 하락 허용폭 결정
        trailing_drop_pct = None
        for tier_pct, drop_pct in TRAILING_TIERS:
            if target.peak_profit_pct >= tier_pct:
                trailing_drop_pct = drop_pct
                if not target.trailing_active:
                    target.trailing_active = True
                    logger.info(
                        "trailing_activated",
                        market=market,
                        profit_pct=round(profit_pct, 2),
                        peak_profit_pct=round(target.peak_profit_pct, 2),
                        tier_pct=tier_pct,
                        drop_pct=drop_pct,
                        peak_price=target.peak_price,
                    )
                break

        # 4. 트레일링 스탑 매도 체크
        if target.trailing_active and trailing_drop_pct is not None:
            trailing_stop_price = target.peak_price * (1 - trailing_drop_pct / 100)
            if bid1_price <= trailing_stop_price:
                self._trigger_sell(
                    target=target,
                    price=bid1_price,
                    profit_pct=profit_pct,
                    reason=f"ML_TRAILING_STOP(tier:{target.peak_profit_pct:.1f}%,drop:{trailing_drop_pct}%)",
                )
                target.last_sell_time = now
                return

        # 마지막 가격 저장 (시간 종료 매도용)
        target.last_price = bid1_price

    def _trigger_sell(
        self,
        target: MonitorTarget,
        price: float,
        profit_pct: float,
        reason: str,
    ) -> None:
        """
        매도 신호 발행 (전량 매도)

        Args:
            target: 매도 대상
            price: bid1 가격 (예상 매도가)
            profit_pct: 수익률 (%)
            reason: 매도 사유
        """
        logger.info(
            "sell_triggered",
            market=target.market,
            bid1_price=price,
            avg_buy_price=target.avg_buy_price,
            profit_pct=round(profit_pct, 2),
            reason=reason,
        )

        # 매도 신호 생성
        sell_signal = Signal.sell(
            market=target.market,
            price=price,
            volume=target.volume,
            reason=reason,
        )
        sell_signal.avg_buy_price = target.avg_buy_price
        sell_signal.profit_percent = profit_pct

        # Redis로 신호 발행 (trade-service가 실제 매도 실행)
        try:
            self._publisher.publish_sell_signal(sell_signal)
        except Exception as e:
            logger.error("publish_sell_failed", error=str(e))

        # Slack 알림
        try:
            self._slack.send_sell_signal(
                market=target.market,
                price=price,
                volume=target.volume,
                reason=f"[실시간-bid1] {reason}",
                mode="LIVE",
                avg_buy_price=target.avg_buy_price,
            )
        except Exception as e:
            logger.error("slack_notification_failed", error=str(e))

        # 모니터링 대상에서 제거 (매도 완료)
        if target.market in self._targets:
            del self._targets[target.market]
            # Redis 트레일링 상태 초기화
            self._redis_portfolio.clear_trailing_state(target.market)
            logger.info("target_removed", market=target.market)

    async def _subscribe_trade_completed(self) -> None:
        """
        trade:completed 채널 구독 (체결 즉시 포트폴리오 갱신)

        trade-service가 주문 체결 시 발행하는 이벤트를 구독하여
        BUY/SELL 체결 즉시 포트폴리오를 갱신합니다.
        """
        try:
            self._async_redis = aioredis.Redis(
                host=self.settings.redis_host,
                port=self.settings.redis_port,
                decode_responses=True,
            )

            pubsub = self._async_redis.pubsub()
            await pubsub.subscribe(TRADE_COMPLETED_CHANNEL)

            logger.info(
                "trade_completed_subscribed",
                channel=TRADE_COMPLETED_CHANNEL,
            )

            while self._running:
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=1.0
                    )

                    if message and message["type"] == "message":
                        data = json.loads(message["data"])
                        side = data.get("side")

                        # BUY 또는 SELL 체결 시 포트폴리오 즉시 갱신
                        if side in ("BUY", "SELL"):
                            market = data.get("market", "unknown")
                            logger.info(
                                "trade_completed_received",
                                side=side,
                                market=market,
                            )
                            await self._refresh_portfolio_once()

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    logger.warning("invalid_trade_completed_message", error=str(e))

        except Exception as e:
            logger.error("trade_completed_subscribe_error", error=str(e))
        finally:
            if self._async_redis:
                await self._async_redis.close()

    async def _refresh_portfolio_once(self) -> None:
        """포트폴리오 1회 갱신 (trade:completed 수신 시 호출)"""
        try:
            portfolio = self._redis_portfolio.get_portfolio()
            if not portfolio:
                logger.warning("portfolio_not_found")
                return

            # 현재 보유 포지션 기준으로 타겟 업데이트
            current_markets = set()

            for market, position in portfolio.positions.items():
                current_markets.add(market)

                # 이미 모니터링 중이면 스킵 (단, 수량/가격 변경 시 업데이트)
                if market in self._targets:
                    target = self._targets[market]
                    if (target.volume != position.volume or
                        target.avg_buy_price != position.avg_buy_price):
                        # 평균 매수가 변경 시 (추가 매수) 트레일링 리셋
                        avg_price_changed = target.avg_buy_price != position.avg_buy_price
                        old_avg_price = target.avg_buy_price

                        # 포지션 변경됨 - 업데이트
                        target.volume = position.volume
                        target.avg_buy_price = position.avg_buy_price

                        if avg_price_changed:
                            # 트레일링 상태 리셋
                            target.peak_price = 0.0
                            target.trailing_active = False
                            logger.info(
                                "trailing_reset_on_avg_price_change",
                                market=market,
                                old_avg_price=old_avg_price,
                                new_avg_price=position.avg_buy_price,
                                reason="additional_buy_detected",
                            )

                        logger.info(
                            "target_updated",
                            market=market,
                            volume=position.volume,
                            avg_buy_price=position.avg_buy_price,
                        )
                    continue

                # 새 포지션 추가 - Redis에서 entry_time 복구
                entry_time = time.time()  # 기본값: 현재 시간
                entry_time_str = self._redis_portfolio.get_entry_time(market)
                if entry_time_str:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(entry_time_str)
                        entry_time = dt.timestamp()
                    except (ValueError, TypeError):
                        pass  # 파싱 실패 시 현재 시간 사용

                # 코인별 hold_minutes
                hold_minutes = COIN_HOLD_MINUTES.get(market, ML_DEFAULT_HOLD_MINUTES)

                self._targets[market] = MonitorTarget(
                    market=market,
                    avg_buy_price=position.avg_buy_price,
                    volume=position.volume,
                    entry_time=entry_time,
                    hold_minutes=hold_minutes,
                )
                logger.info(
                    "target_added",
                    market=market,
                    avg_buy_price=position.avg_buy_price,
                    volume=position.volume,
                    hold_minutes=hold_minutes,
                    sl_pct=ML_SL_PCT,
                    trailing_tiers=str(TRAILING_TIERS),
                )

            # 포트폴리오에서 제거된 마켓 정리
            removed = set(self._targets.keys()) - current_markets
            for market in removed:
                del self._targets[market]
                logger.info("target_removed", market=market, reason="not_in_portfolio")

            # 웹소켓 구독 마켓 업데이트
            if self._websocket:
                await self._websocket.update_markets(list(current_markets))

            logger.debug(
                "portfolio_refreshed",
                target_count=len(self._targets),
                markets=list(self._targets.keys()),
            )

        except Exception as e:
            logger.error("portfolio_refresh_error", error=str(e))

    async def _refresh_portfolio(self) -> None:
        """포트폴리오 갱신 (주기적 실행)"""
        while self._running:
            await self._refresh_portfolio_once()
            await asyncio.sleep(PORTFOLIO_REFRESH_INTERVAL)

    # ML_TIME_EXIT 비활성화 - 백테스트와 일치시키기 위해 주석 처리
    # async def _check_time_exits(self) -> None:
    #     """시간 종료 체크 (주기적 실행)"""
    #     while self._running:
    #         now = time.time()
    #         targets_to_sell = []
    #
    #         for market, target in self._targets.items():
    #             # entry_time이 설정되지 않았으면 스킵
    #             if target.entry_time <= 0:
    #                 continue
    #
    #             # 보유 시간 체크
    #             holding_seconds = now - target.entry_time
    #             hold_limit_seconds = target.hold_minutes * 60
    #
    #             if holding_seconds >= hold_limit_seconds:
    #                 targets_to_sell.append((market, target))
    #
    #         # 시간 종료 매도 실행
    #         for market, target in targets_to_sell:
    #             # 매수 신호 플래그 체크 - true면 매도하지 않음 (모멘텀 유지 중)
    #             try:
    #                 if self._redis_portfolio.get_buy_signal_flag(market):
    #                     logger.debug(
    #                         "time_exit_skipped_buy_signal",
    #                         market=market,
    #                         reason="buy_signal_flag_true",
    #                     )
    #                     continue
    #             except Exception as e:
    #                 logger.warning("buy_signal_flag_check_failed", market=market, error=str(e))
    #
    #             # 마지막 가격이 있으면 사용, 없으면 평균 매수가 사용
    #             sell_price = target.last_price if target.last_price > 0 else target.avg_buy_price
    #             profit_pct = ((sell_price - target.avg_buy_price) / target.avg_buy_price) * 100
    #
    #             logger.info(
    #                 "time_exit_triggered",
    #                 market=market,
    #                 holding_minutes=round(holding_seconds / 60, 1),
    #                 hold_limit=target.hold_minutes,
    #                 profit_pct=round(profit_pct, 2),
    #             )
    #
    #             self._trigger_sell(
    #                 target=target,
    #                 price=sell_price,
    #                 profit_pct=profit_pct,
    #                 reason="ML_TIME_EXIT",
    #             )
    #
    #         await asyncio.sleep(TIME_EXIT_CHECK_INTERVAL)

    async def start(self) -> None:
        """모니터 시작"""
        logger.info(
            "price_monitor_starting",
            user_id=self.user_id,
            sl_pct=ML_SL_PCT,
            trailing_tiers=str(TRAILING_TIERS),
        )

        # Redis 연결
        self._redis_portfolio.connect()
        self._publisher.connect()

        self._running = True

        # 웹소켓 생성 (호가창 구독)
        self._websocket = UpbitWebSocket(
            on_orderbook=self._on_orderbook,
            markets=list(self._targets.keys()),
            slack_client=self._slack,  # 재연결 실패 시 알림용
        )

        # Slack 시작 알림
        self._slack.send_service_started(
            mode="LIVE" if self.live_mode else "SIMULATION",
            user_id=self.user_id,
            strategy=self.strategy_type,
        )

        # 태스크 시작
        portfolio_task = asyncio.create_task(self._refresh_portfolio())
        websocket_task = asyncio.create_task(self._websocket.start())
        trade_completed_task = asyncio.create_task(self._subscribe_trade_completed())
        # time_exit_task 제거 - 백테스트와 일치시키기 위해 ML_TIME_EXIT 비활성화

        # 시그널 핸들러 (메인 스레드에서만 등록 가능)
        try:
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
        except (ValueError, RuntimeError):
            # 메인 스레드가 아닌 경우 무시 (TradingLoop에서 스레드로 실행 시)
            pass

        try:
            await asyncio.gather(
                portfolio_task,
                websocket_task,
                trade_completed_task,
            )
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """모니터 중지"""
        logger.info("price_monitor_stopping")
        self._running = False

        if self._websocket:
            await self._websocket.stop()

        # Redis 연결 해제
        self._redis_portfolio.disconnect()
        self._publisher.disconnect()

        # Slack 종료 알림
        self._slack.send_service_stopping(reason="실시간 모니터 종료")
        self._slack.close()

        logger.info("price_monitor_stopped")


async def run_price_monitor(
    user_id: str = "default_user",
) -> None:
    """
    가격 모니터 실행 (진입점)

    ML 전략 파라미터 (고정):
    - ML_SL_PCT: 손절 기준 (-1.0%)
    - TRAILING_TIERS: 단계별 트레일링 스탑
      - +1.8% 도달 → 고점 대비 -0.3% 하락 시 매도
      - +2.5% 도달 → 고점 대비 -0.5% 하락 시 매도
      - +3.2% 도달 → 고점 대비 -0.7% 하락 시 매도
      - +4.0% 도달 → 고점 대비 -1.0% 하락 시 매도

    Args:
        user_id: 사용자 ID
    """
    from ..config.params import BEST_STRATEGY

    settings = Settings.from_env()
    params = BEST_STRATEGY

    monitor = PriceMonitor(
        params=params,
        settings=settings,
        user_id=user_id,
    )

    await monitor.start()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="실시간 가격 모니터 (트레일링 스톱)")
    parser.add_argument("--user-id", default="default_user", help="사용자 ID")
    args = parser.parse_args()

    asyncio.run(run_price_monitor(
        user_id=args.user_id,
    ))
