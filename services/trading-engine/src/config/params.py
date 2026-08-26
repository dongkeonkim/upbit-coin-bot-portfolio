"""전략 파라미터 정의 (Go config/config.go와 동일)"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict


# 백테스트 기간 정의 (Go와 동일)
PERIODS: Dict[str, tuple] = {
    "P1": (
        datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc),
    ),
    "P2": (
        datetime(2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 11, 15, 0, 0, 0, tzinfo=timezone.utc),
    ),
    "P3": (
        datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 4, 17, 0, 0, 0, tzinfo=timezone.utc),
    ),
    "FULL": (
        datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 11, 23, 0, 0, 0, tzinfo=timezone.utc),
    ),
}

# 상수 (Go와 동일)
INITIAL_BALANCE = 1_000_000.0  # 초기 잔고 (100만원)
FEE_RATE = 0.0005  # 거래 수수료율 0.05%
LONG_TIMEFRAME_MINUTES = 180  # 장기 타임프레임 (3시간)
TRADE_CHECK_TIMEFRAME_MINUTES = 1  # 매수 체크용 타임프레임 (1분)
BUY_STAGE1_ALT_TOP_N = 10  # Stage 1 대체 조건용 상위 N개 코인


@dataclass
class Params:
    """전략 파라미터 (Go Params 구조체와 동일) - Trial #731 적용 (부분 매도 지원)"""

    # 기본 매수 조건 (Trial #731 - avg_return: 65.59%, win_rate: 76.50%, trades: 1581)
    # P1: 116.48%, P2: 73.30%, P3: 6.97%
    short_timeframe_minutes: int = 9
    volume_avg_window_minutes: int = 190
    buy_stage1_change_pct: float = 1.3
    buy_stage1_min_coins: int = 5
    buy_stage1_sliding_window: bool = True
    buy_stage1_alt_change_pct: float = 1.7
    buy_stage1_alt_min_coins: int = 4
    buy_stage2_change_pct: float = 0.15
    buy_stage3_volume_surge: float = 1.4
    investment_ratio: float = 0.27
    stop_loss: float = -6.0
    trailing_target_early: float = 5.5
    trailing_drop_early: float = 0.7
    trailing_exit_early: float = 0.31
    trailing_target: float = 9.5  # ANCA trailing target
    trailing_exit: float = 1.1  # ANCA trailing exit
    trailing_drop: float = 37.0  # ANCA trailing drop

    # Market Filter 기능 (비활성화)
    market_filter_enabled: bool = False
    market_filter_ma_days: int = 1
    market_filter_bear_threshold: float = -9.5
    market_filter_bull_threshold: float = 1.5
    market_filter_sideways_reduction: float = 0.1

    # Dynamic Position 기능 (활성화)
    dynamic_position_enabled: bool = True
    dynamic_position_strong_ratio: float = 0.35
    dynamic_position_medium_ratio: float = 0.24
    dynamic_position_weak_ratio: float = 0.09
    dynamic_position_strong_threshold: float = 4.0
    dynamic_position_medium_threshold: float = 1.2

    # Improved Stop Loss 기능 (비활성화 - 기본 stop_loss 사용)
    improved_stop_loss_enabled: bool = False
    improved_stop_loss_bull: float = -3.8
    improved_stop_loss_sideways: float = -5.8

    # RSI 지표 (비활성화)
    rsi_enabled: bool = False
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0

    # 볼린저 밴드 (비활성화)
    bb_enabled: bool = False
    bb_period: int = 20
    bb_std_dev: float = 2.0

    # ATR (Average True Range) (비활성화)
    atr_enabled: bool = False
    atr_period: int = 14
    atr_multiplier: float = 2.0

    # ADX (Average Directional Index) (비활성화)
    adx_enabled: bool = False
    adx_period: int = 14
    adx_threshold: float = 25.0

    # 부분 매도 (Partial Sell) 기능 (활성화 - Trial #731)
    partial_sell_enabled: bool = True  # 부분 매도 활성화
    early_partial_sell_ratio: float = 0.5  # 1차 부분 매도 비율 (50%)
    early_defense_threshold: float = 0.61  # 방어선 수익률 (%) = early_exit(0.31) + offset(0.3)


