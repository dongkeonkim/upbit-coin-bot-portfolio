"""시간 흐름 시뮬레이터 (Go simulation/simulator.go와 동일)"""

import calendar
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from ..config.params import Params, LONG_TIMEFRAME_MINUTES, TRADE_CHECK_TIMEFRAME_MINUTES
from ..models.candle import Candle

# 24시간 누적 거래대금 계산용 윈도우 크기 (분) - Go와 동일
ACC_24H_WINDOW_MINUTES = 1440


class TimeSimulator:
    """시간 흐름 시뮬레이션 (슬라이스 기반 Sliding/Tumbling 윈도우) (Go TimeSimulator와 동일)"""

    def __init__(
        self,
        start_time: datetime,
        end_time: datetime,
        candles_1m: Dict[str, List[Candle]],
        params: Params,
    ):
        self.start_time = start_time
        self.end_time = end_time
        self.current_time = start_time
        self.params = params

        # 1분봉 데이터 (인덱스 기반 접근)
        self.candles_1m = candles_1m
        self.candle_indices_1m: Dict[str, int] = {}

        # 윈도우 버퍼 (슬라이스 기반)
        # 거래량 평균 계산용: Sliding Window
        self.window_volume_avg: Dict[str, List[Candle]] = {}
        # 단기 타임프레임: 파라미터 기반 (Sliding or Tumbling)
        self.window_short: Dict[str, List[Candle]] = {}
        # 장기 타임프레임: Sliding Window
        self.window_long: Dict[str, List[Candle]] = {}
        # 매수 체크용 타임프레임: Sliding Window
        self.window_trade_check: Dict[str, List[Candle]] = {}
        # 24시간 누적 거래대금용: Sliding Window (TOP 10 계산용) - Go와 동일
        self.window_24h: Dict[str, List[Candle]] = {}
        # 24시간 누적 거래대금 캐시 (O(1) 업데이트용) - Go와 동일
        self.acc_24h_volume: Dict[str, float] = {}

        # Tumbling Window용 카운터 (단기봉만)
        self.tumbling_counters: Dict[str, int] = {}

        # 각 마켓별 윈도우 초기화
        for market in candles_1m:
            self.window_volume_avg[market] = []
            self.window_short[market] = []
            self.window_long[market] = []
            self.window_trade_check[market] = []
            self.window_24h[market] = []
            self.acc_24h_volume[market] = 0.0
            self.candle_indices_1m[market] = 0
            self.tumbling_counters[market] = 0

    def get_candles_at_time(
        self, timestamp_ms: int
    ) -> Tuple[
        Dict[str, Candle],  # candles_1m
        Dict[str, Candle],  # candles_trade_check
        Dict[str, Candle],  # candles_short
        Dict[str, Candle],  # candles_long
    ]:
        """특정 시간의 모든 마켓 캔들 반환 (Go GetCandlesAtTime와 동일)"""
        DELAY_1M = 60 * 1000  # 1분 = 60초

        candles_1m_result: Dict[str, Candle] = {}
        candles_trade_check_result: Dict[str, Candle] = {}
        candles_short_result: Dict[str, Candle] = {}
        candles_long_result: Dict[str, Candle] = {}

        # 1. 확정된 1분봉 가져오기 (1분 delay)
        self._get_confirmed_1m_candles_into(candles_1m_result, timestamp_ms, DELAY_1M)

        # 2. 각 마켓의 윈도우 업데이트
        for market, candle in candles_1m_result.items():
            maxlen_volume_avg = self.params.volume_avg_window_minutes
            maxlen_long = LONG_TIMEFRAME_MINUTES
            maxlen_trade_check = TRADE_CHECK_TIMEFRAME_MINUTES

            # 거래량 평균 윈도우 (Sliding)
            self.window_volume_avg[market].append(candle)
            if len(self.window_volume_avg[market]) > maxlen_volume_avg:
                self.window_volume_avg[market] = self.window_volume_avg[market][1:]

            # 장기 윈도우 (Sliding)
            self.window_long[market].append(candle)
            if len(self.window_long[market]) > maxlen_long:
                self.window_long[market] = self.window_long[market][1:]

            # 매수 체크 윈도우 (Sliding)
            self.window_trade_check[market].append(candle)
            if len(self.window_trade_check[market]) > maxlen_trade_check:
                self.window_trade_check[market] = self.window_trade_check[market][1:]

            # Short Window (Sliding or Tumbling)
            maxlen_short = self.params.short_timeframe_minutes
            if self.params.buy_stage1_sliding_window:
                # Sliding: 슬라이스로 최근 N개 유지
                self.window_short[market].append(candle)
                if len(self.window_short[market]) > maxlen_short:
                    self.window_short[market] = self.window_short[market][1:]
            else:
                # Tumbling: 정확히 N개일 때만 생성하고 클리어
                self.window_short[market].append(candle)
                self.tumbling_counters[market] += 1

                # N개가 쌓이면 다음에 Clear 준비
                if self.tumbling_counters[market] > maxlen_short:
                    self.window_short[market] = [candle]  # 새 윈도우 시작
                    self.tumbling_counters[market] = 1

            # 24시간 누적 거래대금 윈도우 (Sliding, O(1) 업데이트) - Go와 동일
            window_24h = self.window_24h[market]
            # 윈도우가 가득 찼으면 가장 오래된 것을 빼고
            if len(window_24h) >= ACC_24H_WINDOW_MINUTES:
                self.acc_24h_volume[market] -= window_24h[0].quote_volume
                self.window_24h[market] = window_24h[1:]
            # 새 캔들 추가
            self.window_24h[market].append(candle)
            self.acc_24h_volume[market] += candle.quote_volume

        # 3. 집계된 캔들 생성
        for market in candles_1m_result:
            # 단기 타임프레임: 파라미터 기반 (Sliding or Tumbling)
            if self.params.buy_stage1_sliding_window:
                # Sliding: 최근 N개가 모두 있을 때만 생성
                if len(self.window_short[market]) >= self.params.short_timeframe_minutes:
                    candles_short_result[market] = self._merge_candles(
                        self.window_short[market],
                        f"{self.params.short_timeframe_minutes}m",
                    )
            else:
                # Tumbling: 정확히 N개일 때만 생성
                if (
                    self.tumbling_counters[market]
                    == self.params.short_timeframe_minutes
                ):
                    candles_short_result[market] = self._merge_candles(
                        self.window_short[market],
                        f"{self.params.short_timeframe_minutes}m",
                    )

            # 장기 타임프레임: 최근 N개 1분봉이 모두 있을 때만 생성 (Sliding)
            if len(self.window_long[market]) >= LONG_TIMEFRAME_MINUTES:
                candles_long_result[market] = self._merge_candles(
                    self.window_long[market], f"{LONG_TIMEFRAME_MINUTES}m"
                )

            # 매수 체크용 타임프레임: 최근 N개 1분봉이 모두 있을 때만 생성 (Sliding)
            if len(self.window_trade_check[market]) >= TRADE_CHECK_TIMEFRAME_MINUTES:
                candles_trade_check_result[market] = self._merge_candles(
                    self.window_trade_check[market], f"{TRADE_CHECK_TIMEFRAME_MINUTES}m"
                )

        return (
            candles_1m_result,
            candles_trade_check_result,
            candles_short_result,
            candles_long_result,
        )

    def _get_confirmed_1m_candles_into(
        self, result: Dict[str, Candle], timestamp_ms: int, delay_ms: int
    ) -> None:
        """확정된 1분봉 캔들 조회 (Look-ahead bias 방지) (Go getConfirmed1mCandlesInto와 동일)"""
        adjusted_timestamp_ms = timestamp_ms - delay_ms

        for market, candles in self.candles_1m.items():
            idx = self.candle_indices_1m[market]
            candles_len = len(candles)

            # 최적화: 대부분의 경우 다음 인덱스만 확인하면 됨 (1분씩 진행)
            if (
                idx + 1 < candles_len
                and candles[idx + 1].timestamp <= adjusted_timestamp_ms
            ):
                idx += 1
                # 연속된 여러 캔들이 있을 수 있으므로 while 루프로 처리
                while (
                    idx + 1 < candles_len
                    and candles[idx + 1].timestamp <= adjusted_timestamp_ms
                ):
                    idx += 1
            elif idx < candles_len and candles[idx].timestamp > adjusted_timestamp_ms:
                # 시간이 뒤로 갔거나 처음 시작하는 경우 (드문 경우)
                while idx > 0 and candles[idx].timestamp > adjusted_timestamp_ms:
                    idx -= 1

            # 인덱스 업데이트
            self.candle_indices_1m[market] = idx

            # 확정된 캔들이 있으면 결과 맵에 추가
            if idx < candles_len and candles[idx].timestamp <= adjusted_timestamp_ms:
                result[market] = candles[idx]

    def _merge_candles(self, candles: List[Candle], timeframe: str) -> Candle:
        """슬라이스의 1분봉을 하나의 캔들로 병합 (Go mergeCandles와 동일)"""
        if len(candles) == 0:
            raise ValueError("cannot merge empty candles")

        first = candles[0]
        open_price = first.open
        high = first.high
        low = first.low
        close = first.close
        volume = first.volume
        quote_volume = first.quote_volume

        # 슬라이스 순회
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

    def advance(self, minutes: int) -> None:
        """시간 진행 (Go Advance와 동일)"""
        self.current_time = self.current_time + timedelta(minutes=minutes)

    def is_finished(self) -> bool:
        """백테스트 종료 여부 (Go IsFinished와 동일)"""
        return self.current_time >= self.end_time

    def get_progress_percent(self) -> float:
        """진행률 (%) (Go GetProgressPercent와 동일)"""
        total_seconds = (self.end_time - self.start_time).total_seconds()
        elapsed_seconds = (self.current_time - self.start_time).total_seconds()
        if total_seconds <= 0:
            return 100.0
        return (elapsed_seconds / total_seconds) * 100

    def get_avg_quote_volume(self, market: str) -> float:
        """거래량 평균 계산 (Sliding Window) (Go GetAvgQuoteVolume와 동일)"""
        window = self.window_volume_avg.get(market, [])

        # N개 1분봉이 모두 채워지지 않았으면 0 반환
        if len(window) < self.params.volume_avg_window_minutes:
            return 0.0

        # 평균 계산
        total_quote_volume = sum(c.quote_volume for c in window)
        return total_quote_volume / self.params.volume_avg_window_minutes

    def get_current_timestamp_ms(self) -> int:
        """현재 시간 (밀리초 타임스탬프) (Go GetCurrentTimestampMs와 동일)"""
        # calendar.timegm()은 UTC 기준 타임스탬프 반환 (DB와 동일)
        # datetime.timestamp()는 로컬 타임존 기준이라 KST에서 9시간 차이 발생
        return calendar.timegm(self.current_time.timetuple()) * 1000

    def get_current_time(self) -> datetime:
        """현재 시간 반환 (Go GetCurrentTime과 동일)"""
        return self.current_time

    def get_top10_markets(self) -> Dict[str, bool]:
        """24시간 누적 거래대금 기준 상위 10개 마켓 반환 (Go GetTop10Markets와 동일)"""
        # 데이터가 있는 마켓만 (volume > 0)
        volumes = [
            (market, volume)
            for market, volume in self.acc_24h_volume.items()
            if volume > 0
        ]

        # 거래대금 기준 내림차순 정렬
        volumes.sort(key=lambda x: x[1], reverse=True)

        # 상위 10개 선택
        count = min(10, len(volumes))
        return {volumes[i][0]: True for i in range(count)}

    def get_acc_24h_volume(self, market: str) -> float:
        """특정 마켓의 24시간 누적 거래대금 반환 (Go GetAcc24hVolume과 동일)"""
        return self.acc_24h_volume.get(market, 0.0)
