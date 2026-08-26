"""Redis 캔들 리더 (market-data-service가 publish한 캔들 데이터 읽기)"""

import json
from typing import Dict, List, Optional

import redis

from ..models.candle import Candle
from ..utils import get_logger, trading_logger

logger = get_logger(__name__)


class RedisCandleReader:
    """
    Redis에서 캔들 데이터를 읽는 클라이언트

    market-data-service가 Redis에 publish한 캔들 데이터를 읽습니다.
    - 캔들 저장 형식: candles:1m:{market} (List, 최신이 앞)
    - 대상 마켓: market:target:{userId}
    """

    CANDLE_KEY_PREFIX = "candles:1m:"
    TARGET_MARKETS_KEY = "market:target:"
    MAX_CANDLES = 500  # market-data-service와 동일 (ML 전략 480개 필요)

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        user_id: str = "default_user",
    ):
        """
        Redis 캔들 리더 초기화

        Args:
            host: Redis 호스트
            port: Redis 포트
            db: Redis DB 번호
            user_id: 사용자 ID (대상 마켓 조회용)
        """
        self.host = host
        self.port = port
        self.db = db
        self.user_id = user_id
        self._client: Optional[redis.Redis] = None

    def connect(self) -> None:
        """Redis 연결"""
        if self._client is None:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                decode_responses=True,
            )
            self._client.ping()
            trading_logger.redis_connected(self.host, self.port)

    def disconnect(self) -> None:
        """Redis 연결 종료"""
        if self._client:
            self._client.close()
            self._client = None

    def get_target_markets(self) -> List[str]:
        """
        대상 마켓 목록 조회 (market-data-service가 설정한 것)

        Returns:
            마켓 코드 목록
        """
        if not self._client:
            return []

        key = f"{self.TARGET_MARKETS_KEY}{self.user_id}"
        data = self._client.get(key)

        if not data:
            return []

        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return []

    def get_candles(self, market: str, count: int = 200) -> List[Candle]:
        """
        특정 마켓의 캔들 데이터 조회

        Args:
            market: 마켓 코드 (예: KRW-BTC)
            count: 조회할 캔들 수 (최신 N개)

        Returns:
            캔들 목록 (시간순 - 과거 → 최신)
        """
        if not self._client:
            return []

        key = f"{self.CANDLE_KEY_PREFIX}{market}"

        # Redis List는 LPUSH로 저장되어 최신이 앞에 있음
        # LRANGE 0 count-1 로 최신 count개 조회
        raw_candles = self._client.lrange(key, 0, count - 1)

        if not raw_candles:
            return []

        candles = []
        for raw in raw_candles:
            try:
                data = json.loads(raw)
                candle = Candle(
                    market=data["market"],
                    timestamp=data["timestamp"],
                    open=float(data["open"]),
                    high=float(data["high"]),
                    low=float(data["low"]),
                    close=float(data["close"]),
                    volume=float(data["volume"]),
                    quote_volume=float(data["quote_volume"]),
                    timeframe=data.get("interval", "1m"),
                    candle_time=data.get("candle_time"),  # 캔들 시작시간 (피처 계산용)
                )
                candles.append(candle)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse candle", error=str(e))
                continue

        # Redis에서는 최신이 앞이므로 역순으로 정렬 (과거 → 최신)
        candles.reverse()
        return candles

    def get_latest_candle(self, market: str) -> Optional[Candle]:
        """
        특정 마켓의 최신 캔들 1개 조회

        Args:
            market: 마켓 코드

        Returns:
            최신 캔들 (없으면 None)
        """
        candles = self.get_candles(market, count=1)
        return candles[0] if candles else None

    def get_latest_candles(self, markets: List[str]) -> Dict[str, Candle]:
        """
        여러 마켓의 최신 캔들 조회

        Args:
            markets: 마켓 코드 목록

        Returns:
            {market: candle} 딕셔너리
        """
        result = {}
        for market in markets:
            candle = self.get_latest_candle(market)
            if candle:
                result[market] = candle
        return result

    def get_all_candles(self, markets: List[str], count: int = 200) -> Dict[str, List[Candle]]:
        """
        여러 마켓의 캔들 데이터 조회

        Args:
            markets: 마켓 코드 목록
            count: 각 마켓별 조회할 캔들 수

        Returns:
            {market: [candles]} 딕셔너리
        """
        result = {}
        for market in markets:
            candles = self.get_candles(market, count)
            if candles:
                result[market] = candles
        return result

    def has_candles(self, market: str) -> bool:
        """
        마켓에 캔들 데이터가 있는지 확인

        Args:
            market: 마켓 코드

        Returns:
            캔들 데이터 존재 여부
        """
        if not self._client:
            return False

        key = f"{self.CANDLE_KEY_PREFIX}{market}"
        return self._client.exists(key) > 0

    def get_candle_count(self, market: str) -> int:
        """
        마켓의 캔들 데이터 개수 조회

        Args:
            market: 마켓 코드

        Returns:
            캔들 개수
        """
        if not self._client:
            return 0

        key = f"{self.CANDLE_KEY_PREFIX}{market}"
        return self._client.llen(key)
