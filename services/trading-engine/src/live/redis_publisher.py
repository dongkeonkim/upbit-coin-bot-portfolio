"""Redis Pub/Sub 신호 발행 (trade-service 연동)"""

import json
import time
from datetime import datetime
from typing import Optional

import redis

from ..models.signal import Signal, SignalAction
from ..utils import trading_logger


class RedisPublisher:
    """Redis Pub/Sub으로 매매 신호 발행 (trade-service와 동일한 형식)"""

    # 채널명 (trade-service와 일치해야 함)
    CHANNEL_SIGNALS = "trade:signals"  # trade-service가 구독하는 채널
    CHANNEL_STATUS = "trading:status"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        user_id: str = "default_user",
    ):
        """
        Redis Publisher 초기화

        Args:
            host: Redis 호스트
            port: Redis 포트
            db: Redis DB 번호
            password: Redis 비밀번호
            user_id: 사용자 ID (trade-service 연동용)
        """
        self.host = host
        self.port = port
        self.db = db
        self._client: Optional[redis.Redis] = None
        self._password = password
        self.user_id = user_id

    def connect(self) -> None:
        """Redis 연결"""
        self._client = redis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self._password,
            decode_responses=True,
        )
        # 연결 테스트
        self._client.ping()
        trading_logger.redis_connected(self.host, self.port)

    def disconnect(self) -> None:
        """Redis 연결 해제"""
        if self._client:
            self._client.close()
            self._client = None
            trading_logger.redis_disconnected()

    def publish_buy_signal(self, signal: Signal) -> int:
        """
        매수 신호 발행 (trade-service TradeSignal 형식)

        Args:
            signal: 매수 신호 (Signal with action=BUY)

        Returns:
            수신자 수
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        # trade-service TradeSignal 형식에 맞춤
        message = {
            "userId": self.user_id,
            "signalType": "BUY",
            "market": signal.market,
            "price": signal.price,
            "reason": signal.reason,
            "timestamp": int(time.time() * 1000),  # Unix timestamp (ms)
            "amount": signal.amount,  # BUY: 투자 금액 (KRW)
        }

        count = self._client.publish(self.CHANNEL_SIGNALS, json.dumps(message))
        trading_logger.signal(
            "BUY",
            signal.market,
            price=signal.price,
            amount=signal.amount,
            reason=signal.reason,
            subscribers=count,
        )
        return count

    def publish_sell_signal(self, signal: Signal) -> int:
        """
        매도 신호 발행 (trade-service TradeSignal 형식)

        Args:
            signal: 매도 신호 (Signal with action=SELL)

        Returns:
            수신자 수
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        # trade-service TradeSignal 형식에 맞춤
        message = {
            "userId": self.user_id,
            "signalType": "SELL",
            "market": signal.market,
            "price": signal.price,
            "reason": signal.reason,
            "timestamp": int(time.time() * 1000),  # Unix timestamp (ms)
            "volume": signal.volume,  # SELL: 매도 수량
            "avg_buy_price": signal.avg_buy_price,  # 평균 매수가 (손익 계산용)
        }

        count = self._client.publish(self.CHANNEL_SIGNALS, json.dumps(message))
        trading_logger.signal(
            "SELL",
            signal.market,
            price=signal.price,
            volume=signal.volume,
            reason=signal.reason,
            subscribers=count,
        )
        return count

    def publish_status(self, status: dict) -> int:
        """
        상태 정보 발행

        Args:
            status: 상태 딕셔너리

        Returns:
            수신자 수
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        status["timestamp"] = datetime.now().isoformat()
        return self._client.publish(self.CHANNEL_STATUS, json.dumps(status))

    def publish_heartbeat(
        self,
        portfolio_value: float,
        position_count: int,
        market_count: int,
    ) -> int:
        """
        하트비트 발행 (상태 모니터링용)

        Args:
            portfolio_value: 포트폴리오 총 가치
            position_count: 보유 포지션 수
            market_count: 모니터링 마켓 수

        Returns:
            수신자 수
        """
        return self.publish_status({
            "type": "heartbeat",
            "portfolio_value": portfolio_value,
            "position_count": position_count,
            "market_count": market_count,
        })

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


class RedisSubscriber:
    """Redis Pub/Sub 신호 구독 (테스트/모니터링용)"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
    ):
        """
        Redis Subscriber 초기화

        Args:
            host: Redis 호스트
            port: Redis 포트
            db: Redis DB 번호
            password: Redis 비밀번호
        """
        self.host = host
        self.port = port
        self.db = db
        self._client: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._password = password

    def connect(self) -> None:
        """Redis 연결 및 구독 시작"""
        self._client = redis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self._password,
            decode_responses=True,
        )
        self._pubsub = self._client.pubsub()
        self._pubsub.subscribe(
            RedisPublisher.CHANNEL_SIGNALS,  # trade:signals (trade-service와 동일)
            RedisPublisher.CHANNEL_STATUS,
        )
        trading_logger.redis_connected(self.host, self.port)

    def disconnect(self) -> None:
        """Redis 연결 해제"""
        if self._pubsub:
            self._pubsub.unsubscribe()
            self._pubsub.close()
            self._pubsub = None
        if self._client:
            self._client.close()
            self._client = None
        trading_logger.redis_disconnected()

    def listen(self):
        """
        메시지 수신 제너레이터

        Yields:
            (channel, message) 튜플
        """
        if not self._pubsub:
            raise RuntimeError("Redis not connected")

        for message in self._pubsub.listen():
            if message["type"] == "message":
                channel = message["channel"]
                data = json.loads(message["data"])
                yield channel, data

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
