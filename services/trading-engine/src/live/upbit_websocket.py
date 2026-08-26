"""업비트 웹소켓 클라이언트 (실시간 호가 데이터 수신)"""

import asyncio
import json
import uuid
from typing import Callable, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed

from ..clients.slack_client import SlackClient
from ..config.settings import Settings
from ..utils import get_logger

logger = get_logger(__name__)

# 업비트 웹소켓 URL
UPBIT_WS_URL = "wss://api.upbit.com/websocket/v1"

# 재연결 설정
RECONNECT_DELAY_INITIAL = 1.0  # 초기 재연결 대기 (초)
RECONNECT_DELAY_MAX = 30.0  # 최대 재연결 대기 (초)
RECONNECT_ALERT_THRESHOLD = 3  # N회 연속 실패 시 Slack 알림
PING_INTERVAL = 60  # 핑 간격 (초)


class UpbitWebSocket:
    """
    업비트 웹소켓 클라이언트

    실시간 호가(orderbook) 데이터를 수신하여 콜백 함수로 전달합니다.
    bid1(매수1호가)을 기준으로 익절/손절을 판단합니다.
    """

    def __init__(
        self,
        on_orderbook: Callable[[str, float], None],
        markets: Optional[List[str]] = None,
        slack_client: Optional[SlackClient] = None,
    ):
        """
        웹소켓 클라이언트 초기화

        Args:
            on_orderbook: 호가 데이터 수신 시 호출할 콜백 함수 (market, bid1_price)
            markets: 구독할 마켓 목록 (None이면 동적으로 설정)
            slack_client: Slack 알림 클라이언트 (재연결 실패 시 알림)
        """
        self.on_orderbook = on_orderbook
        self._markets: Set[str] = set(markets) if markets else set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = RECONNECT_DELAY_INITIAL
        self._reconnect_failures = 0  # 연속 재연결 실패 횟수
        self._slack = slack_client

    @property
    def markets(self) -> List[str]:
        """현재 구독 중인 마켓 목록"""
        return list(self._markets)

    async def update_markets(self, markets: List[str]) -> None:
        """
        구독 마켓 업데이트

        Args:
            markets: 새로운 마켓 목록
        """
        new_markets = set(markets)
        if new_markets == self._markets:
            return

        self._markets = new_markets
        logger.info(
            "markets_updated",
            market_count=len(self._markets),
            markets=list(self._markets)[:5],  # 처음 5개만 로그
        )

        # 연결 중이면 재구독
        if self._ws:
            try:
                await self._subscribe()
            except Exception as e:
                logger.warning("resubscribe_failed", error=str(e))

    async def _subscribe(self) -> None:
        """현재 마켓 목록으로 구독 요청"""
        if not self._markets:
            logger.warning("no_markets_to_subscribe")
            return

        # 업비트 웹소켓 구독 메시지 형식 (호가창)
        subscribe_msg = [
            {"ticket": str(uuid.uuid4())},
            {
                "type": "orderbook",
                "codes": list(self._markets),
                "isOnlyRealtime": True,
            },
            {"format": "SIMPLE"},  # 간소화된 필드명 사용
        ]

        await self._ws.send(json.dumps(subscribe_msg))
        logger.info(
            "subscribed",
            type="orderbook",
            market_count=len(self._markets),
        )

    async def _connect(self) -> None:
        """웹소켓 연결"""
        try:
            self._ws = await websockets.connect(
                UPBIT_WS_URL,
                ping_interval=PING_INTERVAL,
                ping_timeout=10,
            )
            self._reconnect_delay = RECONNECT_DELAY_INITIAL
            self._reconnect_failures = 0  # 연결 성공 시 실패 카운터 리셋
            logger.info("websocket_connected", url=UPBIT_WS_URL)

            # 마켓이 있으면 구독
            if self._markets:
                await self._subscribe()

        except Exception as e:
            self._reconnect_failures += 1
            logger.error(
                "websocket_connect_failed",
                error=str(e),
                failures=self._reconnect_failures,
            )
            raise

    async def _receive_loop(self) -> None:
        """메시지 수신 루프"""
        while self._running and self._ws:
            try:
                message = await self._ws.recv()

                # 바이너리 메시지 (호가 데이터)
                if isinstance(message, bytes):
                    data = json.loads(message.decode('utf-8'))
                    self._handle_orderbook(data)
                else:
                    # 텍스트 메시지 (에러 등)
                    data = json.loads(message)
                    logger.debug("text_message_received", data=data)

            except ConnectionClosed as e:
                logger.warning(
                    "websocket_connection_closed",
                    code=e.code,
                    reason=e.reason,
                )
                break
            except Exception as e:
                logger.error("websocket_receive_error", error=str(e))
                break

    def _handle_orderbook(self, data: dict) -> None:
        """
        호가 데이터 처리

        업비트 SIMPLE 포맷:
        {
            "ty": "orderbook",      # 타입
            "cd": "KRW-BTC",        # 마켓 코드
            "tas": 10.5,            # 총 매도 잔량
            "tbs": 8.2,             # 총 매수 잔량
            "obu": [                # 호가 유닛 배열
                {
                    "ap": 50000000, # 매도 호가 (ask price)
                    "as": 1.5,      # 매도 잔량 (ask size)
                    "bp": 49900000, # 매수 호가 (bid price) ← bid1
                    "bs": 2.0       # 매수 잔량 (bid size)
                },
                ...
            ],
            "tms": 1704096000000    # 타임스탬프 (ms)
        }
        """
        if data.get("ty") != "orderbook":
            return

        market = data.get("cd")
        orderbook_units = data.get("obu", [])

        if not market or not orderbook_units:
            return

        # bid1 = 첫 번째 호가 유닛의 매수 호가
        bid1 = orderbook_units[0].get("bp", 0)

        if bid1 <= 0:
            return

        # 콜백 호출 (market, bid1_price)
        try:
            self.on_orderbook(market, float(bid1))
        except Exception as e:
            logger.error(
                "orderbook_callback_error",
                market=market,
                error=str(e),
            )

    async def start(self) -> None:
        """웹소켓 시작 (자동 재연결 포함)"""
        self._running = True
        logger.info("websocket_starting")

        while self._running:
            try:
                await self._connect()
                await self._receive_loop()

            except Exception as e:
                logger.error("websocket_error", error=str(e))

            # 재연결 대기
            if self._running:
                logger.info(
                    "websocket_reconnecting",
                    delay_sec=self._reconnect_delay,
                    failures=self._reconnect_failures,
                )

                # N회 연속 실패 시 Slack 알림 (최초 1회만)
                if self._reconnect_failures == RECONNECT_ALERT_THRESHOLD and self._slack:
                    self._slack.send_error(
                        context="UpbitWebSocket",
                        error_msg=f"웹소켓 재연결 {self._reconnect_failures}회 연속 실패. "
                                  f"실시간 매도가 지연될 수 있습니다. "
                                  f"다음 재시도까지 {self._reconnect_delay}초 대기.",
                    )

                await asyncio.sleep(self._reconnect_delay)

                # 지수 백오프
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    RECONNECT_DELAY_MAX,
                )

    async def stop(self) -> None:
        """웹소켓 중지"""
        self._running = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("websocket_stopped")
