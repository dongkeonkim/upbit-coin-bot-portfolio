"""
Production-grade structured logging using structlog

프로덕션 환경에서는 JSON 로깅, 개발 환경에서는 pretty print 출력을 제공합니다.
TypeScript 서비스의 Pino 로거와 동일한 철학을 따릅니다.
"""

import os
import sys
from typing import Any, Dict, List, Optional

import structlog


# 환경 감지
IS_PRODUCTION = os.getenv("ENVIRONMENT", "development") == "production"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# 로그 레벨 매핑
LEVEL_MAP = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_configured = False


def _filter_by_level(logger, method_name: str, event_dict: dict) -> dict:
    """LOG_LEVEL 기준으로 로그 필터링"""
    level_name = event_dict.get("level", "info").upper()
    current_level = LEVEL_MAP.get(level_name, 20)
    min_level = LEVEL_MAP.get(LOG_LEVEL, 20)

    if current_level < min_level:
        raise structlog.DropEvent

    return event_dict


def configure_logging() -> None:
    """structlog 설정 (프로덕션 또는 개발 모드)"""
    global _configured
    if _configured:
        return

    # 공통 프로세서 (로그 레벨 필터 포함)
    shared_processors = [
        structlog.stdlib.add_log_level,
        _filter_by_level,  # LOG_LEVEL 기준 필터링
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if IS_PRODUCTION:
        # 프로덕션: JSON 출력 (Loki/Grafana 친화적)
        structlog.configure(
            processors=shared_processors
            + [
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        # 개발: 컬러 콘솔 출력 (이모지 포함)
        structlog.configure(
            processors=shared_processors
            + [
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

    _configured = True


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """로거 인스턴스 반환"""
    configure_logging()
    return structlog.get_logger(name).bind(service="trading-engine")


class TradingLogger:
    """트레이딩 전용 로거 (이벤트 타입별 메서드 제공)"""

    def __init__(self):
        configure_logging()
        self._logger = structlog.get_logger("trading").bind(service="trading-engine")

    def signal(
        self,
        signal_type: str,
        market: str,
        price: Optional[float] = None,
        amount: Optional[float] = None,
        volume: Optional[float] = None,
        reason: Optional[str] = None,
        subscribers: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """트레이딩 신호 로깅"""
        data = {
            "signal_type": signal_type,
            "market": market,
        }
        if price is not None:
            data["price"] = price
        if amount is not None:
            data["amount"] = amount
        if volume is not None:
            data["volume"] = volume
        if reason is not None:
            data["reason"] = reason
        if subscribers is not None:
            data["subscribers"] = subscribers
        data.update(kwargs)

        self._logger.info("signal", **data)

    def position_update(
        self,
        market: str,
        action: str,
        profit_pct: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """포지션 업데이트 로깅"""
        data = {
            "market": market,
            "action": action,
        }
        if profit_pct is not None:
            data["profit_pct"] = profit_pct
        data.update(kwargs)

        self._logger.info("position_update", **data)

    def portfolio_update(
        self,
        portfolio_value: float,
        position_count: int,
        **kwargs: Any,
    ) -> None:
        """포트폴리오 상태 로깅"""
        data = {
            "portfolio_value": portfolio_value,
            "position_count": position_count,
        }
        data.update(kwargs)

        self._logger.info(
            "portfolio_update",
            **data,
        )

    def stop_loss(
        self,
        market: str,
        profit_pct: float,
        threshold: float,
        **kwargs: Any,
    ) -> None:
        """손절 트리거 로깅"""
        self._logger.warning(
            "stop_loss",
            market=market,
            profit_pct=profit_pct,
            threshold=threshold,
            **kwargs,
        )

    def target_hit(
        self,
        market: str,
        target_type: str,
        profit_pct: float,
        **kwargs: Any,
    ) -> None:
        """목표가 도달 로깅"""
        self._logger.info(
            "target_hit",
            market=market,
            target_type=target_type,
            profit_pct=profit_pct,
            **kwargs,
        )

    def condition_check(
        self,
        stage: str,
        passed: bool,
        **kwargs: Any,
    ) -> None:
        """조건 체크 로깅 (debug 레벨)"""
        self._logger.debug(
            "condition_check",
            stage=stage,
            passed=passed,
            **kwargs,
        )

    def iteration(
        self,
        iteration: int,
        market_count: int,
        **kwargs: Any,
    ) -> None:
        """반복 시작 로깅 (debug 레벨)"""
        self._logger.debug(
            "iteration",
            iteration=iteration,
            market_count=market_count,
            **kwargs,
        )

    def stale_data(
        self,
        stale_markets: List[tuple],
        **kwargs: Any,
    ) -> None:
        """데이터 지연 로깅 (10분 이상이면 error, 미만이면 warning)"""
        # 상위 5개만 로깅
        top_stale = stale_markets[:5]
        stale_data = [{"market": m, "delay_sec": d} for m, d in top_stale]

        # 10분(600초) 이상 지연된 마켓이 있으면 error
        max_delay = max((d for _, d in stale_markets), default=0)
        if max_delay >= 600:
            self._logger.error(
                "stale_data",
                stale_market_count=len(stale_markets),
                stale_markets=stale_data,
                max_delay_sec=max_delay,
                **kwargs,
            )
        else:
            self._logger.warning(
                "stale_data",
                stale_market_count=len(stale_markets),
                stale_markets=stale_data,
                max_delay_sec=max_delay,
                **kwargs,
            )

    def markets_loaded(
        self,
        market_count: int,
        source: str,
        **kwargs: Any,
    ) -> None:
        """마켓 로딩 완료 로깅"""
        self._logger.info(
            "markets_loaded",
            market_count=market_count,
            source=source,
            **kwargs,
        )

    def candles_updated(
        self,
        updated_count: int,
        total_markets: int,
        **kwargs: Any,
    ) -> None:
        """캔들 업데이트 로깅 (debug 레벨)"""
        self._logger.debug(
            "candles_updated",
            updated_count=updated_count,
            total_markets=total_markets,
            **kwargs,
        )

    def redis_connected(
        self,
        host: str,
        port: int,
        **kwargs: Any,
    ) -> None:
        """Redis 연결 성공 로깅"""
        self._logger.info(
            "redis_connected",
            host=host,
            port=port,
            **kwargs,
        )

    def redis_disconnected(self, **kwargs: Any) -> None:
        """Redis 연결 해제 로깅"""
        self._logger.info(
            "redis_disconnected",
            **kwargs,
        )

    def stage_analysis(
        self,
        market: str,
        stage1_passed: bool,
        stage2_passed: bool,
        stage3_passed: bool,
        all_passed: bool,
        **kwargs: Any,
    ) -> None:
        """스테이지 분석 결과 로깅 (info 레벨)"""
        self._logger.info(
            "stage_analysis",
            market=market,
            stage1_passed=stage1_passed,
            stage2_passed=stage2_passed,
            stage3_passed=stage3_passed,
            all_passed=all_passed,
            **kwargs,
        )


# 싱글톤 인스턴스
trading_logger = TradingLogger()
