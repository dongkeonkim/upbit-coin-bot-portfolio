"""전략 로직 디버깅"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from src.config.params import Params
from src.config.settings import Settings, PERIODS
from src.data.postgres_loader import DataCache, Period
from src.models.candle import Candle
from src.models.position import PortfolioState
from src.strategy.momentum import MomentumStrategy
from src.simulation.time_simulator import TimeSimulator


def debug_first_few_days():
    """처음 며칠간의 전략 동작 디버깅"""
    # 간단한 파라미터로 테스트
    params = Params(
        short_timeframe_minutes=3,
        volume_avg_window_minutes=60,
        buy_stage1_change_pct=0.8,
        buy_stage1_min_coins=2,
        buy_stage1_sliding_window=True,
        buy_stage2_change_pct=0.2,
        buy_stage3_volume_surge=0.8,
        investment_ratio=0.1,
        stop_loss=-1.5,
        trailing_target_early=1.0,
        trailing_exit_early=0.3,
        trailing_target=6.0,
        trailing_exit=1.0,
        trailing_drop=6.0,
        market_filter_enabled=False,  # 시장 필터 비활성화
        dynamic_position_enabled=False,
    )

    settings = Settings.from_env()

    # P1 기간의 첫 3일만 테스트
    start_time = datetime(2024, 1, 1)
    end_time = datetime(2024, 1, 4)  # 3일

    period = Period(name="TEST", start=start_time, end=end_time)

    print("=" * 70)
    print("전략 디버깅 테스트")
    print("=" * 70)
    print(f"기간: {start_time} ~ {end_time}")
    print(f"파라미터: short_tf={params.short_timeframe_minutes}, sliding={params.buy_stage1_sliding_window}")

    # 데이터 로드
    data_cache = DataCache(settings)
    data_cache.connect()

    try:
        data_cache.load_all_data([], {"TEST": period})
        candles_1m = data_cache.get_all_candles_1m_for_period("TEST")

        if not candles_1m:
            print("❌ 데이터 없음")
            return

        print(f"마켓 수: {len(candles_1m)}")
        for market, candles in candles_1m.items():
            print(f"  {market}: {len(candles)} candles")

        # 시뮬레이션
        time_sim = TimeSimulator(start_time, end_time, candles_1m, params)
        portfolio = PortfolioState(1_000_000.0)
        strategy = MomentumStrategy(params)

        buy_signals = []
        sell_signals = []
        iteration = 0

        while not time_sim.is_finished():
            iteration += 1
            current_ts = time_sim.get_current_timestamp_ms()
            current_time = time_sim.get_current_time()

            # 캔들 데이터 가져오기
            candles_1m_now, candles_trade_check, candles_short, candles_long = time_sim.get_candles_at_time(current_ts)

            # 평균 거래대금 계산
            avg_quote_volumes = {}
            for market in candles_1m_now:
                avg_quote_volumes[market] = time_sim.get_avg_quote_volume(market)

            # 전략 업데이트
            strategy.update_candles(candles_1m_now, candles_trade_check, candles_short, candles_long, avg_quote_volumes)

            # 처음 몇 번의 iteration 상세 출력
            if iteration <= 100 and iteration % 10 == 0:
                print(f"\n[Iter {iteration}] {current_time}")
                print(f"  1m candles: {len(candles_1m_now)}, short: {len(candles_short)}, trade_check: {len(candles_trade_check)}")

                # Stage 1 체크
                if len(candles_short) > 0:
                    stage1_candidates = strategy._stage1_check()
                    if stage1_candidates:
                        print(f"  Stage1 후보: {len(stage1_candidates)}")
                        for c in stage1_candidates[:3]:
                            print(f"    - {c.market}: {c.change_short:.2f}%")

            # 매수 신호 분석
            buy_signal = strategy.analyze_buy(portfolio, current_time)
            if buy_signal:
                buy_signals.append((current_time, buy_signal))
                # 매수 실행
                volume = buy_signal.amount / buy_signal.price
                portfolio.add_position(buy_signal.market, volume, buy_signal.price, 0.0005)
                strategy.on_position_opened(buy_signal.market, buy_signal.price)
                print(f"\n🔵 BUY: {buy_signal.market} @ {buy_signal.price:.0f} (amount: {buy_signal.amount:.0f})")

            # 매도 신호 분석
            sell_signal_list = strategy.analyze_sell(portfolio)
            for signal in sell_signal_list:
                sell_signals.append((current_time, signal))
                # 매도 실행
                portfolio.remove_position(signal.market, signal.volume, signal.price, 0.0005)
                strategy.on_position_closed(signal.market)
                print(f"\n🔴 SELL: {signal.market} @ {signal.price:.0f} (reason: {signal.reason})")

            # 전략 상태 업데이트
            current_date = current_time.strftime("%Y-%m-%d")
            top10 = {market: True for market in list(candles_1m_now.keys())[:10]}
            strategy.update_strategy_state(portfolio, candles_1m_now, current_date, top10)

            # 시간 진행
            time_sim.advance(1)

        print("\n" + "=" * 70)
        print("결과 요약")
        print("=" * 70)
        print(f"총 iteration: {iteration}")
        print(f"매수 신호: {len(buy_signals)}")
        print(f"매도 신호: {len(sell_signals)}")
        print(f"최종 잔고: {portfolio.quote_balance:,.0f}")
        print(f"보유 포지션: {len(portfolio.positions)}")

        # 현재 가격으로 총 가치 계산
        current_prices = {m: c[-1].close for m, c in candles_1m.items() if c}
        total_value = portfolio.get_total_value(current_prices)
        return_pct = ((total_value - 1_000_000) / 1_000_000) * 100
        print(f"총 자산: {total_value:,.0f} ({return_pct:+.2f}%)")

    finally:
        data_cache.disconnect()


if __name__ == "__main__":
    debug_first_few_days()
