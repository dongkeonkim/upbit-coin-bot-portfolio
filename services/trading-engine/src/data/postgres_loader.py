"""PostgreSQL 데이터 로더 (Go database/db.go와 동일)"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
import time as time_module

import psycopg2
from psycopg2.extras import RealDictCursor

from ..models.candle import Candle
from ..config.settings import Settings
from ..utils import get_logger

logger = get_logger(__name__)


@dataclass
class Period:
    """기간 정의 (Go config.Period와 동일)"""

    name: str
    start: datetime
    end: datetime


class DataCache:
    """데이터 캐시 (1분봉만 저장) (Go DataCache와 동일)"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()
        self.conn: Optional[psycopg2.extensions.connection] = None
        self.data: Dict[str, Dict[str, List[Candle]]] = {}  # {market: {period: [Candle]}}
        self.loaded = False

    def connect(self) -> None:
        """데이터베이스 연결 (Go Connect와 동일)"""
        if self.conn is not None:
            return

        self.conn = psycopg2.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            dbname=self.settings.db_name,
        )

        logger.info(
            "db_connected",
            host=self.settings.db_host,
            port=self.settings.db_port,
        )

    def disconnect(self) -> None:
        """데이터베이스 연결 종료 (Go Disconnect와 동일)"""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            logger.info("db_disconnected")

    def load_all_data(
        self, markets: List[str], periods: Dict[str, Period]
    ) -> None:
        """모든 마켓/기간의 1분봉 데이터 로드 (Go LoadAllData와 동일)"""
        if self.conn is None:
            raise RuntimeError("database not connected")

        logger.info(
            "candles_loading",
            market_count=len(markets),
            period_count=len(periods),
        )

        # 마켓이 비어있으면 자동 조회
        if len(markets) == 0:
            markets = self._get_available_markets()
            logger.info(
                "markets_found",
                market_count=len(markets),
            )

        total_candles = 0
        start_time = time_module.time()

        for market in markets:
            market_map: Dict[str, List[Candle]] = {}
            for period_name, period in periods.items():
                candles = self._load_candles_1m(market, period.start, period.end)
                market_map[period_name] = candles
                total_candles += len(candles)
            self.data[market] = market_map

        elapsed = time_module.time() - start_time
        logger.info(
            "candles_loaded",
            total_candles=total_candles,
            elapsed_sec=round(elapsed, 2),
        )
        self.loaded = True

    def _load_candles_1m(
        self, market: str, start_time: datetime, end_time: datetime
    ) -> List[Candle]:
        """1분봉 데이터 로드 (Upbit candles_upbit 테이블 사용)"""
        query = """
            SELECT
                market,
                interval as timeframe,
                EXTRACT(EPOCH FROM timestamp)::bigint * 1000 as timestamp,
                open,
                high,
                low,
                close,
                volume,
                quote_volume
            FROM candles_upbit
            WHERE market = %s
                AND interval = '1m'
                AND timestamp >= %s
                AND timestamp <= %s
            ORDER BY timestamp ASC
        """

        # DB 컬럼이 'timestamp without time zone'이므로 timezone 정보 제거
        # Go의 lib/pq는 timezone 정보를 그대로 비교하지만, psycopg2는 서버 로컬 시간대로 변환함
        # Go와 동일한 동작을 위해 naive datetime으로 변환
        start_naive = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
        end_naive = end_time.replace(tzinfo=None) if end_time.tzinfo else end_time

        candles: List[Candle] = []
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (market, start_naive, end_naive))
            rows = cur.fetchall()

            for row in rows:
                candle = Candle(
                    market=row["market"],
                    timeframe=row["timeframe"],
                    timestamp=row["timestamp"],
                    candle_time=row["timestamp"],  # 백테스트에서도 candle_time 설정 (실시간과 일관성)
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    quote_volume=float(row["quote_volume"]),
                )
                candles.append(candle)

        return candles

    def _get_available_markets(self) -> List[str]:
        """데이터베이스에서 사용 가능한 마켓 목록 조회 (candles_upbit 테이블)"""
        query = """
            SELECT DISTINCT market
            FROM candles_upbit
            WHERE interval = '1m'
            ORDER BY market
        """

        markets: List[str] = []
        with self.conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            for row in rows:
                markets.append(row[0])

        return markets

    def get_candles_1m(self, market: str, period_name: str) -> List[Candle]:
        """특정 마켓/기간의 1분봉 데이터 조회 (Go GetCandles1m와 동일)"""
        if not self.loaded:
            raise RuntimeError("data not loaded")

        market_map = self.data.get(market)
        if market_map is None:
            raise ValueError(f"market not found: {market}")

        candles = market_map.get(period_name)
        if candles is None:
            raise ValueError(f"period not found: {period_name}")

        return candles

    def get_markets(self) -> List[str]:
        """로드된 마켓 목록 반환 (Go GetMarkets와 동일)"""
        return list(self.data.keys())

    def get_all_candles_1m_for_period(
        self, period_name: str
    ) -> Dict[str, List[Candle]]:
        """특정 기간의 모든 마켓 1분봉 데이터 반환"""
        result: Dict[str, List[Candle]] = {}
        for market, market_map in self.data.items():
            if period_name in market_map:
                result[market] = market_map[period_name]
        return result
