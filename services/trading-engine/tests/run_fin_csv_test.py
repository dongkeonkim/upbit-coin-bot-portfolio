"""fin.csv 파라미터로 백테스트 실행 및 Go 결과와 비교"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import csv
from datetime import datetime
from typing import Dict, List, Optional

from src.config.params import Params, INITIAL_BALANCE
from src.config.settings import Settings, PERIODS
from src.data.postgres_loader import DataCache, Period
from src.strategy.momentum import MomentumStrategy
from src.simulation.engine import BacktestEngine


def parse_bool(value: str) -> bool:
    """문자열을 불린으로 변환"""
    if not value:
        return False
    return value.strip().upper() == "TRUE"


def parse_float(value: str, default: float = 0.0) -> float:
    """문자열을 실수로 변환"""
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_int(value: str, default: int = 0) -> int:
    """문자열을 정수로 변환"""
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return default


def load_first_params_from_fin_csv(filepath: str) -> Optional[Params]:
    """fin.csv에서 첫 번째 파라미터 세트 로드"""
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # index 컬럼이 숫자로 시작하는 행만 처리
            index_val = row.get("index", "").strip()
            if not index_val or not index_val.isdigit():
                continue

            # 첫 번째 유효한 행 찾음
            params = Params(
                short_timeframe_minutes=parse_int(row.get("short_tf", "3"), 3),
                volume_avg_window_minutes=parse_int(row.get("volume_avg_window", "60"), 60),
                buy_stage1_change_pct=parse_float(row.get("stage1_change_pct", "0.8")),
                buy_stage1_min_coins=parse_int(row.get("stage1_min_coins", "2"), 2),
                buy_stage1_sliding_window=parse_bool(row.get("stage1_sliding_window", "true")),
                buy_stage1_alt_change_pct=parse_float(row.get("stage1_alt_change_pct", "1.5")),
                buy_stage1_alt_min_coins=parse_int(row.get("stage1_alt_min_coins", "1"), 1),
                buy_stage2_change_pct=parse_float(row.get("stage2_change_pct", "0.2")),
                buy_stage3_volume_surge=parse_float(row.get("stage3_volume_surge", "0.8")),
                investment_ratio=parse_float(row.get("investment_ratio", "0.1")),
                stop_loss=parse_float(row.get("stop_loss", "-1.5")),
                trailing_target_early=parse_float(row.get("early_trailing_target", "1.0")),
                trailing_exit_early=parse_float(row.get("early_trailing_exit", "0.3")),
                trailing_target=parse_float(row.get("anca_trailing_target", "6.0")),
                trailing_exit=parse_float(row.get("anca_trailing_exit", "1.0")),
                trailing_drop=parse_float(row.get("anca_trailing_drop", "6.0")),
                market_filter_enabled=parse_bool(row.get("market_filter_enabled", "false")),
                market_filter_ma_days=parse_int(row.get("market_filter_ma_days", "3"), 3),
                market_filter_bear_threshold=parse_float(row.get("market_filter_bear_threshold", "-2.0")),
                market_filter_bull_threshold=parse_float(row.get("market_filter_bull_threshold", "0.0")),
                market_filter_sideways_reduction=parse_float(row.get("market_filter_sideways_reduction", "0.5")),
                dynamic_position_enabled=parse_bool(row.get("dynamic_position_enabled", "false")),
                dynamic_position_strong_ratio=parse_float(row.get("dynamic_position_strong_ratio", "0.15")),
                dynamic_position_medium_ratio=parse_float(row.get("dynamic_position_medium_ratio", "0.10")),
                dynamic_position_weak_ratio=parse_float(row.get("dynamic_position_weak_ratio", "0.05")),
                dynamic_position_strong_threshold=parse_float(row.get("dynamic_position_strong_threshold", "1.5")),
                dynamic_position_medium_threshold=parse_float(row.get("dynamic_position_medium_threshold", "0.8")),
                improved_stop_loss_enabled=parse_bool(row.get("improved_stop_loss_enabled", "false")),
                improved_stop_loss_bull=parse_float(row.get("improved_stop_loss_bull", "-0.5")),
                improved_stop_loss_sideways=parse_float(row.get("improved_stop_loss_sideways", "-0.7")),
                rsi_enabled=parse_bool(row.get("rsi_enabled", "false")),
                rsi_period=parse_int(row.get("rsi_period", "14"), 14),
                rsi_overbought=parse_float(row.get("rsi_overbought", "70.0")),
                rsi_oversold=parse_float(row.get("rsi_oversold", "30.0")),
                bb_enabled=parse_bool(row.get("bb_enabled", "false")),
                bb_period=parse_int(row.get("bb_period", "20"), 20),
                bb_std_dev=parse_float(row.get("bb_std_dev", "2.0")),
                atr_enabled=parse_bool(row.get("atr_enabled", "false")),
                atr_period=parse_int(row.get("atr_period", "14"), 14),
                atr_multiplier=parse_float(row.get("atr_multiplier", "2.0")),
                adx_enabled=parse_bool(row.get("adx_enabled", "false")),
                adx_period=parse_int(row.get("adx_period", "14"), 14),
                adx_threshold=parse_float(row.get("adx_threshold", "25.0")),
            )

            # Go 결과도 함께 반환 (비교용)
            go_results = {
                "avg_return": parse_float(row.get("avg_return", "0")),
                "avg_win_rate": parse_float(row.get("avg_win_rate", "0")),
                "avg_trade_count": parse_float(row.get("avg_trade_count", "0")),
                "p1_return": parse_float(row.get("p1_return", "0")),
                "p2_return": parse_float(row.get("p2_return", "0")),
                "p3_return": parse_float(row.get("p3_return", "0")),
                "total_trades": parse_int(row.get("total_trades", "0")),
            }

            return params, go_results

    return None, None


def run_backtest_for_period(
    params: Params,
    period_name: str,
    settings: Settings,
) -> Dict:
    """특정 기간의 백테스트 실행"""
    period_config = PERIODS[period_name]
    period = Period(
        name=period_name,
        start=period_config["start"],
        end=period_config["end"],
    )

    # 데이터 로드
    data_cache = DataCache(settings)
    data_cache.connect()

    try:
        data_cache.load_all_data([], {period_name: period})
        candles_1m = data_cache.get_all_candles_1m_for_period(period_name)

        if not candles_1m:
            return None

        # 전략 생성
        strategy = MomentumStrategy(params)

        # 백테스트 실행
        engine = BacktestEngine(strategy, INITIAL_BALANCE)
        result = engine.run_with_data(
            markets=list(candles_1m.keys()),
            candles_1m=candles_1m,
            start_time=period.start,
            end_time=period.end,
            params=params,
        )

        return {
            "return_percent": result.total_return_percent,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "buy_count": result.buy_count,
            "sell_count": result.sell_count,
            "final_balance": result.final_balance,
        }

    finally:
        data_cache.disconnect()


def main():
    """메인 실행"""
    fin_csv_path = os.getenv("FIN_CSV_PATH", "fin.csv")

    print("=" * 70)
    print("fin.csv 파라미터로 Python 백테스트 실행")
    print("=" * 70)

    # 파라미터 로드
    params, go_results = load_first_params_from_fin_csv(fin_csv_path)
    if params is None:
        print("❌ fin.csv에서 파라미터를 로드할 수 없습니다.")
        return

    print("\n📋 로드된 파라미터:")
    print(f"  - short_tf: {params.short_timeframe_minutes}")
    print(f"  - volume_avg_window: {params.volume_avg_window_minutes}")
    print(f"  - stage1_change_pct: {params.buy_stage1_change_pct}")
    print(f"  - stage1_min_coins: {params.buy_stage1_min_coins}")
    print(f"  - stage1_sliding_window: {params.buy_stage1_sliding_window}")
    print(f"  - investment_ratio: {params.investment_ratio}")
    print(f"  - stop_loss: {params.stop_loss}")
    print(f"  - market_filter_enabled: {params.market_filter_enabled}")
    print(f"  - dynamic_position_enabled: {params.dynamic_position_enabled}")
    print(f"  - bb_enabled: {params.bb_enabled}")
    print(f"  - atr_enabled: {params.atr_enabled}")

    print("\n📊 Go 결과 (fin.csv 기록):")
    print(f"  - avg_return: {go_results['avg_return']:.2f}%")
    print(f"  - avg_win_rate: {go_results['avg_win_rate']:.2f}%")
    print(f"  - total_trades: {go_results['total_trades']}")
    print(f"  - P1 return: {go_results['p1_return']:.2f}%")
    print(f"  - P2 return: {go_results['p2_return']:.2f}%")
    print(f"  - P3 return: {go_results['p3_return']:.2f}%")

    settings = Settings.from_env()

    print("\n" + "=" * 70)
    print("Python 백테스트 실행 중...")
    print("=" * 70)

    # 각 기간별 백테스트 실행
    python_results = {}
    for period_name in ["P1", "P2", "P3"]:
        print(f"\n🔄 {period_name} 기간 백테스트 실행 중...")
        result = run_backtest_for_period(params, period_name, settings)
        if result:
            python_results[period_name] = result
            print(f"  ✅ {period_name}: {result['return_percent']:.2f}% (trades: {result['total_trades']}, win_rate: {result['win_rate']:.1f}%)")
        else:
            print(f"  ❌ {period_name}: 데이터 없음")

    # 결과 비교
    print("\n" + "=" * 70)
    print("📊 결과 비교 (Go vs Python)")
    print("=" * 70)

    if "P1" in python_results:
        p1_diff = python_results["P1"]["return_percent"] - go_results["p1_return"]
        print(f"\nP1 기간:")
        print(f"  Go:     {go_results['p1_return']:.4f}%")
        print(f"  Python: {python_results['P1']['return_percent']:.4f}%")
        print(f"  차이:   {p1_diff:.4f}% {'✅' if abs(p1_diff) < 0.01 else '⚠️'}")

    if "P2" in python_results:
        p2_diff = python_results["P2"]["return_percent"] - go_results["p2_return"]
        print(f"\nP2 기간:")
        print(f"  Go:     {go_results['p2_return']:.4f}%")
        print(f"  Python: {python_results['P2']['return_percent']:.4f}%")
        print(f"  차이:   {p2_diff:.4f}% {'✅' if abs(p2_diff) < 0.01 else '⚠️'}")

    if "P3" in python_results:
        p3_diff = python_results["P3"]["return_percent"] - go_results["p3_return"]
        print(f"\nP3 기간:")
        print(f"  Go:     {go_results['p3_return']:.4f}%")
        print(f"  Python: {python_results['P3']['return_percent']:.4f}%")
        print(f"  차이:   {p3_diff:.4f}% {'✅' if abs(p3_diff) < 0.01 else '⚠️'}")

    # 평균 계산
    if python_results:
        py_avg_return = sum(r["return_percent"] for r in python_results.values()) / len(python_results)
        py_total_trades = sum(r["total_trades"] for r in python_results.values())

        print(f"\n평균 수익률:")
        print(f"  Go:     {go_results['avg_return']:.4f}%")
        print(f"  Python: {py_avg_return:.4f}%")
        print(f"  차이:   {py_avg_return - go_results['avg_return']:.4f}%")

        print(f"\n총 거래 수:")
        print(f"  Go:     {go_results['total_trades']}")
        print(f"  Python: {py_total_trades}")


if __name__ == "__main__":
    main()
