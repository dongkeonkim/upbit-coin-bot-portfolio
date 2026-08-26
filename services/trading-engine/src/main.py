"""트레이딩 엔진 메인 엔트리 포인트 (백테스트, 실시간, 가격 모니터)"""

import argparse
import asyncio
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import Params, Settings, PERIODS
from .config.csv_loader import load_params_from_csv, load_single_params_from_csv
from .config.params import INITIAL_BALANCE, ML_STRATEGY
from .data import DataCache
from .data.postgres_loader import Period
from .models.candle import Candle
from .strategy import MomentumStrategy, WeightedVotingStrategy, Voting240mStrategy, MLStrategy, DropBounceStrategy
from .strategy.ml_strategy import (
    COIN_PARAMS, TRAILING_TIERS, SL_PCT, INVESTMENT_RATIO
)
from .simulation import BacktestEngine
from .live import TradingLoop, LeaderElector
from .reporter import Logger, generate_log_filename
from .utils import get_logger

# 구조화된 로거 (실시간 모드용)
main_logger = get_logger(__name__)


def _print_detailed_stats(result, period_name: str) -> None:
    """
    백테스트 결과의 상세 통계 출력 (verify_strategy_performance.py 형식)
    - 매도 사유별 승률/수익률
    - 진입 전략별 승률/수익률
    - 코인별 승률/수익률
    - 전체 통합 결과
    """
    from collections import defaultdict

    # SELL 거래만 분석 (realized_pnl이 있는 거래)
    sell_trades = [t for t in result.trades if t.side == "SELL" and t.realized_pnl is not None]

    if not sell_trades:
        print("\n⚠️ No completed trades to analyze")
        return

    # 매도 사유별 통계 (reason: stop_loss, anca_exit_condition, etc.)
    reason_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    # 진입 전략별 통계 (entry_strategy: Active Volatility, Stable Dip, etc.)
    strategy_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})
    # 코인별 통계
    coin_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0.0})

    for trade in sell_trades:
        # 수익률 계산 (realized_pnl / amount * 100)
        pnl_pct = (trade.realized_pnl / trade.amount) if trade.amount > 0 else 0

        # 매도 사유별 집계
        reason_name = trade.reason if trade.reason else "unknown"
        reason_stats[reason_name]['trades'] += 1
        reason_stats[reason_name]['pnl'] += pnl_pct
        if trade.realized_pnl > 0:
            reason_stats[reason_name]['wins'] += 1

        # 진입 전략별 집계
        entry_strat = trade.entry_strategy if trade.entry_strategy else "unknown"
        strategy_stats[entry_strat]['trades'] += 1
        strategy_stats[entry_strat]['pnl'] += pnl_pct
        if trade.realized_pnl > 0:
            strategy_stats[entry_strat]['wins'] += 1

        # 코인별 집계
        coin_stats[trade.market]['trades'] += 1
        coin_stats[trade.market]['pnl'] += pnl_pct
        if trade.realized_pnl > 0:
            coin_stats[trade.market]['wins'] += 1

    # 매도 사유별 출력
    print(f"\n{'=' * 80}")
    print(f"📊 Strategy Performance Report [{period_name}]")
    print(f"{'=' * 80}")

    print(f"\n📈 Strategy Summary")
    print(f"   {'Strategy':<35} | {'Trades':>7} | {'WR':>6} | {'Total PnL':>10} | {'Avg PnL':>8}")
    print(f"   {'-' * 75}")

    for reason_name, stats in sorted(reason_stats.items()):
        wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        total_pnl = stats['pnl'] * 100
        avg_pnl = (stats['pnl'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        print(f"   {reason_name:<35} | {stats['trades']:>7} | {wr:>5.1f}% | {total_pnl:>+9.1f}% | {avg_pnl:>+7.2f}%")

    # 진입 전략별 출력
    print(f"\n📈 Entry Strategy Summary")
    print(f"   {'Entry Strategy':<35} | {'Trades':>7} | {'WR':>6} | {'Total PnL':>10} | {'Avg PnL':>8}")
    print(f"   {'-' * 75}")

    for strat_name, stats in sorted(strategy_stats.items()):
        wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        total_pnl = stats['pnl'] * 100
        avg_pnl = (stats['pnl'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        print(f"   {strat_name:<35} | {stats['trades']:>7} | {wr:>5.1f}% | {total_pnl:>+9.1f}% | {avg_pnl:>+7.2f}%")

    # 코인별 출력
    print(f"\n📈 Coin Summary")
    print(f"   {'Coin':<12} | {'Trades':>7} | {'WR':>6} | {'Total PnL':>10} | {'Avg PnL':>8}")
    print(f"   {'-' * 55}")

    for coin, stats in sorted(coin_stats.items()):
        wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        total_pnl = stats['pnl'] * 100
        avg_pnl = (stats['pnl'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
        print(f"   {coin:<12} | {stats['trades']:>7} | {wr:>5.1f}% | {total_pnl:>+9.1f}% | {avg_pnl:>+7.2f}%")

    # 전체 통합
    total_trades = len(sell_trades)
    total_wins = sum(1 for t in sell_trades if t.realized_pnl > 0)
    total_pnl = sum((t.realized_pnl / t.amount) if t.amount > 0 else 0 for t in sell_trades)

    print(f"\n{'=' * 80}")
    print(f"📈 TOTAL (All Strategies Combined)")
    g_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    g_total_pnl = total_pnl * 100
    g_avg_pnl = (total_pnl / total_trades * 100) if total_trades > 0 else 0
    print(f"   [Total] Trades: {total_trades} | WR: {g_wr:.1f}% | Total PnL: {g_total_pnl:+.1f}% | Avg PnL: {g_avg_pnl:+.2f}%")
    print(f"{'=' * 80}")


def run_backtest(
    params: Params,
    period_name: str = "FULL",
    settings: Optional[Settings] = None,
    markets: Optional[list] = None,
    log_enabled: bool = False,
    log_file: Optional[str] = None,
    strategy_type: str = "ml",
) -> None:
    """백테스트 실행 (Go와 동일한 로직)"""
    settings = settings or Settings.from_env()

    # 기간 설정
    if period_name not in PERIODS:
        raise ValueError(f"Invalid period: {period_name}. Available: {list(PERIODS.keys())}")

    period_config = PERIODS[period_name]
    period = Period(
        name=period_name,
        start=period_config["start"],
        end=period_config["end"],
    )

    # 로거 초기화
    logger = None
    if log_enabled:
        if log_file is None:
            # 절대 경로로 로그 디렉토리 설정 (services/trading-engine/logs/)
            log_dir = Path(__file__).parent.parent / "logs"
            log_dir.mkdir(exist_ok=True)
            log_file = str(log_dir / generate_log_filename(period_name))
        logger = Logger(log_file)
        print(f"📝 Logging to: {log_file}")

    print(f"\n🚀 Starting backtest for period {period_name}")
    print(f"   Period: {period.start} ~ {period.end}")
    print(f"   Initial balance: {INITIAL_BALANCE:,.0f} KRW")

    # 데이터 로드
    data_cache = DataCache(settings)
    data_cache.connect()

    try:
        # 마켓이 지정되지 않으면 자동 조회
        # 쉼표로 구분된 마켓 목록도 처리 (예: ["KRW-ETH,KRW-XRP"] → ["KRW-ETH", "KRW-XRP"])
        market_list = []
        if markets:
            for m in markets:
                if "," in m:
                    market_list.extend([x.strip() for x in m.split(",") if x.strip()])
                else:
                    market_list.append(m.strip())

        # ML 전략인 경우 COIN_PARAMS에 있는 코인만 로드 (최적화: 231개 → 15개)
        if strategy_type == "ml" and not market_list:
            market_list = list(COIN_PARAMS.keys())
            print(f"   🚀 ML 최적화: {len(market_list)}개 코인만 로드")

        data_cache.load_all_data(market_list, {period_name: period})

        # 1분봉 데이터 준비
        candles_1m = data_cache.get_all_candles_1m_for_period(period_name)

        if not candles_1m:
            print("❌ No candle data loaded")
            return

        market_count = len(candles_1m)
        total_candles = sum(len(c) for c in candles_1m.values())

        print(f"   Markets: {market_count}, Total candles: {total_candles}")

        # 로거에 전략 파라미터 출력
        if logger:
            logger.print_strategy_params(
                params=params,
                start_time=period.start,
                end_time=period.end,
                markets=list(candles_1m.keys()),
                initial_balance=INITIAL_BALANCE,
                total_candles=total_candles,
            )

        # 전략 생성
        if strategy_type == "weighted-voting":
            strategy = WeightedVotingStrategy(params)
            print(f"   Strategy: WeightedVotingStrategy (TP +0.5%, SL -0.4%)")
        elif strategy_type == "multi":
            from .strategy.multi_strategy import MultiStrategy

            strategy = MultiStrategy()  # strategies_combined.json 로드
            print(f"   Strategy: MultiStrategy (3-Strategy Portfolio + Smart Trailing)")
        elif strategy_type == "voting-240m":
            strategy = Voting240mStrategy(params)
            print(f"   Strategy: Voting240mStrategy (17 strategies, 4+ votes, +0.5%/-0.3% trailing)")
        elif strategy_type == "ml":
            strategy = MLStrategy(params)
            print(f"   Strategy: MLStrategy (코인별 XGBoost 모델, 25개 피처, Trailing 1.8%/0.3%)")
            # ML 전략 파라미터 로그 출력
            if logger:
                # 로드된 모델의 coin_params만 필터링
                loaded_coin_params = {m: COIN_PARAMS[m] for m in strategy.models.keys() if m in COIN_PARAMS}
                logger.print_ml_strategy_params(
                    model_count=len(strategy.models),
                    feature_count=len(strategy.features),
                    coin_params=loaded_coin_params,
                    trailing_tiers=TRAILING_TIERS,
                    stop_loss=SL_PCT,
                    investment_ratio=INVESTMENT_RATIO,
                )
        elif strategy_type == "drop-bounce":
            strategy = DropBounceStrategy(params)
            print(f"   Strategy: DropBounceStrategy (10m -2.5% drop + 0.5% bounce, TP +1.5%, SL -1.0%)")
        else:
            strategy = MomentumStrategy(params)
            print(f"   Strategy: MomentumStrategy (default)")

        # 백테스트 엔진 생성 및 실행
        engine = BacktestEngine(strategy, INITIAL_BALANCE, logger=logger)
        result = engine.run_with_data(
            markets=list(candles_1m.keys()),
            candles_1m=candles_1m,
            start_time=period.start,
            end_time=period.end,
            params=params,
        )

        # 로거에 결과 출력
        if logger:
            result_dict = {
                "start_time": result.start_time,
                "end_time": result.end_time,
                "initial_balance": result.initial_balance,
                "final_balance": result.final_balance,
                "total_return": result.total_return,
                "total_return_percent": result.total_return_percent,
                "total_trades": result.total_trades,
                "buy_count": result.buy_count,
                "sell_count": result.sell_count,
                "trades": [asdict(t) for t in result.trades],
            }
            logger.print_results(result_dict)
            logger.flush()
            logger.print_to_console()
            logger.close()
        else:
            # 기본 콘솔 출력
            print("\n" + "=" * 60)
            print("📊 BACKTEST RESULTS")
            print("=" * 60)
            print(f"Period: {result.start_time.date()} ~ {result.end_time.date()}")
            print(f"Initial Balance: {result.initial_balance:,.0f} KRW")
            print(f"Final Balance: {result.final_balance:,.0f} KRW")
            print(f"Total Return: {result.total_return:,.0f} KRW ({result.total_return_percent:.2f}%)")
            print(f"Total Trades: {result.total_trades} (Buy: {result.buy_count}, Sell: {result.sell_count})")
            print(f"Win Rate: {result.win_rate:.1f}%")
            print(f"Stop Loss Count: {result.stop_loss_count}")
            print(f"Trailing Early Count: {result.trailing_early_count}")
            print(f"ANCA Count: {result.anca_exit_count + result.anca_drop_count} (exit: {result.anca_exit_count}, drop: {result.anca_drop_count})")
            print("=" * 60)

            # 추가 상세 통계 (전략별, 코인별)
            _print_detailed_stats(result, period_name)

    finally:
        data_cache.disconnect()


def run_realtime(
    params: Params,
    settings: Optional[Settings] = None,
    markets: Optional[List[str]] = None,
    initial_balance: float = INITIAL_BALANCE,
    live_mode: bool = False,
    user_id: str = "default_user",
    strategy_type: str = "ml",
) -> None:
    """실시간 트레이딩 루프 실행"""
    settings = settings or Settings.from_env()

    main_logger.info(
        "realtime_starting",
        mode="LIVE" if live_mode else "SIMULATION",
        strategy=strategy_type,
        strategy_timeframe=params.short_timeframe_minutes,
        user_id=user_id if live_mode else None,
        initial_balance=initial_balance if not live_mode else None,
        market_count=len(markets) if markets else "all",
    )

    # 트레이딩 루프 생성 및 실행
    loop = TradingLoop(
        params=params,
        settings=settings,
        initial_balance=initial_balance,
        markets=markets,
        live_mode=live_mode,
        user_id=user_id,
        strategy_type=strategy_type,
    )

    loop.start()


def run_monitor(
    params: Params,
    settings: Optional[Settings] = None,
    user_id: str = "default_user",
) -> None:
    """실시간 가격 모니터 실행 (웹소켓 기반 트레일링 익절/손절)"""
    from .live.price_monitor import PriceMonitor

    settings = settings or Settings.from_env()

    main_logger.info(
        "monitor_starting",
        user_id=user_id,
        stop_loss=params.stop_loss,
        early_target=params.trailing_target_early,
        early_exit=params.trailing_exit_early,
        anca_target=params.trailing_target,
        anca_drop=params.trailing_drop,
    )

    monitor = PriceMonitor(
        params=params,
        settings=settings,
        user_id=user_id,
    )

    asyncio.run(monitor.start())


def run_realtime_ha(
    params: Params,
    settings: Optional[Settings] = None,
    markets: Optional[List[str]] = None,
    initial_balance: float = INITIAL_BALANCE,
    live_mode: bool = False,
    user_id: str = "default_user",
    lease_name: str = "trading-engine-lock",
    strategy_type: str = "ml",
) -> None:
    """
    실시간 트레이딩 (Leader Election 기반 HA 모드)

    - Leader: TradingLoop 실행
    - Standby: 대기 상태 (리소스 최소화)

    Args:
        params: 전략 파라미터
        settings: 환경 설정
        markets: 모니터링할 마켓 목록
        initial_balance: 초기 잔고
        live_mode: 실거래 모드
        strategy_type: 전략 타입 (multi, drop-bounce)
        user_id: 사용자 ID
        lease_name: K8s Lease 이름
    """
    settings = settings or Settings.from_env()

    # ML 전략일 경우 전용 파라미터 사용
    if strategy_type == "ml":
        params = ML_STRATEGY
        main_logger.info(
            "ml_strategy_params_applied",
            stop_loss=params.stop_loss,
            trailing_trigger=params.trailing_target_early,
            trailing_drop=params.trailing_drop_early,
        )

    main_logger.info(
        "realtime_ha_starting",
        mode="LIVE" if live_mode else "SIMULATION",
        strategy=strategy_type,
        strategy_timeframe=params.short_timeframe_minutes,
        user_id=user_id if live_mode else None,
        initial_balance=initial_balance if not live_mode else None,
        market_count=len(markets) if markets else "all",
        lease_name=lease_name,
    )

    # 상태 변수
    trading_loop: Optional[TradingLoop] = None
    loop_thread: Optional[threading.Thread] = None
    stop_event = threading.Event()

    def on_started_leading() -> None:
        """Leader가 되었을 때 - TradingLoop 시작"""
        nonlocal trading_loop, loop_thread

        main_logger.info("became_leader_starting_trading_loop")

        # 이전 인스턴스 정리
        if trading_loop is not None:
            main_logger.warning("cleaning_up_previous_trading_loop")
            trading_loop = None

        stop_event.clear()

        trading_loop = TradingLoop(
            params=params,
            settings=settings,
            initial_balance=initial_balance,
            markets=markets,
            live_mode=live_mode,
            user_id=user_id,
            strategy_type=strategy_type,
        )

        # 별도 스레드에서 TradingLoop 실행
        def run_loop():
            trading_loop.start_for_ha(stop_event)

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

    def on_stopped_leading() -> None:
        """Leader에서 해제되었을 때 - TradingLoop 중지"""
        nonlocal trading_loop, loop_thread

        main_logger.warning("lost_leadership_stopping_trading_loop")

        # stop_event 설정하여 TradingLoop 중지
        stop_event.set()

        if loop_thread and loop_thread.is_alive():
            loop_thread.join(timeout=30)
            if loop_thread.is_alive():
                main_logger.warning("trading_loop_thread_still_alive_after_timeout")

        trading_loop = None
        loop_thread = None

    def on_new_leader(leader_id: str) -> None:
        """새 Leader 감지"""
        main_logger.info("new_leader_elected", leader=leader_id)

    # Leader Elector 생성 및 실행
    elector = LeaderElector(
        lease_name=lease_name,
        on_started_leading=on_started_leading,
        on_stopped_leading=on_stopped_leading,
        on_new_leader=on_new_leader,
    )

    # 블로킹 실행 (SIGTERM/SIGINT 대기)
    elector.run_until_stopped()


def main():
    """CLI 엔트리 포인트"""
    parser = argparse.ArgumentParser(description="Trading Engine - Backtest & Live Trading")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # backtest 명령어
    backtest_parser = subparsers.add_parser("backtest", help="Run backtest")
    backtest_parser.add_argument(
        "--params-csv",
        type=str,
        help="Path to parameters CSV file",
    )
    backtest_parser.add_argument(
        "--period",
        type=str,
        default="FULL",
        choices=["P1", "P2", "P3", "P4", "P5", "P6", "FULL"],
        help="Backtest period (default: FULL)",
    )
    backtest_parser.add_argument(
        "--markets",
        type=str,
        nargs="+",
        help="Specific markets to backtest (default: all)",
    )
    backtest_parser.add_argument(
        "--strategy",
        type=str,
        default="ml",
        choices=["momentum", "weighted-voting", "multi", "voting-240m", "ml", "drop-bounce"],
        help="Trading strategy to use (default: ml)",
    )

    # 파라미터 직접 지정 옵션들
    backtest_parser.add_argument("--short-tf", type=int, help="Short timeframe minutes")
    backtest_parser.add_argument("--stage1-change", type=float, help="Stage 1 change percent")
    backtest_parser.add_argument("--stage1-min-coins", type=int, help="Stage 1 min coins")
    backtest_parser.add_argument("--stage2-change", type=float, help="Stage 2 change percent")
    backtest_parser.add_argument("--stage3-volume", type=float, help="Stage 3 volume surge")
    backtest_parser.add_argument("--investment-ratio", type=float, help="Investment ratio")
    backtest_parser.add_argument("--stop-loss", type=float, help="Stop loss percent")
    backtest_parser.add_argument("--trailing-target-early", type=float, help="Early trailing target")
    backtest_parser.add_argument("--trailing-exit-early", type=float, help="Early trailing exit")
    backtest_parser.add_argument("--trailing-target", type=float, help="ANCA trailing target")
    backtest_parser.add_argument("--trailing-exit", type=float, help="ANCA trailing exit")
    backtest_parser.add_argument("--trailing-drop", type=float, help="ANCA trailing drop")

    # 로그 옵션 (기본 활성화)
    backtest_parser.add_argument(
        "--log",
        action="store_true",
        default=True,
        help="Enable detailed logging (output to file and console)",
    )
    backtest_parser.add_argument(
        "--log-file",
        type=str,
        help="Log file path (default: logs/<hash>-p<period>.log)",
    )

    # realtime 명령어
    realtime_parser = subparsers.add_parser("realtime", help="Run real-time trading")
    realtime_parser.add_argument(
        "--params-csv",
        type=str,
        help="Path to parameters CSV file",
    )
    realtime_parser.add_argument(
        "--markets",
        type=str,
        nargs="+",
        help="Specific markets to trade (default: all KRW markets)",
    )
    realtime_parser.add_argument(
        "--initial-balance",
        type=float,
        default=INITIAL_BALANCE,
        help=f"Initial balance in KRW (default: {INITIAL_BALANCE:,.0f})",
    )

    # 실시간 모드용 파라미터 옵션
    realtime_parser.add_argument("--short-tf", type=int, help="Short timeframe minutes")
    realtime_parser.add_argument("--stage1-change", type=float, help="Stage 1 change percent")
    realtime_parser.add_argument("--stage1-min-coins", type=int, help="Stage 1 min coins")
    realtime_parser.add_argument("--investment-ratio", type=float, help="Investment ratio")
    realtime_parser.add_argument("--stop-loss", type=float, help="Stop loss percent")
    realtime_parser.add_argument(
        "--strategy",
        type=str,
        default="ml",
        choices=["multi", "drop-bounce", "ml"],
        help="Trading strategy to use (default: ml)",
    )

    # Live 모드 (Redis 포트폴리오 연동)
    realtime_parser.add_argument(
        "--live",
        action="store_true",
        help="Live mode: read portfolio from Redis (trade-service integration)",
    )
    realtime_parser.add_argument(
        "--user-id",
        type=str,
        default=os.getenv("USER_ID", "default_user"),
        help="User ID for Redis portfolio (default: default_user or USER_ID env)",
    )

    # monitor 명령어 (웹소켓 기반 실시간 트레일링 익절/손절) - 단독 실행용
    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Run real-time price monitor (WebSocket-based trailing stop)",
    )
    monitor_parser.add_argument(
        "--user-id",
        type=str,
        default=os.getenv("USER_ID", "default_user"),
        help="User ID for Redis portfolio (default: default_user or USER_ID env)",
    )

    # realtime-ha 명령어 (Leader Election 기반 HA 모드)
    realtime_ha_parser = subparsers.add_parser(
        "realtime-ha",
        help="Run real-time trading with HA (Leader Election)",
    )
    realtime_ha_parser.add_argument(
        "--params-csv",
        type=str,
        help="Path to parameters CSV file",
    )
    realtime_ha_parser.add_argument(
        "--markets",
        type=str,
        nargs="+",
        help="Specific markets to trade (default: all KRW markets)",
    )
    realtime_ha_parser.add_argument(
        "--initial-balance",
        type=float,
        default=INITIAL_BALANCE,
        help=f"Initial balance in KRW (default: {INITIAL_BALANCE:,.0f})",
    )
    realtime_ha_parser.add_argument(
        "--live",
        action="store_true",
        help="Live mode: read portfolio from Redis (trade-service integration)",
    )
    realtime_ha_parser.add_argument(
        "--user-id",
        type=str,
        default=os.getenv("USER_ID", "default_user"),
        help="User ID for Redis portfolio (default: default_user or USER_ID env)",
    )
    realtime_ha_parser.add_argument(
        "--lease-name",
        type=str,
        default=os.getenv("LEASE_NAME", "trading-engine-lock"),
        help="K8s Lease name for Leader Election (default: trading-engine-lock)",
    )
    realtime_ha_parser.add_argument(
        "--strategy",
        type=str,
        default="ml",
        choices=["multi", "drop-bounce", "ml"],
        help="Trading strategy to use (default: ml)",
    )

    args = parser.parse_args()

    if args.command == "backtest":
        # 파라미터 로드
        if args.params_csv:
            params = load_single_params_from_csv(args.params_csv)
            if params is None:
                main_logger.error(
                    "params_load_failed",
                    path=args.params_csv,
                )
                return
        else:
            # 기본 파라미터 또는 CLI 옵션으로 생성
            params = Params()

            # CLI 옵션으로 오버라이드
            if args.short_tf is not None:
                params.short_timeframe_minutes = args.short_tf
            if args.stage1_change is not None:
                params.buy_stage1_change_pct = args.stage1_change
            if args.stage1_min_coins is not None:
                params.buy_stage1_min_coins = args.stage1_min_coins
            if args.stage2_change is not None:
                params.buy_stage2_change_pct = args.stage2_change
            if args.stage3_volume is not None:
                params.buy_stage3_volume_surge = args.stage3_volume
            if args.investment_ratio is not None:
                params.investment_ratio = args.investment_ratio
            if args.stop_loss is not None:
                params.stop_loss = args.stop_loss
            if args.trailing_target_early is not None:
                params.trailing_target_early = args.trailing_target_early
            if args.trailing_exit_early is not None:
                params.trailing_exit_early = args.trailing_exit_early
            if args.trailing_target is not None:
                params.trailing_target = args.trailing_target
            if args.trailing_exit is not None:
                params.trailing_exit = args.trailing_exit
            if args.trailing_drop is not None:
                params.trailing_drop = args.trailing_drop

        run_backtest(
            params=params,
            period_name=args.period,
            markets=args.markets,
            log_enabled=args.log,
            log_file=args.log_file,
            strategy_type=args.strategy,
        )

    elif args.command == "realtime":
        # 파라미터 로드
        if args.params_csv:
            params = load_single_params_from_csv(args.params_csv)
            if params is None:
                main_logger.error(
                    "params_load_failed",
                    path=args.params_csv,
                )
                return
        else:
            params = Params()

            # CLI 옵션으로 오버라이드
            if args.short_tf is not None:
                params.short_timeframe_minutes = args.short_tf
            if args.stage1_change is not None:
                params.buy_stage1_change_pct = args.stage1_change
            if args.stage1_min_coins is not None:
                params.buy_stage1_min_coins = args.stage1_min_coins
            if args.investment_ratio is not None:
                params.investment_ratio = args.investment_ratio
            if args.stop_loss is not None:
                params.stop_loss = args.stop_loss

        run_realtime(
            params=params,
            markets=args.markets,
            initial_balance=args.initial_balance,
            live_mode=args.live,
            user_id=args.user_id,
            strategy_type=args.strategy,
        )

    elif args.command == "monitor":
        # 기본 파라미터 로드 (trailing_target_early, trailing_exit_early, stop_loss 사용)
        params = Params()

        run_monitor(
            params=params,
            user_id=args.user_id,
        )

    elif args.command == "realtime-ha":
        # 파라미터 로드
        if args.params_csv:
            params = load_single_params_from_csv(args.params_csv)
            if params is None:
                main_logger.error(
                    "params_load_failed",
                    path=args.params_csv,
                )
                return
        else:
            params = Params()

        run_realtime_ha(
            params=params,
            markets=args.markets,
            initial_balance=args.initial_balance,
            live_mode=args.live,
            user_id=args.user_id,
            lease_name=args.lease_name,
            strategy_type=args.strategy,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