# 최적화 Trial #731 전략 (HIGH_TRADES - 부분 매도 활성화)
# avg_return: 65.59%, win_rate: 76.50%, total_trades: 1581
# P1: 116.48%, P2: 73.30%, P3: 6.97%
BEST_STRATEGY = Params(
    # 기본 전략 (Trial #731)
    short_timeframe_minutes=9,
    volume_avg_window_minutes=190,
    investment_ratio=0.27,
    stop_loss=-6.0,
    # 매수 조건 (4단계)
    buy_stage1_change_pct=1.3,
    buy_stage1_min_coins=5,
    buy_stage1_sliding_window=True,
    buy_stage1_alt_change_pct=1.7,
    buy_stage1_alt_min_coins=4,
    buy_stage2_change_pct=0.15,
    buy_stage3_volume_surge=1.4,
    # 매도 조건 (EARLY - 부분 매도 지원)
    trailing_target_early=5.5,
    trailing_drop_early=0.7,
    trailing_exit_early=0.31,
    # 매도 조건 (ANCA)
    trailing_target=9.5,
    trailing_exit=1.1,
    trailing_drop=37.0,
    # Market Filter (비활성화)
    market_filter_enabled=False,
    market_filter_ma_days=1,
    market_filter_bear_threshold=-9.5,
    market_filter_bull_threshold=1.5,
    market_filter_sideways_reduction=0.1,
    # Dynamic Position (활성화)
    dynamic_position_enabled=True,
    dynamic_position_strong_ratio=0.35,
    dynamic_position_medium_ratio=0.24,
    dynamic_position_weak_ratio=0.09,
    dynamic_position_strong_threshold=4.0,
    dynamic_position_medium_threshold=1.2,
    # Improved Stop Loss (비활성화 - 기본 stop_loss 사용)
    improved_stop_loss_enabled=False,
    improved_stop_loss_bull=-3.8,
    improved_stop_loss_sideways=-5.8,
    # 기술 지표 (비활성화)
    rsi_enabled=False,
    rsi_period=14,
    rsi_overbought=70.0,
    rsi_oversold=30.0,
    bb_enabled=False,
    bb_period=20,
    bb_std_dev=2.0,
    atr_enabled=False,
    atr_period=14,
    atr_multiplier=2.0,
    adx_enabled=False,
    adx_period=14,
    adx_threshold=25.0,
    # 부분 매도 (활성화 - 핵심!)
    partial_sell_enabled=True,
    early_partial_sell_ratio=0.5,  # 1차 부분 매도 50%
    early_defense_threshold=0.61,  # 방어선 = early_exit(0.31) + offset(0.3)
)


# ML 전략 전용 파라미터 (strategy_ml_trailing.py 기반)
# 트리거: +1.8% / 하락폭: -0.3% / SL: -1.0%
ML_STRATEGY = Params(
    # 기본 설정 (ML 전략은 자체 매수 로직 사용)
    short_timeframe_minutes=9,
    volume_avg_window_minutes=190,
    investment_ratio=0.27,
    # ML 전략 매도 조건 (타이트)
    stop_loss=-1.0,               # 손절 -1.0%
    trailing_target_early=1.8,    # 트레일링 트리거 +1.8%
    trailing_drop_early=0.3,      # 고점 대비 하락폭 0.3%
    trailing_exit_early=0.0,      # 미사용 (트레일링 하락으로 청산)
    # ANCA 매도 조건 (ML 전략에서는 미사용, 시간 만료로 청산)
    trailing_target=99.0,         # 도달 불가능한 값
    trailing_exit=99.0,
    trailing_drop=99.0,
    # 매수 조건 (ML 전략은 자체 매수 로직 사용, 무시됨)
    buy_stage1_change_pct=1.3,
    buy_stage1_min_coins=5,
    buy_stage1_sliding_window=True,
    buy_stage1_alt_change_pct=1.7,
    buy_stage1_alt_min_coins=4,
    buy_stage2_change_pct=0.15,
    buy_stage3_volume_surge=1.4,
    # Market Filter (비활성화)
    market_filter_enabled=False,
    # Dynamic Position (비활성화 - ML은 고정 투자비율)
    dynamic_position_enabled=False,
    # Improved Stop Loss (비활성화)
    improved_stop_loss_enabled=False,
    # 기술 지표 (비활성화)
    rsi_enabled=False,
    bb_enabled=False,
    atr_enabled=False,
    adx_enabled=False,
    # 부분 매도 (비활성화 - ML 전략은 전량 청산)
    partial_sell_enabled=False,
)
