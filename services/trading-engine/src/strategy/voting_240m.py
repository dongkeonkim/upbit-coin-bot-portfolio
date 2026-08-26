"""
Voting 240m 전략 (17개 전략 투표 기반)

- 1분봉을 240분봉으로 내부 집계
- 17개 전략 신호 투표
- 4개 이상 동시 신호 시 매수
- 트레일링 스탑: +0.5% 활성화, -0.3% 하락 시 매도
- TP: +1.5%, SL: -1.0%, Hold: 180분
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import numpy as np

from ..config.params import Params, FEE_RATE
from ..models.candle import Candle
from ..models.position import PortfolioState
from ..models.signal import Signal, SignalAction, BuyReason, SellReason


# === 전략 파라미터 ===
VOTE_THRESHOLD = 4          # 최소 투표 수
TP_PCT = 1.5                # 익절 %
SL_PCT = 1.0                # 손절 %
HOLD_MINUTES = 180          # 최대 홀딩 시간 (분)
TRAIL_ACTIVATION = 0.5      # 트레일링 활성화 (%)
TRAIL_DISTANCE = 0.3        # 트레일링 거리 (%)
TF_MINUTES = 240            # 집계 타임프레임 (분)
MIN_BARS = 50               # 최소 필요 봉 수


@dataclass
class Bar240m:
    """240분봉 데이터"""
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime


@dataclass
class Voting240mState:
    """포지션별 상태 추적"""
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    peak_price: float = 0.0
    peak_profit: float = 0.0
    trailing_active: bool = False
    accumulated_fee: float = 0.0


class Voting240mStrategy:
    """
    240분봉 기반 17개 전략 투표 시스템

    매수 조건: 17개 전략 중 4개 이상 동시 신호
    매도 조건:
      1. 트레일링 스탑: +0.5% 도달 후 -0.3% 하락
      2. 익절: +1.5%
      3. 손절: -1.0%
      4. 타임아웃: 180분
    """

    def __init__(self, params: Params, redis_portfolio=None):
        self.params = params
        self.redis_portfolio = redis_portfolio

        # 캔들 데이터
        self.candles_1m: Dict[str, Candle] = {}

        # 240분봉 집계용
        self.bars_240m: Dict[str, List[Bar240m]] = {}
        self.current_bar: Dict[str, Optional[Bar240m]] = {}
        self.bar_candle_count: Dict[str, int] = {}
        self.new_bar_completed: bool = False  # 새 봉 완성 여부 (매수 체크 최적화)

        # 포지션 상태
        self.position_states: Dict[str, Voting240mState] = {}

        # 거래대금 (TOP10 필터용)
        self.avg_quote_volumes: Dict[str, float] = {}

        # 현재 시간
        self.current_time: Optional[datetime] = None

    def update_candles(
        self,
        candles_1m: Dict[str, Candle],
        candles_trade_check: Dict[str, Candle],
        candles_short: Dict[str, Candle],
        candles_long: Dict[str, Candle],
        avg_quote_volumes: Dict[str, float],
    ) -> None:
        """캔들 데이터 업데이트 및 240분봉 집계"""
        self.candles_1m = candles_1m
        self.avg_quote_volumes = avg_quote_volumes

        # 각 마켓별로 1분봉을 240분봉으로 집계
        for market, candle in candles_1m.items():
            self._aggregate_to_240m(market, candle)

    def _aggregate_to_240m(self, market: str, candle: Candle) -> None:
        """1분봉을 240분봉으로 집계"""
        # 초기화
        if market not in self.bars_240m:
            self.bars_240m[market] = []
            self.current_bar[market] = None
            self.bar_candle_count[market] = 0

        # 새 봉 시작 조건: 현재 봉이 없거나 TF_MINUTES개 캔들 수집 완료
        if self.current_bar[market] is None:
            self.current_bar[market] = Bar240m(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                timestamp=candle.candle_time or candle.timestamp,
            )
            self.bar_candle_count[market] = 1
        else:
            # 기존 봉 업데이트
            bar = self.current_bar[market]
            bar.high = max(bar.high, candle.high)
            bar.low = min(bar.low, candle.low)
            bar.close = candle.close
            bar.volume += candle.volume
            self.bar_candle_count[market] += 1

        # 240분(TF_MINUTES) 완료 시 봉 확정
        if self.bar_candle_count[market] >= TF_MINUTES:
            self.bars_240m[market].append(self.current_bar[market])
            self.current_bar[market] = None
            self.bar_candle_count[market] = 0

            # 최대 300개 유지
            if len(self.bars_240m[market]) > 300:
                self.bars_240m[market] = self.bars_240m[market][-300:]

    def update_btc_history(self, current_time: datetime) -> None:
        """BTC 히스토리 업데이트"""
        self.current_time = current_time

    def analyze_buy(
        self, portfolio: PortfolioState, current_time: datetime
    ) -> Optional[Signal]:
        """매수 신호 분석"""
        self.current_time = current_time

        # 최소 잔고 체크
        if portfolio.quote_balance < 100000.0:
            return None

        # TOP10 마켓에서 투표
        best_market = None
        best_votes = 0

        for market in self.avg_quote_volumes.keys():
            # 이미 보유 중인 코인은 스킵
            if market in portfolio.positions:
                continue

            # 240분봉 히스토리가 충분한지 체크
            if market not in self.bars_240m or len(self.bars_240m[market]) < MIN_BARS:
                continue

            # 17개 전략 투표
            votes = self._count_votes(market)
            if votes >= VOTE_THRESHOLD and votes > best_votes:
                best_votes = votes
                best_market = market

        if best_market is None:
            return None

        # 현재 가격
        candle = self.candles_1m.get(best_market)
        if candle is None:
            return None

        current_price = candle.close
        investment_amount = portfolio.quote_balance * self.params.investment_ratio

        return Signal(
            action=SignalAction.BUY,
            market=best_market,
            reason=BuyReason.MOMENTUM_SURGE,
            price=current_price,
            amount=investment_amount,
        )

    def analyze_sell(self, portfolio: PortfolioState) -> List[Signal]:
        """매도 신호 분석"""
        signals: List[Signal] = []

        for market, position in portfolio.positions.items():
            candle = self.candles_1m.get(market)
            if candle is None:
                continue

            current_price = candle.close
            state = self.position_states.get(market)
            if state is None:
                continue

            # 수익률 계산
            profit_pct = ((current_price - state.entry_price) / state.entry_price) * 100

            # 1. 손절 체크 (-1.0%)
            if profit_pct <= -SL_PCT:
                signals.append(Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.STOP_LOSS,
                    price=current_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_pct,
                ))
                continue

            # 2. 익절 체크 (+1.5%)
            if profit_pct >= TP_PCT:
                signals.append(Signal(
                    action=SignalAction.SELL,
                    market=market,
                    reason=SellReason.TAKE_PROFIT,
                    price=current_price,
                    volume=position.volume,
                    avg_buy_price=position.avg_buy_price,
                    profit_percent=profit_pct,
                ))
                continue

            # 3. 트레일링 스탑 체크
            if current_price > state.peak_price:
                state.peak_price = current_price
                state.peak_profit = profit_pct

            if not state.trailing_active and profit_pct >= TRAIL_ACTIVATION:
                state.trailing_active = True

            if state.trailing_active:
                drop_from_peak = state.peak_profit - profit_pct
                if drop_from_peak >= TRAIL_DISTANCE:
                    signals.append(Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.TRAILING_STOP,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_pct,
                    ))
                    continue

            # 4. 타임아웃 체크 (180분)
            if state.entry_time is not None and self.current_time is not None:
                hold_minutes = (self.current_time - state.entry_time).total_seconds() / 60
                if hold_minutes >= HOLD_MINUTES:
                    signals.append(Signal(
                        action=SignalAction.SELL,
                        market=market,
                        reason=SellReason.HOLD_TIMEOUT,
                        price=current_price,
                        volume=position.volume,
                        avg_buy_price=position.avg_buy_price,
                        profit_percent=profit_pct,
                    ))
                    continue

        return signals

    def update_strategy_state(
        self,
        portfolio: PortfolioState,
        candles_1m: Dict[str, Candle],
        current_date: str,
        top10_markets: Dict[str, bool],
    ) -> None:
        """전략 상태 업데이트"""
        for market, position in portfolio.positions.items():
            candle = candles_1m.get(market)
            if candle is None:
                continue

            state = self.position_states.get(market)
            if state is not None:
                if candle.high > state.peak_price:
                    state.peak_price = candle.high
                    profit_pct = ((candle.high - state.entry_price) / state.entry_price) * 100
                    state.peak_profit = profit_pct

    def on_position_opened(self, market: str, price: float) -> None:
        """포지션 오픈 시 호출"""
        self.position_states[market] = Voting240mState(
            entry_price=price,
            entry_time=self.current_time,
            peak_price=price,
            peak_profit=0.0,
            trailing_active=False,
            accumulated_fee=FEE_RATE * 100,
        )

    def on_position_closed(self, market: str) -> None:
        """포지션 종료 시 호출"""
        if market in self.position_states:
            del self.position_states[market]

    def on_partial_sell(self, market: str, reason: str) -> None:
        """부분 매도 시 호출 (미사용)"""
        pass

    # ========================================
    # 17개 전략 구현
    # ========================================

    def _count_votes(self, market: str) -> int:
        """17개 전략 투표 수 계산"""
        bars = self.bars_240m.get(market, [])
        if len(bars) < MIN_BARS:
            return 0

        # 봉 데이터를 numpy 배열로 변환
        opens = np.array([b.open for b in bars])
        highs = np.array([b.high for b in bars])
        lows = np.array([b.low for b in bars])
        closes = np.array([b.close for b in bars])
        volumes = np.array([b.volume for b in bars])

        votes = 0

        # 1. ADX_TRENDING
        if self._check_adx_trending(opens, highs, lows, closes, volumes):
            votes += 1

        # 2. HIGH_VOL_BREAK
        if self._check_high_vol_break(opens, highs, lows, closes, volumes):
            votes += 1

        # 3. ATR_BREAK_2X
        if self._check_atr_breakout(highs, lows, closes, mult=2.0):
            votes += 1

        # 4. HIGH_VOL
        if self._check_high_vol(opens, highs, lows, closes, volumes):
            votes += 1

        # 5. RSI_NEUTRAL
        if self._check_rsi_neutral(opens, highs, lows, closes, volumes):
            votes += 1

        # 6. VOL_BREAKOUT
        if self._check_vol_breakout(opens, closes, volumes):
            votes += 1

        # 7. VOTING_4
        if self._check_voting_4(opens, highs, lows, closes, volumes):
            votes += 1

        # 8. BOS_VOL
        if self._check_bos_vol(highs, closes, volumes):
            votes += 1

        # 9. ATR_BREAKOUT
        if self._check_atr_breakout(highs, lows, closes, mult=1.0):
            votes += 1

        # 10. KELTNER_BREAK_1.5
        if self._check_keltner_breakout(highs, lows, closes):
            votes += 1

        # 11. DONCHIAN_WIDTH
        if self._check_donchian_width(highs, lows, closes):
            votes += 1

        # 12. BOS_20
        if self._check_bos_20(highs, closes):
            votes += 1

        # 13. DC_BOUNCE
        if self._check_dc_bounce(lows, closes):
            votes += 1

        # 14. HA_STRONG_GREEN
        if self._check_ha_strong_green(opens, highs, lows, closes):
            votes += 1

        # 15. BB_BOUNCE
        if self._check_bb_bounce(opens, lows, closes):
            votes += 1

        # 16. EOM_CROSS
        if self._check_eom_cross(highs, lows, volumes):
            votes += 1

        # 17. VOTE_5
        if self._check_vote_5(opens, highs, lows, closes, volumes):
            votes += 1

        return votes

    # === 지표 계산 헬퍼 ===

    def _calc_sma(self, data: np.ndarray, period: int) -> np.ndarray:
        """Simple Moving Average"""
        result = np.full(len(data), np.nan)
        for i in range(period - 1, len(data)):
            result[i] = np.mean(data[i - period + 1:i + 1])
        return result

    def _calc_ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average"""
        result = np.full(len(data), np.nan)
        if len(data) < period:
            return result
        multiplier = 2 / (period + 1)
        result[period - 1] = np.mean(data[:period])
        for i in range(period, len(data)):
            result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
        return result

    def _calc_rsi(self, closes: np.ndarray, period: int = 14) -> np.ndarray:
        """Relative Strength Index"""
        rsi = np.full(len(closes), np.nan)
        if len(closes) < period + 1:
            return rsi

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(closes)):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            if avg_loss == 0:
                rsi[i] = 100
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100 - (100 / (1 + rs))

        return rsi

    def _calc_macd(self, closes: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """MACD (12, 26, 9)"""
        ema12 = self._calc_ema(closes, 12)
        ema26 = self._calc_ema(closes, 26)
        macd = ema12 - ema26
        signal = self._calc_ema(np.nan_to_num(macd), 9)
        hist = macd - signal
        return macd, signal, hist

    def _calc_bollinger(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Bollinger Bands"""
        sma = self._calc_sma(closes, period)
        std = np.full(len(closes), np.nan)
        for i in range(period - 1, len(closes)):
            std[i] = np.std(closes[i - period + 1:i + 1])
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        return upper, sma, lower

    def _calc_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
        """Average True Range"""
        tr = np.zeros(len(closes))
        tr[0] = highs[0] - lows[0]
        for i in range(1, len(closes)):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
        return self._calc_sma(tr, period)

    def _calc_adx(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """ADX, +DI, -DI"""
        n = len(closes)
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)

        for i in range(1, n):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]
            plus_dm[i] = high_diff if high_diff > low_diff and high_diff > 0 else 0
            minus_dm[i] = low_diff if low_diff > high_diff and low_diff > 0 else 0
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

        atr = self._calc_ema(tr, period)
        plus_di = np.zeros(n)
        minus_di = np.zeros(n)

        for i in range(period, n):
            if atr[i] > 0:
                plus_di[i] = 100 * self._calc_ema(plus_dm, period)[i] / atr[i]
                minus_di[i] = 100 * self._calc_ema(minus_dm, period)[i] / atr[i]

        dx = np.zeros(n)
        for i in range(n):
            if plus_di[i] + minus_di[i] > 0:
                dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i])

        adx = self._calc_ema(dx, period)
        return adx, plus_di, minus_di

    def _calc_mfi(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray, period: int = 14) -> np.ndarray:
        """Money Flow Index"""
        typical_price = (highs + lows + closes) / 3
        raw_money_flow = typical_price * volumes

        mfi = np.full(len(closes), np.nan)
        for i in range(period, len(closes)):
            pos_flow = 0
            neg_flow = 0
            for j in range(i - period + 1, i + 1):
                if j > 0 and typical_price[j] > typical_price[j - 1]:
                    pos_flow += raw_money_flow[j]
                else:
                    neg_flow += raw_money_flow[j]
            if neg_flow == 0:
                mfi[i] = 100
            else:
                mfi[i] = 100 - (100 / (1 + pos_flow / neg_flow))
        return mfi

    # === 개별 전략 체크 ===

    def _check_adx_trending(self, opens, highs, lows, closes, volumes) -> bool:
        """ADX_TRENDING: MACD+Vol + ADX 강한 추세"""
        macd, signal, hist = self._calc_macd(closes)
        adx, plus_di, minus_di = self._calc_adx(highs, lows, closes)
        vol_ma = self._calc_sma(volumes, 20)

        i = -1
        if np.isnan(macd[i]) or np.isnan(signal[i]) or np.isnan(adx[i]) or np.isnan(vol_ma[i]):
            return False

        macd_cross = macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]
        vol_surge = volumes[i] > vol_ma[i] * 1.5
        trend_strong = adx[i] > 25
        di_up = plus_di[i] > minus_di[i]

        return macd_cross and vol_surge and trend_strong and di_up

    def _check_high_vol_break(self, opens, highs, lows, closes, volumes) -> bool:
        """HIGH_VOL_BREAK: 고변동성 + 돌파"""
        atr = self._calc_atr(highs, lows, closes)
        atr_pct = atr / closes * 100
        atr_ma = self._calc_sma(atr_pct, 50)
        vol_ma = self._calc_sma(volumes, 20)

        i = -1
        if np.isnan(atr_pct[i]) or np.isnan(atr_ma[i]) or np.isnan(vol_ma[i]):
            return False

        high_vol = atr_pct[i] > atr_ma[i] * 1.2
        high_20 = np.nanmax(highs[max(0, len(highs) - 21):-1]) if len(highs) > 21 else highs[-2]
        breakout = closes[i] > high_20
        vol_surge = volumes[i] > vol_ma[i] * 2.5
        is_green = closes[i] > opens[i]

        return high_vol and breakout and vol_surge and is_green

    def _check_atr_breakout(self, highs, lows, closes, mult: float = 1.0) -> bool:
        """ATR_BREAKOUT: ATR 돌파"""
        atr = self._calc_atr(highs, lows, closes)
        i = -1
        if np.isnan(atr[i]):
            return False
        return closes[i] > closes[i - 1] + atr[i] * mult

    def _check_high_vol(self, opens, highs, lows, closes, volumes) -> bool:
        """HIGH_VOL: MACD+Vol + 고변동성"""
        macd, signal, hist = self._calc_macd(closes)
        atr = self._calc_atr(highs, lows, closes)
        atr_pct = atr / closes * 100
        atr_ma = self._calc_sma(atr_pct, 50)
        vol_ma = self._calc_sma(volumes, 20)

        i = -1
        if np.isnan(macd[i]) or np.isnan(atr_pct[i]) or np.isnan(atr_ma[i]) or np.isnan(vol_ma[i]):
            return False

        macd_cross = macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]
        vol_surge = volumes[i] > vol_ma[i] * 1.5
        high_vol = atr_pct[i] > atr_ma[i] * 1.2

        return macd_cross and vol_surge and high_vol

    def _check_rsi_neutral(self, opens, highs, lows, closes, volumes) -> bool:
        """RSI_NEUTRAL: MACD+Vol + RSI 중립대"""
        macd, signal, hist = self._calc_macd(closes)
        rsi = self._calc_rsi(closes)
        vol_ma = self._calc_sma(volumes, 20)

        i = -1
        if np.isnan(macd[i]) or np.isnan(rsi[i]) or np.isnan(vol_ma[i]):
            return False

        macd_cross = macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]
        vol_surge = volumes[i] > vol_ma[i] * 1.5
        rsi_neutral = 40 < rsi[i] < 60

        return macd_cross and vol_surge and rsi_neutral

    def _check_vol_breakout(self, opens, closes, volumes) -> bool:
        """VOL_BREAKOUT: 거래량 스파이크 + 양봉"""
        vol_ma = self._calc_sma(volumes, 20)
        i = -1
        if np.isnan(vol_ma[i]):
            return False
        vol_spike = volumes[i] > vol_ma[i] * 2.0
        is_green = closes[i] > opens[i]
        return vol_spike and is_green

    def _check_voting_4(self, opens, highs, lows, closes, volumes) -> bool:
        """VOTING_4: v4 투표 전략"""
        macd, signal, hist = self._calc_macd(closes)
        rsi = self._calc_rsi(closes)
        sma10 = self._calc_sma(closes, 10)
        sma20 = self._calc_sma(closes, 20)
        vol_ma = self._calc_sma(volumes, 20)

        i = -1
        votes = 0

        if not np.isnan(hist[i]) and hist[i] > 0:
            votes += 1
        if not np.isnan(rsi[i]) and not np.isnan(rsi[i - 1]) and rsi[i] > 30 and rsi[i - 1] <= 30:
            votes += 1
        if not np.isnan(sma10[i]) and not np.isnan(sma20[i]) and sma10[i] > sma20[i]:
            votes += 1
        if not np.isnan(vol_ma[i]) and volumes[i] > vol_ma[i] * 1.5:
            votes += 1
        if closes[i] > opens[i]:
            votes += 1

        return votes >= 4

    def _check_bos_vol(self, highs, closes, volumes) -> bool:
        """BOS_VOL: BOS + Volume Spike"""
        lookback = 10
        i = -1
        if len(highs) < lookback + 2:
            return False

        recent_high = np.nanmax(highs[-lookback - 1:-1])
        bos = closes[i] > recent_high and closes[i - 1] <= recent_high
        vol_ma = self._calc_sma(volumes, 20)
        vol_spike = volumes[i] > vol_ma[i] * 1.5 if not np.isnan(vol_ma[i]) else False

        return bos and vol_spike

    def _check_keltner_breakout(self, highs, lows, closes) -> bool:
        """KELTNER_BREAK_1.5: Keltner Channel 상단 돌파"""
        period = 20
        atr_mult = 1.5
        ema = self._calc_ema(closes, period)
        atr = self._calc_atr(highs, lows, closes, period)

        i = -1
        if np.isnan(ema[i]) or np.isnan(atr[i]) or np.isnan(ema[i - 1]) or np.isnan(atr[i - 1]):
            return False

        upper = ema[i] + atr_mult * atr[i]
        upper_prev = ema[i - 1] + atr_mult * atr[i - 1]

        return closes[i - 1] <= upper_prev and closes[i] > upper

    def _check_donchian_width(self, highs, lows, closes) -> bool:
        """DONCHIAN_WIDTH: Donchian 채널 폭 확장 + 상단 돌파"""
        period = 20
        if len(highs) < period + 2:
            return False

        dc_high = np.nanmax(highs[-period - 1:-1])
        dc_high_prev = np.nanmax(highs[-period - 2:-2])
        dc_low = np.nanmin(lows[-period - 1:-1])
        dc_low_prev = np.nanmin(lows[-period - 2:-2])

        dc_mid = (dc_high + dc_low) / 2
        dc_mid_prev = (dc_high_prev + dc_low_prev) / 2

        if dc_mid == 0 or dc_mid_prev == 0:
            return False

        width = (dc_high - dc_low) / dc_mid * 100
        width_prev = (dc_high_prev - dc_low_prev) / dc_mid_prev * 100

        width_expanding = width > width_prev
        breakout = closes[-1] > dc_high_prev

        return width_expanding and breakout

    def _check_bos_20(self, highs, closes) -> bool:
        """BOS_20: Break of Structure 20"""
        lookback = 20
        if len(highs) < lookback + 2:
            return False

        recent_high = np.nanmax(highs[-lookback - 1:-1])
        return closes[-1] > recent_high and closes[-2] <= recent_high

    def _check_dc_bounce(self, lows, closes) -> bool:
        """DC_BOUNCE: Donchian Channel 하단 바운스"""
        period = 20
        if len(lows) < period + 1:
            return False

        dc_low = np.nanmin(lows[-period - 1:-1])
        return lows[-1] <= dc_low * 1.002 and closes[-1] > dc_low

    def _check_ha_strong_green(self, opens, highs, lows, closes) -> bool:
        """HA_STRONG_GREEN: Heikin Ashi 양봉 + 아래꼬리 없음"""
        n = len(closes)
        if n < 3:
            return False

        # Heikin Ashi 계산
        ha_close = (opens + highs + lows + closes) / 4
        ha_open = np.zeros(n)
        ha_open[0] = (opens[0] + closes[0]) / 2
        for j in range(1, n):
            ha_open[j] = (ha_open[j - 1] + ha_close[j - 1]) / 2

        i = -1
        ha_green = ha_close[i] > ha_open[i]
        ha_no_lower = lows[i] >= min(ha_open[i], ha_close[i]) - 0.0001

        ha_green_prev = ha_close[i - 1] > ha_open[i - 1]
        ha_no_lower_prev = lows[i - 1] >= min(ha_open[i - 1], ha_close[i - 1]) - 0.0001
        prev_not = not (ha_green_prev and ha_no_lower_prev)

        return ha_green and ha_no_lower and prev_not

    def _check_bb_bounce(self, opens, lows, closes) -> bool:
        """BB_BOUNCE: BB 하단 터치 후 양봉"""
        upper, mid, lower = self._calc_bollinger(closes)
        i = -1
        if np.isnan(lower[i - 1]):
            return False
        prev_touched = lows[i - 1] <= lower[i - 1]
        is_green = closes[i] > opens[i]
        return prev_touched and is_green

    def _check_eom_cross(self, highs, lows, volumes) -> bool:
        """EOM_CROSS: Ease of Movement 0선 상향 돌파"""
        n = len(highs)
        if n < 3:
            return False

        dm = np.zeros(n)
        br = np.zeros(n)
        for j in range(1, n):
            dm[j] = ((highs[j] + lows[j]) / 2) - ((highs[j - 1] + lows[j - 1]) / 2)
            range_val = highs[j] - lows[j]
            br[j] = volumes[j] / range_val if range_val > 0 else 0

        eom = np.zeros(n)
        for j in range(1, n):
            eom[j] = dm[j] / br[j] if br[j] > 0 else 0

        return eom[-1] > 0 and eom[-2] <= 0

    def _check_vote_5(self, opens, highs, lows, closes, volumes) -> bool:
        """VOTE_5: 8개 지표 투표 (5개 이상)"""
        macd, signal, hist = self._calc_macd(closes)
        rsi = self._calc_rsi(closes)
        mfi = self._calc_mfi(highs, lows, closes, volumes)
        sma10 = self._calc_sma(closes, 10)
        sma20 = self._calc_sma(closes, 20)
        vol_ma = self._calc_sma(volumes, 20)
        adx, plus_di, minus_di = self._calc_adx(highs, lows, closes)
        upper, mid, lower = self._calc_bollinger(closes)

        i = -1
        votes = 0

        if not np.isnan(hist[i]) and hist[i] > 0:
            votes += 1
        if not np.isnan(rsi[i]) and 25 < rsi[i] < 45:
            votes += 1
        if not np.isnan(mfi[i]) and mfi[i] < 50:
            votes += 1
        if not np.isnan(sma10[i]) and not np.isnan(sma20[i]) and sma10[i] > sma20[i]:
            votes += 1
        if not np.isnan(vol_ma[i]) and volumes[i] > vol_ma[i] * 1.5:
            votes += 1
        if closes[i] > opens[i]:
            votes += 1
        if not np.isnan(adx[i]) and adx[i] > 20:
            votes += 1
        if not np.isnan(lower[i]) and not np.isnan(upper[i]) and upper[i] != lower[i]:
            bb_pct = (closes[i] - lower[i]) / (upper[i] - lower[i])
            if bb_pct < 0.4:
                votes += 1

        return votes >= 5
