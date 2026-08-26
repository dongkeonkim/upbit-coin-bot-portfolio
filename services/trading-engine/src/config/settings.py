"""애플리케이션 설정 (Go config와 동일한 기본값)"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Settings:
    """애플리케이션 설정 (Go config와 동일한 기본값)"""

    # 데이터베이스 설정
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_user: str = "coinbot"
    db_password: str = ""
    db_name: str = "coinbot"

    # Redis 설정 (실시간 모드용)
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0

    # Upbit API (실시간 모드용)
    upbit_access_key: str = ""
    upbit_secret_key: str = ""

    # Slack 알림
    slack_webhook_url: str = ""

    # 허용된 마켓 목록 (빈 문자열이면 모든 마켓 허용)
    # 예: "KRW-BTC,KRW-ETH,KRW-XRP"
    allowed_markets: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """환경변수에서 설정 로드"""
        return cls(
            # 데이터베이스 설정
            db_host=os.getenv("DB_HOST", "127.0.0.1"),
            db_port=int(os.getenv("DB_PORT", "5432")),
            db_user=os.getenv("DB_USER", "coinbot"),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_name=os.getenv("DB_NAME", "coinbot"),
            # Redis 설정
            redis_host=os.getenv("REDIS_HOST", "127.0.0.1"),
            redis_port=int(os.getenv("REDIS_PORT", "6379")),
            redis_db=int(os.getenv("REDIS_DB", "0")),
            # Upbit API
            upbit_access_key=os.getenv("UPBIT_ACCESS_KEY", ""),
            upbit_secret_key=os.getenv("UPBIT_SECRET_KEY", ""),
            # Slack 알림
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
            # 허용된 마켓 목록
            allowed_markets=os.getenv("ALLOWED_MARKETS", ""),
        )


# 기간 정의 (backtests/ml_optimize_per_coin.py와 동일)
PERIODS = {
    "P1": {
        "start": datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc),
    },
    "P2": {
        "start": datetime(2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2024, 11, 15, 0, 0, 0, tzinfo=timezone.utc),
    },
    "P3": {
        "start": datetime(2024, 11, 16, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2025, 1, 14, 0, 0, 0, tzinfo=timezone.utc),
    },
    "P4": {
        "start": datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2025, 4, 17, 0, 0, 0, tzinfo=timezone.utc),
    },
    "P5": {
        "start": datetime(2025, 10, 1, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2025, 12, 22, 0, 0, 0, tzinfo=timezone.utc),
    },
    "P6": {
        "start": datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc),
    },
    "FULL": {
        "start": datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2025, 12, 22, 0, 0, 0, tzinfo=timezone.utc),
    },
}
