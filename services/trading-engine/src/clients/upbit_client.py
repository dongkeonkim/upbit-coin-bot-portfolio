"""Upbit API 클라이언트 (실시간 캔들 조회)"""

import time
import hmac
import hashlib
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx

from ..models.candle import Candle


class RateLimitError(Exception):
    """429 Rate Limit 에러"""

    def __init__(self, endpoint: str, retry_after: Optional[int] = None):
        self.endpoint = endpoint
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for {endpoint}")


class UpbitClient:
    """Upbit REST API 클라이언트"""

    BASE_URL = "https://api.upbit.com/v1"

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        timeout: float = 10.0,
    ):
        """
        Upbit API 클라이언트 초기화

        Args:
            access_key: Upbit API access key (공개 API만 사용시 불필요)
            secret_key: Upbit API secret key (공개 API만 사용시 불필요)
            timeout: HTTP 요청 타임아웃 (초)
        """
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self._last_request_time = 0.0
        self._min_request_interval = 0.2  # 200ms (초당 5회 - 보수적 설정)

    def _rate_limit(self) -> None:
        """API 호출 속도 제한"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """GET 요청 (429 Rate Limit 에러 시 RateLimitError 발생)"""
        self._rate_limit()
        url = f"{self.BASE_URL}{endpoint}"
        response = self._client.get(url, params=params)

        # 429 Rate Limit 에러 감지
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            retry_after_int = int(retry_after) if retry_after else None
            raise RateLimitError(endpoint, retry_after_int)

        response.raise_for_status()
        return response.json()

    def get_markets(self, is_details: bool = False) -> List[Dict]:
        """
        마켓 코드 목록 조회

        Args:
            is_details: 상세 정보 포함 여부

        Returns:
            마켓 코드 목록
        """
        params = {"isDetails": str(is_details).lower()}
        return self._get("/market/all", params)

    def get_krw_markets(self) -> List[str]:
        """KRW 마켓 코드 목록 조회"""
        markets = self.get_markets()
        return [m["market"] for m in markets if m["market"].startswith("KRW-")]

    def get_candles_minutes(
        self,
        market: str,
        unit: int = 1,
        count: int = 200,
        to: Optional[str] = None,
    ) -> List[Candle]:
        """
        분봉 캔들 조회

        Args:
            market: 마켓 코드 (예: "KRW-BTC")
            unit: 분 단위 (1, 3, 5, 15, 10, 30, 60, 240)
            count: 캔들 개수 (최대 200)
            to: 마지막 캔들 시각 (ISO 8601, 예: "2024-01-01T00:00:00")

        Returns:
            캔들 목록 (최신순 → 역순 정렬하여 시간순 반환)
        """
        params = {
            "market": market,
            "count": min(count, 200),
        }
        if to:
            params["to"] = to

        data = self._get(f"/candles/minutes/{unit}", params)

        candles = []
        for item in data:
            # Upbit API 응답 형식:
            # - candle_date_time_utc: "2024-01-01T00:00:00"
            # - timestamp: 밀리초 타임스탬프
            candle = Candle(
                market=item["market"],
                timestamp=item["timestamp"],
                open=float(item["opening_price"]),
                high=float(item["high_price"]),
                low=float(item["low_price"]),
                close=float(item["trade_price"]),
                volume=float(item["candle_acc_trade_volume"]),
                quote_volume=float(item["candle_acc_trade_price"]),
                timeframe=f"{unit}m",
            )
            candles.append(candle)

        # 최신순으로 오므로 역순 정렬 (시간순)
        candles.reverse()
        return candles

    def get_current_prices(self, markets: List[str]) -> Dict[str, float]:
        """
        현재가 조회

        Args:
            markets: 마켓 코드 목록

        Returns:
            {market: price} 딕셔너리
        """
        if not markets:
            return {}

        # 최대 100개씩 조회
        result = {}
        for i in range(0, len(markets), 100):
            batch = markets[i:i+100]
            params = {"markets": ",".join(batch)}
            data = self._get("/ticker", params)
            for item in data:
                result[item["market"]] = float(item["trade_price"])

        return result

    def get_tickers(self, quote_currency: str = "KRW") -> List[Dict]:
        """
        전체 마켓 티커 조회 (거래대금 포함)

        Args:
            quote_currency: 기준 통화 (KRW, BTC, USDT)

        Returns:
            티커 정보 목록 (acc_trade_price_24h 포함)
        """
        # KRW 마켓 목록 조회
        krw_markets = self.get_krw_markets()
        if not krw_markets:
            return []

        # 티커 조회 (최대 100개씩)
        all_tickers = []
        for i in range(0, len(krw_markets), 100):
            batch = krw_markets[i:i+100]
            params = {"markets": ",".join(batch)}
            data = self._get("/ticker", params)
            all_tickers.extend(data)

        return all_tickers

    def get_candles_days(self, market: str, count: int = 200) -> List[Dict]:
        """
        일봉 캔들 조회 (상장일 확인용)

        Args:
            market: 마켓 코드 (예: "KRW-BTC")
            count: 캔들 개수 (최대 200)

        Returns:
            일봉 캔들 목록
        """
        params = {
            "market": market,
            "count": min(count, 200),
        }
        return self._get("/candles/days", params)

    def get_orderbook(self, markets: List[str]) -> List[Dict]:
        """
        호가 정보 조회

        Args:
            markets: 마켓 코드 목록

        Returns:
            호가 정보 목록
        """
        params = {"markets": ",".join(markets)}
        return self._get("/orderbook", params)

    def close(self) -> None:
        """클라이언트 종료"""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
