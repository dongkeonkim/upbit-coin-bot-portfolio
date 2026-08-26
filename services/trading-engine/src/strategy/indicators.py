"""기술 지표 계산 (Go strategy/momentum.go 하단부와 동일)"""

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class OHLCData:
    """OHLC 데이터 구조체 (Go OHLCData와 동일)"""

    high: float
    low: float
    close: float


@dataclass
class BollingerBands:
    """볼린저 밴드 결과 (Go BollingerBands와 동일)"""

    upper: float
    middle: float
    lower: float


@dataclass
class ADXResult:
    """ADX 결과 (Go ADXResult와 동일)"""

    adx: float
    plus_di: float
    minus_di: float


def calculate_rsi(prices: List[float], period: int) -> float:
    """
    RSI 계산 (Go calculateRSI와 동일)

    Args:
        prices: 종가 히스토리
        period: RSI 기간

    Returns:
        RSI 값 (0-100)
    """
    if len(prices) < period + 1:
        return 50.0  # 데이터 부족 시 중립값

    # 최근 period+1개 가격 사용
    recent_prices = prices[-(period + 1) :]

    gains = 0.0
    losses = 0.0

    for i in range(1, len(recent_prices)):
        change = recent_prices[i] - recent_prices[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change  # 음수를 양수로

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_bollinger_bands(prices: List[float], period: int, std_dev: float) -> Optional[BollingerBands]:
    """
    볼린저 밴드 계산 (Go calculateBollingerBands와 동일)

    Args:
        prices: 종가 히스토리
        period: BB 기간
        std_dev: 표준편차 배수

    Returns:
        볼린저 밴드 또는 None
    """
    if len(prices) < period:
        return None

    # 최근 period개 가격 사용
    recent_prices = prices[-period:]

    # 평균 계산
    total = sum(recent_prices)
    middle = total / period

    # 표준편차 계산
    variance = sum((p - middle) ** 2 for p in recent_prices) / period
    std = math.sqrt(variance)

    return BollingerBands(
        upper=middle + std * std_dev,
        middle=middle,
        lower=middle - std * std_dev,
    )


def calculate_atr(ohlc_history: List[OHLCData], period: int) -> float:
    """
    ATR 계산 (Go calculateATR와 동일)

    Args:
        ohlc_history: OHLC 히스토리
        period: ATR 기간

    Returns:
        ATR 값
    """
    if len(ohlc_history) < period + 1:
        return 0.0

    # 최근 period+1개 OHLC 사용
    recent_ohlc = ohlc_history[-(period + 1) :]

    tr_sum = 0.0
    for i in range(1, len(recent_ohlc)):
        curr = recent_ohlc[i]
        prev = recent_ohlc[i - 1]

        # True Range = max(H-L, |H-prevClose|, |L-prevClose|)
        tr1 = curr.high - curr.low
        tr2 = abs(curr.high - prev.close)
        tr3 = abs(curr.low - prev.close)

        tr = max(tr1, tr2, tr3)
        tr_sum += tr

    return tr_sum / period


def calculate_adx(ohlc_history: List[OHLCData], period: int) -> Optional[ADXResult]:
    """
    ADX 계산 (Go calculateADX와 동일)

    Args:
        ohlc_history: OHLC 히스토리
        period: ADX 기간

    Returns:
        ADX 결과 또는 None
    """
    if len(ohlc_history) < period + 1:
        return None

    # 최근 period+1개 OHLC 사용
    recent_ohlc = ohlc_history[-(period + 1) :]

    smooth_plus_dm = 0.0
    smooth_minus_dm = 0.0
    smooth_tr = 0.0

    for i in range(1, len(recent_ohlc)):
        curr = recent_ohlc[i]
        prev = recent_ohlc[i - 1]

        # +DM, -DM 계산
        up_move = curr.high - prev.high
        down_move = prev.low - curr.low

        plus_dm = 0.0
        minus_dm = 0.0

        if up_move > down_move and up_move > 0:
            plus_dm = up_move
        if down_move > up_move and down_move > 0:
            minus_dm = down_move

        # True Range
        tr1 = curr.high - curr.low
        tr2 = abs(curr.high - prev.close)
        tr3 = abs(curr.low - prev.close)
        tr = max(tr1, tr2, tr3)

        smooth_plus_dm += plus_dm
        smooth_minus_dm += minus_dm
        smooth_tr += tr

    if smooth_tr == 0:
        return None

    # +DI, -DI 계산
    plus_di = (smooth_plus_dm / smooth_tr) * 100.0
    minus_di = (smooth_minus_dm / smooth_tr) * 100.0

    # DX 계산
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return None

    adx = (abs(plus_di - minus_di) / di_sum) * 100.0

    return ADXResult(
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
    )


@dataclass
class OHLCVData:
    """OHLCV 데이터 구조체 (MFI 계산용)"""

    high: float
    low: float
    close: float
    volume: float


def calculate_mfi(ohlcv_history: List[OHLCVData], period: int) -> float:
    """
    MFI (Money Flow Index) 계산

    Args:
        ohlcv_history: OHLCV 히스토리
        period: MFI 기간

    Returns:
        MFI 값 (0-100)
    """
    if len(ohlcv_history) < period + 1:
        return 50.0  # 데이터 부족 시 중립값

    # 최근 period+1개 데이터 사용
    recent_data = ohlcv_history[-(period + 1):]

    positive_flow = 0.0
    negative_flow = 0.0

    for i in range(1, len(recent_data)):
        curr = recent_data[i]
        prev = recent_data[i - 1]

        # Typical Price = (High + Low + Close) / 3
        curr_tp = (curr.high + curr.low + curr.close) / 3
        prev_tp = (prev.high + prev.low + prev.close) / 3

        # Raw Money Flow = Typical Price * Volume
        raw_money_flow = curr_tp * curr.volume

        if curr_tp > prev_tp:
            positive_flow += raw_money_flow
        elif curr_tp < prev_tp:
            negative_flow += raw_money_flow

    if negative_flow == 0:
        return 100.0

    money_ratio = positive_flow / negative_flow
    mfi = 100.0 - (100.0 / (1.0 + money_ratio))

    return mfi


def calculate_sma(prices: List[float], period: int) -> float:
    """
    SMA (Simple Moving Average) 계산

    Args:
        prices: 종가 히스토리
        period: SMA 기간

    Returns:
        SMA 값
    """
    if len(prices) < period:
        return 0.0

    recent_prices = prices[-period:]
    return sum(recent_prices) / period


def calculate_market_slope(prices: List[float], ma_days: int) -> float:
    """
    시장 기울기 계산 (Go calculateMarketSlope와 동일)

    BTC 일봉 이동평균 기울기 계산

    Args:
        prices: BTC 일봉 종가 히스토리
        ma_days: 이동평균 일수

    Returns:
        기울기 (%)
    """
    n = len(prices)
    if n < ma_days or ma_days < 2:
        return 0.0

    # 최근 ma_days일의 가격만 사용
    recent_prices = prices[-ma_days:]
    half = len(recent_prices) // 2

    if half == 0:
        return 0.0

    # 앞쪽 절반의 평균
    ma_start = sum(recent_prices[:half]) / half

    # 뒤쪽 절반의 평균
    ma_end = sum(recent_prices[half:]) / (len(recent_prices) - half)

    if ma_start == 0.0:
        return 0.0

    # 변화율 계산 (기울기)
    return ((ma_end - ma_start) / ma_start) * 100.0
