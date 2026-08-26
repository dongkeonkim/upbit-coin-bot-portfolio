"""CSV 파라미터 로더 (Go config/csv_loader.go와 동일)"""

import csv
from pathlib import Path
from typing import List, Optional

from .params import Params


# CSV 컬럼명 -> Python 속성명 매핑 (Go와 동일)
COLUMN_MAPPING = {
    "short_tf": "short_timeframe_minutes",
    "volume_avg_window": "volume_avg_window_minutes",
    "stage1_change_pct": "buy_stage1_change_pct",
    "stage1_min_coins": "buy_stage1_min_coins",
    "stage1_sliding_window": "buy_stage1_sliding_window",
    "stage1_alt_change_pct": "buy_stage1_alt_change_pct",
    "stage1_alt_min_coins": "buy_stage1_alt_min_coins",
    "stage2_change_pct": "buy_stage2_change_pct",
    "stage3_volume_surge": "buy_stage3_volume_surge",
    "investment_ratio": "investment_ratio",
    "stop_loss": "stop_loss",
    "early_trailing_target": "trailing_target_early",
    "early_trailing_drop": "trailing_drop_early",
    "early_trailing_exit": "trailing_exit_early",
    "anca_trailing_target": "trailing_target",
    "anca_trailing_exit": "trailing_exit",
    "anca_trailing_drop": "trailing_drop",
    "market_filter_enabled": "market_filter_enabled",
    "market_filter_ma_days": "market_filter_ma_days",
    "market_filter_bear_threshold": "market_filter_bear_threshold",
    "market_filter_bull_threshold": "market_filter_bull_threshold",
    "market_filter_sideways_reduction": "market_filter_sideways_reduction",
    "dynamic_position_enabled": "dynamic_position_enabled",
    "dynamic_position_strong_ratio": "dynamic_position_strong_ratio",
    "dynamic_position_medium_ratio": "dynamic_position_medium_ratio",
    "dynamic_position_weak_ratio": "dynamic_position_weak_ratio",
    "dynamic_position_strong_threshold": "dynamic_position_strong_threshold",
    "dynamic_position_medium_threshold": "dynamic_position_medium_threshold",
    "improved_stop_loss_enabled": "improved_stop_loss_enabled",
    "improved_stop_loss_bull": "improved_stop_loss_bull",
    "improved_stop_loss_sideways": "improved_stop_loss_sideways",
    "rsi_enabled": "rsi_enabled",
    "rsi_period": "rsi_period",
    "rsi_overbought": "rsi_overbought",
    "rsi_oversold": "rsi_oversold",
    "bb_enabled": "bb_enabled",
    "bb_period": "bb_period",
    "bb_std_dev": "bb_std_dev",
    "atr_enabled": "atr_enabled",
    "atr_period": "atr_period",
    "atr_multiplier": "atr_multiplier",
    "adx_enabled": "adx_enabled",
    "adx_period": "adx_period",
    "adx_threshold": "adx_threshold",
}

# 기본값 정의 (Go parseIntWithDefault, parseFloatWithDefault와 동일)
DEFAULT_VALUES = {
    "rsi_period": 14,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "bb_period": 20,
    "bb_std_dev": 2.0,
    "atr_period": 14,
    "atr_multiplier": 2.0,
    "adx_period": 14,
    "adx_threshold": 25.0,
}


def parse_int(value: str, default: int = 0) -> int:
    """정수 파싱 (Go parseInt와 동일)"""
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_float(value: str, default: float = 0.0) -> float:
    """실수 파싱 (Go parseFloat와 동일)"""
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_bool(value: str) -> bool:
    """불린 파싱 (Go parseBool와 동일)"""
    if not value:
        return False
    val = value.strip().lower()
    return val in ("true", "1")


def load_params_from_csv(filename: str) -> List[Params]:
    """
    CSV 파일에서 파라미터 조합 로드 (Go LoadParamsFromCSV와 동일)

    Args:
        filename: CSV 파일 경로

    Returns:
        파라미터 리스트
    """
    filepath = Path(filename)
    if not filepath.exists():
        raise FileNotFoundError(f"CSV file not found: {filename}")

    params_list: List[Params] = []

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # index 컬럼이 비어있으면 건너뛰기 (불완전한 행)
            if not row.get("short_tf", "").strip() and not row.get("index", "").strip():
                continue

            # 파라미터 파싱
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
                trailing_target_early=parse_float(row.get("early_trailing_target", "0.5")),
                trailing_drop_early=parse_float(row.get("early_trailing_drop", "0.35")),
                trailing_exit_early=parse_float(row.get("early_trailing_exit", "0.05")),
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
                # 기술 지표
                rsi_enabled=parse_bool(row.get("rsi_enabled", "false")),
                rsi_period=parse_int(row.get("rsi_period", "14"), DEFAULT_VALUES["rsi_period"]),
                rsi_overbought=parse_float(row.get("rsi_overbought", "70.0")),
                rsi_oversold=parse_float(row.get("rsi_oversold", "30.0")),
                bb_enabled=parse_bool(row.get("bb_enabled", "false")),
                bb_period=parse_int(row.get("bb_period", "20"), DEFAULT_VALUES["bb_period"]),
                bb_std_dev=parse_float(row.get("bb_std_dev", "2.0")),
                atr_enabled=parse_bool(row.get("atr_enabled", "false")),
                atr_period=parse_int(row.get("atr_period", "14"), DEFAULT_VALUES["atr_period"]),
                atr_multiplier=parse_float(row.get("atr_multiplier", "2.0")),
                adx_enabled=parse_bool(row.get("adx_enabled", "false")),
                adx_period=parse_int(row.get("adx_period", "14"), DEFAULT_VALUES["adx_period"]),
                adx_threshold=parse_float(row.get("adx_threshold", "25.0")),
            )

            params_list.append(params)

    return params_list


def load_single_params_from_csv(filename: str) -> Optional[Params]:
    """
    CSV 파일에서 단일 파라미터 로드 (첫 번째 행만)

    Args:
        filename: CSV 파일 경로

    Returns:
        파라미터 또는 None
    """
    params_list = load_params_from_csv(filename)
    return params_list[0] if params_list else None
