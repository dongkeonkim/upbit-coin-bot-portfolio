"""실시간 캔들 버퍼 (메모리 기반 슬라이딩 윈도우)"""

from collections import deque
from typing import Dict, List, Optional, Tuple

from ..config.params import Params, LONG_TIMEFRAME_MINUTES, TRADE_CHECK_TIMEFRAME_MINUTES
from ..models.candle import Candle


class CandleBuffer:
    """
    실시간 캔들 버퍼 (TimeSimulator의 실시간 버전)

    백테스트의 TimeSimulator와 동일한 윈도우 로직을 사용하되,
    실시간으로 들어오는 캔들을 버퍼링합니다.
    """

    def __init__(self, params: Params):
        """
        캔들 버퍼 초기화

        Args:
            params: 전략 파라미터
        """
        self.params = params

        # 각 마켓별 1분봉 버퍼
        self._candles_1m: Dict[str, deque] = {}

        # 윈도우 버퍼 (슬라이스 기반)
        self._window_volume_avg: Dict[str, deque] = {}
        self._window_short: Dict[str, deque] = {}
        self._window_long: Dict[str, deque] = {}
        self._window_trade_check: Dict[str, deque] = {}

        # Tumbling Window용 카운터
        self._tumbling_counters: Dict[str, int] = {}

        # 마지막으로 처리한 타임스탬프 (중복 방지)
        self._last_timestamps: Dict[str, int] = {}

    def _ensure_market(self, market: str) -> None:
        """마켓 버퍼 초기화"""
        if market not in self._candles_1m:
            # 최대 버퍼 크기 (가장 긴 윈도우 + 여유분)
            max_size = max(
                self.params.volume_avg_window_minutes,
                LONG_TIMEFRAME_MINUTES,
                TRADE_CHECK_TIMEFRAME_MINUTES,
            ) + 10

            self._candles_1m[market] = deque(maxlen=max_size)
            self._window_volume_avg[market] = deque(maxlen=self.params.volume_avg_window_minutes)
            self._window_short[market] = deque(maxlen=self.params.short_timeframe_minutes * 2)
            self._window_long[market] = deque(maxlen=LONG_TIMEFRAME_MINUTES)
            self._window_trade_check[market] = deque(maxlen=TRADE_CHECK_TIMEFRAME_MINUTES)
            self._tumbling_counters[market] = 0
            self._last_timestamps[market] = 0

    def add_candle(self, candle: Candle) -> bool:
        """
        새 캔들 추가

        Args:
            candle: 1분봉 캔들

        Returns:
            True if 새 캔들이 추가됨, False if 중복
        """
        market = candle.market
        self._ensure_market(market)

        # 중복 체크 (candle_time 기준)
        candle_time = candle.candle_time or candle.timestamp
        if candle_time <= self._last_timestamps[market]:
            return False

        self._last_timestamps[market] = candle_time

        # 1분봉 버퍼에 추가
        self._candles_1m[market].append(candle)

        # 거래량 평균 윈도우 (Sliding)
        self._window_volume_avg[market].append(candle)

        # 장기 윈도우 (Sliding)
        self._window_long[market].append(candle)

        # 매수 체크 윈도우 (Sliding)
        self._window_trade_check[market].append(candle)

        # Short Window (Sliding or Tumbling)
        maxlen_short = self.params.short_timeframe_minutes
        if self.params.buy_stage1_sliding_window:
            # Sliding: 최근 N개 유지
            self._window_short[market].append(candle)
            # deque maxlen으로 자동 관리되지만 명시적으로 제한
            while len(self._window_short[market]) > maxlen_short:
                self._window_short[market].popleft()
        else:
            # Tumbling: N개가 쌓이면 클리어
            self._window_short[market].append(candle)
            self._tumbling_counters[market] += 1

            if self._tumbling_counters[market] > maxlen_short:
                self._window_short[market].clear()
                self._window_short[market].append(candle)
                self._tumbling_counters[market] = 1

        return True

    def add_candles(self, candles: List[Candle]) -> int:
        """
        여러 캔들 추가 (초기화용)

        Args:
            candles: 캔들 목록 (시간순 정렬)

        Returns:
            추가된 캔들 수
        """
        count = 0
        for candle in candles:
            if self.add_candle(candle):
                count += 1
        return count

    def get_candles(
        self, markets: Optional[List[str]] = None
    ) -> Tuple[
        Dict[str, Candle],  # candles_1m (최신 1분봉)
        Dict[str, Candle],  # candles_trade_check
        Dict[str, Candle],  # candles_short
        Dict[str, Candle],  # candles_long
    ]:
        """
        현재 캔들 데이터 반환 (TimeSimulator.get_candles_at_time과 동일한 형식)

        Args:
            markets: 조회할 마켓 목록 (None이면 전체)

        Returns:
            (candles_1m, candles_trade_check, candles_short, candles_long)
        """
        if markets is None:
            markets = list(self._candles_1m.keys())

        candles_1m_result: Dict[str, Candle] = {}
        candles_trade_check_result: Dict[str, Candle] = {}
        candles_short_result: Dict[str, Candle] = {}
        candles_long_result: Dict[str, Candle] = {}

        for market in markets:
            if market not in self._candles_1m:
                continue

            # 최신 1분봉
            if self._candles_1m[market]:
                candles_1m_result[market] = self._candles_1m[market][-1]

            # 단기 타임프레임
            if self.params.buy_stage1_sliding_window:
                # Sliding: 최근 N개가 모두 있을 때만 생성
                if len(self._window_short[market]) >= self.params.short_timeframe_minutes:
                    candles_short_result[market] = self._merge_candles(
                        list(self._window_short[market])[-self.params.short_timeframe_minutes:],
                        f"{self.params.short_timeframe_minutes}m",
                    )
            else:
                # Tumbling: 정확히 N개일 때만 생성
                if self._tumbling_counters[market] == self.params.short_timeframe_minutes:
                    candles_short_result[market] = self._merge_candles(
                        list(self._window_short[market]),
                        f"{self.params.short_timeframe_minutes}m",
                    )

            # 장기 타임프레임
            if len(self._window_long[market]) >= LONG_TIMEFRAME_MINUTES:
                candles_long_result[market] = self._merge_candles(
                    list(self._window_long[market])[-LONG_TIMEFRAME_MINUTES:],
                    f"{LONG_TIMEFRAME_MINUTES}m",
                )

            # 매수 체크용 타임프레임
            if len(self._window_trade_check[market]) >= TRADE_CHECK_TIMEFRAME_MINUTES:
                candles_trade_check_result[market] = self._merge_candles(
                    list(self._window_trade_check[market])[-TRADE_CHECK_TIMEFRAME_MINUTES:],
                    f"{TRADE_CHECK_TIMEFRAME_MINUTES}m",
                )

        return (
            candles_1m_result,
            candles_trade_check_result,
            candles_short_result,
            candles_long_result,
        )

    def get_avg_quote_volume(self, market: str) -> float:
        """
        거래량 평균 계산 (Sliding Window)

        Args:
            market: 마켓 코드

        Returns:
            평균 거래대금 (윈도우 미충족시 0.0)
        """
        if market not in self._window_volume_avg:
            return 0.0

        window = self._window_volume_avg[market]
        if len(window) < self.params.volume_avg_window_minutes:
            return 0.0

        total_quote_volume = sum(c.quote_volume for c in window)
        return total_quote_volume / self.params.volume_avg_window_minutes

    def get_avg_quote_volumes(self, markets: Optional[List[str]] = None) -> Dict[str, float]:
        """
        모든 마켓의 평균 거래대금 반환

        Args:
            markets: 조회할 마켓 목록 (None이면 전체)

        Returns:
            {market: avg_quote_volume} 딕셔너리
        """
        if markets is None:
            markets = list(self._candles_1m.keys())

        return {market: self.get_avg_quote_volume(market) for market in markets}

    def get_current_prices(self) -> Dict[str, float]:
        """현재가 반환 (최신 1분봉 종가)"""
        return {
            market: candles[-1].close
            for market, candles in self._candles_1m.items()
            if candles
        }

    def get_markets(self) -> List[str]:
        """버퍼에 있는 마켓 목록"""
        return list(self._candles_1m.keys())

    def is_ready(self, market: str) -> bool:
        """
        마켓 데이터가 전략 분석 가능한 상태인지 확인

        Args:
            market: 마켓 코드

        Returns:
            True if 충분한 데이터가 쌓임
        """
        if market not in self._candles_1m:
            return False

        # 최소 장기 윈도우만큼 데이터 필요
        return len(self._window_long.get(market, [])) >= LONG_TIMEFRAME_MINUTES

    def _merge_candles(self, candles: List[Candle], timeframe: str) -> Candle:
        """캔들 병합 (TimeSimulator._merge_candles와 동일)"""
        if len(candles) == 0:
            raise ValueError("cannot merge empty candles")

        first = candles[0]
        open_price = first.open
        high = first.high
        low = first.low
        close = first.close
        volume = first.volume
        quote_volume = first.quote_volume

        for candle in candles[1:]:
            if candle.high > high:
                high = candle.high
            if candle.low < low:
                low = candle.low
            close = candle.close
            volume += candle.volume
            quote_volume += candle.quote_volume

        return Candle(
            market=first.market,
            timeframe=timeframe,
            timestamp=first.timestamp,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            quote_volume=quote_volume,
        )

    def clear(self, market: Optional[str] = None) -> None:
        """
        버퍼 초기화

        Args:
            market: 특정 마켓만 초기화 (None이면 전체)
        """
        if market:
            if market in self._candles_1m:
                self._candles_1m[market].clear()
                self._window_volume_avg[market].clear()
                self._window_short[market].clear()
                self._window_long[market].clear()
                self._window_trade_check[market].clear()
                self._tumbling_counters[market] = 0
                self._last_timestamps[market] = 0
        else:
            self._candles_1m.clear()
            self._window_volume_avg.clear()
            self._window_short.clear()
            self._window_long.clear()
            self._window_trade_check.clear()
            self._tumbling_counters.clear()
            self._last_timestamps.clear()
