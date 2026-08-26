"""Slack Incoming Webhook Client for notifications"""

import os
import socket
import httpx
from typing import Optional
from datetime import datetime

from ..utils import get_logger

logger = get_logger(__name__)


class SlackClient:
    """
    Slack Incoming Webhook Client with fire-and-forget pattern.

    All notification methods are designed to never raise exceptions,
    ensuring that trading operations are not affected by notification failures.
    """

    def __init__(self, webhook_url: str, service_name: str = "trading-engine"):
        """
        Initialize SlackClient.

        Args:
            webhook_url: Slack Incoming Webhook URL. Empty string disables all notifications.
            service_name: Service name for logging context.
        """
        self.webhook_url = webhook_url
        self.service_name = service_name
        self._client: Optional[httpx.Client] = None

        if webhook_url:
            self._client = httpx.Client(timeout=10.0)
            logger.info("slack_client_initialized", service=service_name)
        else:
            logger.debug("slack_disabled", reason="webhook_url not configured")

    def _send_message(self, text: str) -> bool:
        """
        Send message to Slack. Internal method with error handling.

        Returns True on success, False on failure (never raises).
        """
        if not self._client or not self.webhook_url:
            return False

        try:
            response = self._client.post(
                self.webhook_url,
                json={"text": text},
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 200:
                logger.debug("slack_message_sent", service=self.service_name)
                return True
            else:
                logger.warning(
                    "slack_send_failed",
                    status_code=response.status_code,
                    response=response.text[:200]
                )
                return False
        except Exception as e:
            logger.error("slack_error", error=str(e), service=self.service_name)
            return False

    def _format_timestamp(self) -> str:
        """Format current timestamp in KST."""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S KST")

    def send_buy_signal(
        self,
        market: str,
        price: float,
        amount: float,
        reason: str,  # 현재 미사용, 향후 확장용
        mode: str = "LIVE",
        portfolio_value: Optional[float] = None,
    ) -> bool:
        """Send buy signal notification."""
        _ = reason  # unused, for future use

        # 포트폴리오 정보
        portfolio_info = ""
        if portfolio_value is not None:
            portfolio_info = (
                f"\n*── Portfolio ──*\n"
                f"💰 {portfolio_value:,.0f} KRW"
            )

        text = (
            f":chart_with_upwards_trend: *BUY Signal*\n"
            f"Market: `{market}`\n"
            f"Price: {price:,.0f} KRW\n"
            f"Amount: {amount:,.0f} KRW"
            f"{portfolio_info}\n"
            f"Mode: {mode}\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def send_sell_signal(
        self,
        market: str,
        price: float,
        volume: float,
        reason: str,
        mode: str = "LIVE",
        avg_buy_price: Optional[float] = None,
        portfolio_value: Optional[float] = None,
    ) -> bool:
        """Send sell signal notification with profit/loss info."""
        # 매도 사유 한글화
        reason_map = {
            "stop_loss": "손절매 (Stop Loss)",
            "trailing_stop_early": "추적 손절 (Trailing Stop)",
            "anca_exit_condition": "ANCA 수익 실현",
            "anca_drop_condition": "ANCA 고점 대비 하락",
            "volume_rank_exit": "거래량 순위 이탈",
            "period_end": "기간 종료",
        }
        reason_display = reason_map.get(reason, reason)

        # 이번 거래 손익
        trade_pnl = ""
        if avg_buy_price is not None:
            pnl_amount = (price - avg_buy_price) * volume
            emoji = "📈" if pnl_amount >= 0 else "📉"
            sign = "+" if pnl_amount >= 0 else ""
            trade_pnl = f"\n{emoji} 거래 손익: {sign}{pnl_amount:,.0f} KRW"

        # 포트폴리오 정보
        portfolio_info = ""
        if portfolio_value is not None:
            portfolio_info = (
                f"\n*── Portfolio ──*\n"
                f"💰 {portfolio_value:,.0f} KRW"
            )

        text = (
            f":chart_with_downwards_trend: *SELL Signal*\n"
            f"Market: `{market}`\n"
            f"Price: {price:,.0f} KRW\n"
            f"Volume: {volume:.8f}\n"
            f"📍 Reason: {reason_display}"
            f"{trade_pnl}"
            f"{portfolio_info}\n"
            f"Mode: {mode}\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def send_rate_limit_alert(
        self,
        endpoint: str,
        retry_after: Optional[int] = None,
    ) -> bool:
        """Send rate limit (429) alert notification."""
        retry_info = f"\nRetry After: {retry_after}s" if retry_after else ""
        text = (
            f":warning: *RATE LIMIT (429): {self.service_name}*\n"
            f"Endpoint: {endpoint}{retry_info}\n"
            f"Action: API 요청 속도 제한 초과. 잠시 대기 후 재시도.\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def send_stale_data_alert(
        self,
        stale_markets: list,
        mode: str,
        user_id: str,
        status_msg: str = "Strategy execution STOPPED. Check market-data-service."
    ) -> bool:
        """Send stale data alert notification."""
        top3 = stale_markets[:3]
        markets_text = "\n".join([f"  - {m}: {int(d)}s delay" for m, d in top3])

        text = (
            f":rotating_light: *STALE DATA ALERT*\n"
            f"Mode: {mode}\n"
            f"User: {user_id}\n"
            f"Stale Markets: {len(stale_markets)}\n"
            f"Top 3:\n{markets_text}\n\n"
            f"{status_msg}\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def send_service_started(
        self,
        mode: str = "LIVE",
        user_id: str = "default_user",
        ha_mode: bool = False,
        strategy: str = "drop-bounce",
    ) -> bool:
        """Send service started notification."""
        ha_text = " (HA Leader)" if ha_mode else ""
        hostname = os.environ.get("HOSTNAME", socket.gethostname())
        text = (
            f":rocket: *SERVICE STARTED: {self.service_name}{ha_text}*\n"
            f"Mode: {mode}\n"
            f"Strategy: {strategy}\n"
            f"Host: {hostname}\n"
            f"User: {user_id}\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def send_service_stopping(self, reason: str = "graceful shutdown") -> bool:
        """Send service stopping notification."""
        text = (
            f":wave: *SERVICE STOPPING: {self.service_name}*\n"
            f"Reason: {reason}\n"
            f"Time: {self._format_timestamp()}"
        )
        return self._send_message(text)

    def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            self._client.close()
            self._client = None
            logger.debug("slack_client_closed", service=self.service_name)
