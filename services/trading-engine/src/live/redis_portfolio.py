"""Redis 포트폴리오 연동 (trade-service와 연동)"""

import json
from typing import Dict, Optional

import redis

from ..models.position import Position, PortfolioState
from ..utils import get_logger, trading_logger

logger = get_logger(__name__)


class RedisPortfolio:
    """
    Redis에서 실제 포트폴리오 읽기 (trade-service가 관리하는 데이터)

    analysis-service의 RedisClient.get_portfolio()와 동일한 역할
    """

    # Redis 키 패턴 (trade-service와 동일)
    PORTFOLIO_KEY = "portfolio:{user_id}"
    TRAILING_FLAG_KEY = "trailing:{user_id}:{market}"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        user_id: str = "default_user",
    ):
        """
        Redis 포트폴리오 초기화

        Args:
            host: Redis 호스트
            port: Redis 포트
            db: Redis DB 번호
            password: Redis 비밀번호
            user_id: 사용자 ID
        """
        self.host = host
        self.port = port
        self.db = db
        self._password = password
        self.user_id = user_id
        self._client: Optional[redis.Redis] = None

    def connect(self) -> None:
        """Redis 연결"""
        self._client = redis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self._password,
            decode_responses=True,
        )
        self._client.ping()
        trading_logger.redis_connected(self.host, self.port)

    def disconnect(self) -> None:
        """Redis 연결 해제"""
        if self._client:
            self._client.close()
            self._client = None

    def get_portfolio(self) -> Optional[PortfolioState]:
        """
        Redis에서 포트폴리오 조회

        Returns:
            PortfolioState 또는 None
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = self.PORTFOLIO_KEY.format(user_id=self.user_id)
        data = self._client.get(key)

        if not data:
            return None

        try:
            portfolio_data = json.loads(data)

            # PortfolioState 생성
            krw_balance = float(portfolio_data.get("krw_balance", 0))
            total_krw_value = float(portfolio_data.get("total_krw_value", krw_balance))
            portfolio = PortfolioState(krw_balance)
            portfolio.initial_quote = total_krw_value  # 초기 투자금 (수익률 계산용)

            # Holdings → Positions 변환
            holdings = portfolio_data.get("holdings", {})
            for currency, holding in holdings.items():
                market = f"KRW-{currency}"
                position = Position(
                    market=market,
                    volume=float(holding.get("balance", 0)),
                    avg_buy_price=float(holding.get("avg_buy_price", 0)),
                )
                if position.volume > 0:
                    portfolio.positions[market] = position

            return portfolio

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse portfolio", error=str(e))
            return None

    def get_krw_balance(self) -> float:
        """KRW 잔고 조회"""
        portfolio = self.get_portfolio()
        return portfolio.quote_balance if portfolio else 0.0

    def get_position(self, market: str) -> Optional[Position]:
        """특정 마켓 포지션 조회"""
        portfolio = self.get_portfolio()
        if not portfolio:
            return None
        return portfolio.positions.get(market)

    def has_position(self, market: str) -> bool:
        """포지션 보유 여부"""
        position = self.get_position(market)
        return position is not None and position.volume > 0

    def get_trailing_flag(self, market: str, flag_type: str) -> bool:
        """
        트레일링 플래그 조회

        Args:
            market: 마켓 코드 (예: "KRW-BTC")
            flag_type: "anca" 또는 "early"

        Returns:
            플래그 값
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"{self.TRAILING_FLAG_KEY.format(user_id=self.user_id, market=market)}:{flag_type}"
        value = self._client.get(key)
        return value == "true" or value == "1"

    def set_trailing_flag(self, market: str, flag_type: str, value: bool) -> None:
        """
        트레일링 플래그 설정

        Args:
            market: 마켓 코드
            flag_type: "anca" 또는 "early"
            value: 플래그 값
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"{self.TRAILING_FLAG_KEY.format(user_id=self.user_id, market=market)}:{flag_type}"
        self._client.set(key, "true" if value else "false")

    def get_peak_profit(self, market: str) -> float:
        """피크 수익률 조회"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"peak_profit:{self.user_id}:{market}"
        value = self._client.get(key)
        return float(value) if value else 0.0

    def set_peak_profit(self, market: str, value: float) -> None:
        """피크 수익률 설정"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"peak_profit:{self.user_id}:{market}"
        self._client.set(key, str(value))

    def get_highest_price(self, market: str) -> float:
        """최고가 조회 (Early 트레일링용)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"highest_price:{self.user_id}:{market}"
        value = self._client.get(key)
        return float(value) if value else 0.0

    def set_highest_price(self, market: str, value: float) -> None:
        """최고가 설정 (Early 트레일링용)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"highest_price:{self.user_id}:{market}"
        self._client.set(key, str(value))

    def get_days_outside_top10(self, market: str) -> int:
        """TOP10 밖 일수 조회"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"days_outside_top10:{self.user_id}:{market}"
        value = self._client.get(key)
        return int(value) if value else 0

    def set_days_outside_top10(self, market: str, days: int) -> None:
        """TOP10 밖 일수 설정"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"days_outside_top10:{self.user_id}:{market}"
        self._client.set(key, str(days))

    def get_last_rank_check_date(self, market: str) -> str:
        """마지막 순위 체크 날짜 조회"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"last_rank_check_date:{self.user_id}:{market}"
        value = self._client.get(key)
        return value if value else ""

    def set_last_rank_check_date(self, market: str, date: str) -> None:
        """마지막 순위 체크 날짜 설정"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"last_rank_check_date:{self.user_id}:{market}"
        self._client.set(key, date)

    def get_entry_count(self, market: str) -> int:
        """매수 횟수 조회"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"entry_count:{self.user_id}:{market}"
        value = self._client.get(key)
        return int(value) if value else 0

    def set_entry_count(self, market: str, count: int) -> None:
        """매수 횟수 설정"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"entry_count:{self.user_id}:{market}"
        self._client.set(key, str(count))

    def increment_entry_count(self, market: str) -> int:
        """매수 횟수 증가 (원자적 연산)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"entry_count:{self.user_id}:{market}"
        return self._client.incr(key)

    def get_entry_time(self, market: str) -> Optional[str]:
        """진입 시간 조회 (ISO format)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"entry_time:{self.user_id}:{market}"
        return self._client.get(key)

    def set_entry_time(self, market: str, entry_time: str) -> None:
        """진입 시간 설정 (ISO format)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"entry_time:{self.user_id}:{market}"
        self._client.set(key, entry_time)

    def get_buy_signal_flag(self, market: str) -> bool:
        """
        매수 신호 플래그 조회

        보유 중인 코인이 직전 1분봉에서 매수 신호가 발생했는지 확인

        Args:
            market: 마켓 코드 (예: "KRW-BTC")

        Returns:
            매수 신호 발생 여부
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"buy_signal:{self.user_id}:{market}"
        value = self._client.get(key)
        return value == "true"

    def set_buy_signal_flag(self, market: str, value: bool) -> None:
        """
        매수 신호 플래그 설정

        Args:
            market: 마켓 코드
            value: 매수 신호 발생 여부
        """
        if not self._client:
            raise RuntimeError("Redis not connected")

        key = f"buy_signal:{self.user_id}:{market}"
        self._client.set(key, "true" if value else "false")

    def clear_trailing_state(self, market: str) -> None:
        """트레일링 상태 초기화 (매도 완료 시)"""
        if not self._client:
            raise RuntimeError("Redis not connected")

        keys = [
            f"trailing:{self.user_id}:{market}:early",
            f"trailing:{self.user_id}:{market}:anca",
            f"trailing:{self.user_id}:{market}:partial_sold",
            f"peak_profit:{self.user_id}:{market}",
            f"highest_price:{self.user_id}:{market}",
            f"days_outside_top10:{self.user_id}:{market}",
            f"last_rank_check_date:{self.user_id}:{market}",
            f"entry_count:{self.user_id}:{market}",
            f"entry_time:{self.user_id}:{market}",
            f"buy_signal:{self.user_id}:{market}",
        ]
        for key in keys:
            self._client.delete(key)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
