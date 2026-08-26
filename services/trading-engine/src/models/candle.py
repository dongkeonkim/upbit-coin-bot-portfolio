"""캔들 데이터 구조 (Go database/db.go CandleData와 동일)"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    """OHLCV 캔들 데이터"""

    market: str  # 마켓 심볼 (예: "KRW-BTC")
    timestamp: int  # API 응답시간
    open: float  # 시가
    high: float  # 고가
    low: float  # 저가
    close: float  # 종가
    volume: float  # 거래량 (코인)
    quote_volume: float  # 거래대금 (KRW)
    timeframe: str = "1m"  # 타임프레임
    candle_time: Optional[int] = None  # 캔들 시작시간 (피처 계산, stale 체크용)

    def change_percent(self) -> float:
        """시가 대비 종가 변화율 (%)"""
        if self.open == 0:
            return 0.0
        return ((self.close - self.open) / self.open) * 100.0

    def range_percent(self) -> float:
        """캔들 레인지 (고가-저가) / 시가 (%)"""
        if self.open == 0:
            return 0.0
        return ((self.high - self.low) / self.open) * 100.0

    @classmethod
    def aggregate(cls, candles: list["Candle"], timeframe: str) -> "Candle":
        """여러 캔들을 하나로 집계 (Go mergeCandles와 동일)"""
        if not candles:
            raise ValueError("cannot aggregate empty candles")

        first = candles[0]
        return cls(
            market=first.market,
            timestamp=first.timestamp,  # 첫 캔들의 타임스탬프 (Go와 동일)
            open=first.open,
            high=max(c.high for c in candles),
            low=min(c.low for c in candles),
            close=candles[-1].close,
            volume=sum(c.volume for c in candles),
            quote_volume=sum(c.quote_volume for c in candles),
            timeframe=timeframe,
        )

    def __repr__(self) -> str:
        return (
            f"Candle({self.market}, {self.timestamp}, "
            f"O={self.open:.2f}, H={self.high:.2f}, L={self.low:.2f}, C={self.close:.2f}, "
            f"V={self.quote_volume:.0f})"
        )
