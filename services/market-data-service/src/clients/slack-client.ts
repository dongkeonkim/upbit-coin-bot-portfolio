/**
 * Slack Incoming Webhook Client for notifications
 *
 * Fire-and-forget pattern: All notification methods are designed to never throw,
 * ensuring that data collection operations are not affected by notification failures.
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

  constructor(webhookUrl: string, serviceName: string = 'market-data-service') {
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

  private async sendMessage(text: string): Promise<boolean> {
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

  async sendRateLimitAlert(endpoint: string, retryAfter?: number): Promise<boolean> {
    const retryInfo = retryAfter ? `\nRetry After: ${retryAfter}s` : '';
    const text =
      `:warning: *RATE LIMIT (429): ${this.serviceName}*\n` +
      `Endpoint: ${endpoint}${retryInfo}\n` +
      `Action: API 요청 속도 제한 초과. 잠시 대기 후 재시도.\n` +
      `Time: ${this.formatTimestamp()}`;

    return this.sendMessage(text);
  }
}
