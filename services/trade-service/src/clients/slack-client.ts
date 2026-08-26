/**
 * Slack Incoming Webhook Client for notifications
 *
 * Fire-and-forget pattern: All notification methods are designed to never throw,
 * ensuring that trading operations are not affected by notification failures.
 */

import axios from 'axios';
import * as os from 'os';
import logger from '../utils/logger';

export interface ServiceStartInfo {
  mode: 'LIVE' | 'SIMULATION';
  hostname?: string;
}

export class SlackClient {
  private readonly webhookUrl: string;
  private readonly serviceName: string;
  private readonly enabled: boolean;

  constructor(webhookUrl: string, serviceName: string = 'trade-service') {
    this.webhookUrl = webhookUrl;
    this.serviceName = serviceName;
    this.enabled = !!webhookUrl;

    if (this.enabled) {
      logger.info({ event: 'slack_client_initialized', service: serviceName });
    } else {
      logger.debug({ event: 'slack_disabled', reason: 'webhook_url not configured' });
    }
  }

  private formatTimestamp(): string {
    return new Date().toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' }) + ' KST';
  }

  async sendMessage(text: string): Promise<boolean> {
    if (!this.enabled) {
      return false;
    }

    try {
      const response = await axios.post(
        this.webhookUrl,
        { text },
        {
          headers: { 'Content-Type': 'application/json' },
          timeout: 10000,
        }
      );

      if (response.status === 200) {
        logger.debug({ event: 'slack_message_sent', service: this.serviceName });
        return true;
      }

      logger.warn({
        event: 'slack_send_failed',
        status: response.status,
        data: String(response.data).slice(0, 200),
      });
      return false;
    } catch (error) {
      logger.error({
        event: 'slack_error',
        error: error instanceof Error ? error.message : String(error),
        service: this.serviceName,
      });
      return false;
    }
  }

  async sendOrderExecution(
    market: string,
    side: 'BUY' | 'SELL',
    price: number,
    volume: number,
    orderId: string,
    pnl?: number  // SELL 시 실제 손익 (KRW)
  ): Promise<boolean> {
    const emoji = side === 'BUY' ? ':white_check_mark:' : ':money_with_wings:';
    // BUY: price는 총 투자금액, SELL: price는 코인당 가격
    const priceLabel = side === 'BUY' ? 'Amount' : 'Price';

    let text =
      `${emoji} *ORDER EXECUTED: ${side} ${market}*\n` +
      `${priceLabel}: ${price.toLocaleString()} KRW\n` +
      `Volume: ${volume}\n`;

    // SELL 시 실제 손익 표시
    if (side === 'SELL' && pnl !== undefined) {
      const pnlEmoji = pnl >= 0 ? '📈' : '📉';
      const sign = pnl >= 0 ? '+' : '';
      text += `${pnlEmoji} PnL: ${sign}${Math.round(pnl).toLocaleString()} KRW\n`;
    }

    text +=
      `Order ID: \`${orderId}\`\n` +
      `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }

  async sendServiceStarted(info?: ServiceStartInfo): Promise<boolean> {
    const mode = info?.mode || 'SIMULATION';
    const hostname = info?.hostname || process.env.HOSTNAME || os.hostname();

    const text =
      `:rocket: *SERVICE STARTED: ${this.serviceName}*\n` +
      `Mode: ${mode}\n` +
      `Host: ${hostname}\n` +
      `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }

  async sendServiceStopping(reason: string = 'graceful shutdown'): Promise<boolean> {
    const text =
      `:wave: *SERVICE STOPPING: ${this.serviceName}*\n` +
      `Reason: ${reason}\n` +
      `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }

  async sendError(context: string, error: string): Promise<boolean> {
    const text =
      `:x: *ERROR: ${this.serviceName}*\n` +
      `Context: ${context}\n` +
      `Error: ${error}\n` +
      `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }

  /**
   * Send Upbit API error notification
   */
  async sendUpbitApiError(
    operation: 'BUY' | 'SELL' | 'GET_BALANCE',
    market: string,
    errorMessage: string,
    details?: { volume?: number; amount?: number; statusCode?: number }
  ): Promise<boolean> {
    const operationEmoji: Record<string, string> = {
      BUY: ':no_entry:',
      SELL: ':no_entry:',
      GET_BALANCE: ':warning:',
    };

    const operationText: Record<string, string> = {
      BUY: '매수 실패',
      SELL: '매도 실패',
      GET_BALANCE: '잔고 조회 실패',
    };

    let text =
      `${operationEmoji[operation]} *UPBIT API ERROR: ${operationText[operation]}*\n` +
      `Market: \`${market}\`\n` +
      `Error: ${errorMessage}\n`;

    if (details?.amount) {
      text += `Amount: ${details.amount.toLocaleString()} KRW\n`;
    }
    if (details?.volume) {
      text += `Volume: ${details.volume.toFixed(8)}\n`;
    }
    if (details?.statusCode) {
      text += `Status Code: ${details.statusCode}\n`;
    }

    text += `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }
}
