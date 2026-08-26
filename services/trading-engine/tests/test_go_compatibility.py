"""Go 결과 검증 테스트 - Python 구현이 Go와 동일한 결과를 내는지 확인"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from datetime import datetime
from typing import List, Dict

from src.config.params import Params, FEE_RATE, INITIAL_BALANCE
from src.models.candle import Candle
from src.models.position import PortfolioState, Position
from src.strategy.indicators import (
    OHLCData,
    calculate_rsi,
    calculate_bollinger_bands,
    calculate_atr,
    calculate_adx,
    calculate_market_slope,
)


class TestFeeCalculation(unittest.TestCase):
    """수수료 계산 검증 (Go와 동일한 결과)"""

    def test_fee_rate(self):
        """FEE_RATE = 0.0005 (0.05%)"""
        self.assertEqual(FEE_RATE, 0.0005)

    def test_buy_fee_calculation(self):
        """매수 시 수수료 계산 (Go AddPosition과 동일)"""
        portfolio = PortfolioState(1_000_000.0)

        # 100,000원어치 매수 (가격 1000원)
        volume = 100_000.0 / 1000.0  # 100 코인
        price = 1000.0
        fee_rate = FEE_RATE

        # Go와 동일한 계산
        cost = volume * price  # 100,000
        fee = cost * fee_rate  # 50
        cost_with_fee = cost + fee  # 100,050

        portfolio.add_position("KRW-TEST", volume, price, fee_rate)

        # 잔고 확인: 1,000,000 - 100,050 = 899,950
        self.assertAlmostEqual(portfolio.quote_balance, 899_950.0, places=2)

        # 평균 매수가 확인: 100,050 / 100 = 1,000.5
        pos = portfolio.positions["KRW-TEST"]
        self.assertAlmostEqual(pos.avg_buy_price, 1000.5, places=4)

    def test_sell_fee_calculation(self):
        """매도 시 수수료 계산 (Go RemovePosition과 동일)"""
        portfolio = PortfolioState(1_000_000.0)

        # 먼저 매수
        portfolio.add_position("KRW-TEST", 100.0, 1000.0, FEE_RATE)

        # 매도 (가격 1100원)
        sell_price = 1100.0
        volume = 100.0

        realized_pnl = portfolio.remove_position("KRW-TEST", volume, sell_price, FEE_RATE)

        # Go와 동일한 계산:
        # revenue = 100 * 1100 = 110,000
        # fee = 110,000 * 0.0005 = 55
        # net_revenue = 110,000 - 55 = 109,945
        # cost = 100 * 1000.5 = 100,050
        # realized_pnl = 109,945 - 100,050 = 9,895
        self.assertAlmostEqual(realized_pnl, 9_895.0, places=2)


class TestRSICalculation(unittest.TestCase):
    """RSI 계산 검증 (Go calculateRSI와 동일)"""

    def test_rsi_neutral_on_insufficient_data(self):
        """데이터 부족 시 중립값 50 반환"""
        prices = [100.0, 101.0, 102.0]  # 3개 가격
        rsi = calculate_rsi(prices, period=14)
        self.assertEqual(rsi, 50.0)

    def test_rsi_uptrend(self):
        """상승 추세에서 RSI > 50"""
        # 15일 연속 상승
        prices = [100.0 + i for i in range(16)]
        rsi = calculate_rsi(prices, period=14)
        self.assertGreater(rsi, 50.0)

    def test_rsi_downtrend(self):
        """하락 추세에서 RSI < 50"""
        # 15일 연속 하락
        prices = [100.0 - i for i in range(16)]
        rsi = calculate_rsi(prices, period=14)
        self.assertLess(rsi, 50.0)

    def test_rsi_extreme_up(self):
        """극단적 상승 시 RSI = 100"""
        # 모든 변화가 양수
        prices = [100.0 + i * 10 for i in range(16)]
        rsi = calculate_rsi(prices, period=14)
        self.assertEqual(rsi, 100.0)


class TestBollingerBands(unittest.TestCase):
    """볼린저 밴드 계산 검증 (Go calculateBollingerBands와 동일)"""

    def test_bb_insufficient_data(self):
        """데이터 부족 시 None 반환"""
        prices = [100.0] * 5  # 5개 가격
        bb = calculate_bollinger_bands(prices, period=20, std_dev=2.0)
        self.assertIsNone(bb)

    def test_bb_constant_price(self):
        """일정한 가격일 때 upper = middle = lower"""
        prices = [100.0] * 20
        bb = calculate_bollinger_bands(prices, period=20, std_dev=2.0)

        self.assertIsNotNone(bb)
        self.assertEqual(bb.middle, 100.0)
        self.assertEqual(bb.upper, 100.0)
        self.assertEqual(bb.lower, 100.0)

    def test_bb_volatility(self):
        """변동성이 있을 때 upper > middle > lower"""
        prices = [100.0 + (i % 2) * 10 for i in range(20)]  # 100, 110, 100, 110...
        bb = calculate_bollinger_bands(prices, period=20, std_dev=2.0)

        self.assertIsNotNone(bb)
        self.assertGreater(bb.upper, bb.middle)
        self.assertLess(bb.lower, bb.middle)


class TestATRCalculation(unittest.TestCase):
    """ATR 계산 검증 (Go calculateATR와 동일)"""

    def test_atr_insufficient_data(self):
        """데이터 부족 시 0 반환"""
        ohlc = [OHLCData(high=110, low=90, close=100)]
        atr = calculate_atr(ohlc, period=14)
        self.assertEqual(atr, 0.0)

    def test_atr_basic(self):
        """기본 ATR 계산"""
        ohlc = []
        for i in range(16):
            ohlc.append(OHLCData(high=110.0 + i, low=90.0 + i, close=100.0 + i))

        atr = calculate_atr(ohlc, period=14)
        self.assertGreater(atr, 0.0)


class TestADXCalculation(unittest.TestCase):
    """ADX 계산 검증 (Go calculateADX와 동일)"""

    def test_adx_insufficient_data(self):
        """데이터 부족 시 None 반환"""
        ohlc = [OHLCData(high=110, low=90, close=100)]
        adx = calculate_adx(ohlc, period=14)
        self.assertIsNone(adx)

    def test_adx_uptrend(self):
        """상승 추세에서 +DI > -DI"""
        ohlc = []
        for i in range(16):
            # 고가/저가 모두 상승
            ohlc.append(OHLCData(high=110.0 + i * 2, low=90.0 + i * 2, close=100.0 + i * 2))

        adx = calculate_adx(ohlc, period=14)
        self.assertIsNotNone(adx)
        self.assertGreater(adx.plus_di, adx.minus_di)


class TestMarketSlope(unittest.TestCase):
    """시장 기울기 계산 검증 (Go calculateMarketSlope와 동일)"""

    def test_slope_insufficient_data(self):
        """데이터 부족 시 0 반환"""
        prices = [100.0, 101.0]
        slope = calculate_market_slope(prices, ma_days=5)
        self.assertEqual(slope, 0.0)

    def test_slope_uptrend(self):
        """상승 추세에서 양수 기울기"""
        prices = [100.0, 102.0, 104.0, 106.0, 108.0]
        slope = calculate_market_slope(prices, ma_days=4)
        self.assertGreater(slope, 0.0)

    def test_slope_downtrend(self):
        """하락 추세에서 음수 기울기"""
        prices = [108.0, 106.0, 104.0, 102.0, 100.0]
        slope = calculate_market_slope(prices, ma_days=4)
        self.assertLess(slope, 0.0)


class TestCandleAggregation(unittest.TestCase):
    """캔들 집계 검증 (Go mergeCandles와 동일)"""

    def test_aggregate_basic(self):
        """기본 캔들 집계"""
        candles = [
            Candle(
                market="KRW-BTC",
                timeframe="1m",
                timestamp=1000,
                open=100.0,
                high=110.0,
                low=95.0,
                close=105.0,
                volume=10.0,
                quote_volume=1000.0,
            ),
            Candle(
                market="KRW-BTC",
                timeframe="1m",
                timestamp=2000,
                open=105.0,
                high=115.0,
                low=100.0,
                close=112.0,
                volume=15.0,
                quote_volume=1500.0,
            ),
            Candle(
                market="KRW-BTC",
                timeframe="1m",
                timestamp=3000,
                open=112.0,
                high=120.0,
                low=108.0,
                close=118.0,
                volume=20.0,
                quote_volume=2000.0,
            ),
        ]

        aggregated = Candle.aggregate(candles, "3m")

        # Go와 동일한 결과 검증
        self.assertEqual(aggregated.market, "KRW-BTC")
        self.assertEqual(aggregated.timeframe, "3m")
        self.assertEqual(aggregated.timestamp, 1000)  # 첫 캔들 시간
        self.assertEqual(aggregated.open, 100.0)  # 첫 캔들 open
        self.assertEqual(aggregated.high, 120.0)  # 최고가
        self.assertEqual(aggregated.low, 95.0)  # 최저가
        self.assertEqual(aggregated.close, 118.0)  # 마지막 캔들 close
        self.assertEqual(aggregated.volume, 45.0)  # 합계
        self.assertEqual(aggregated.quote_volume, 4500.0)  # 합계


class TestChangePercent(unittest.TestCase):
    """변화율 계산 검증"""

    def test_change_percent(self):
        """캔들 변화율 계산"""
        candle = Candle(
            market="KRW-BTC",
            timeframe="1m",
            timestamp=1000,
            open=100.0,
            high=110.0,
            low=95.0,
            close=105.0,
            volume=10.0,
            quote_volume=1000.0,
        )

        change = candle.change_percent()
        # (105 - 100) / 100 * 100 = 5.0%
        self.assertEqual(change, 5.0)


class TestPortfolioState(unittest.TestCase):
    """포트폴리오 상태 검증 (Go PortfolioState와 동일)"""

    def test_initial_state(self):
        """초기 상태"""
        portfolio = PortfolioState(INITIAL_BALANCE)
        self.assertEqual(portfolio.quote_balance, INITIAL_BALANCE)
        self.assertEqual(len(portfolio.positions), 0)

    def test_profit_percent(self):
        """수익률 계산"""
        portfolio = PortfolioState(1_000_000.0)
        portfolio.add_position("KRW-TEST", 100.0, 1000.0, FEE_RATE)

        # 평균 매수가: 1000.5 (수수료 포함)
        # 현재가 1100일 때 수익률: (1100 - 1000.5) / 1000.5 * 100
        profit = portfolio.get_profit_percent("KRW-TEST", 1100.0)
        expected = ((1100.0 - 1000.5) / 1000.5) * 100
        self.assertAlmostEqual(profit, expected, places=4)

    def test_total_value(self):
        """총 자산 평가액"""
        portfolio = PortfolioState(1_000_000.0)
        portfolio.add_position("KRW-TEST", 100.0, 1000.0, FEE_RATE)

        # 잔고: 899,950
        # 포지션 가치: 100 * 1100 = 110,000
        # 총: 1,009,950
        total = portfolio.get_total_value({"KRW-TEST": 1100.0})
        self.assertAlmostEqual(total, 1_009_950.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
