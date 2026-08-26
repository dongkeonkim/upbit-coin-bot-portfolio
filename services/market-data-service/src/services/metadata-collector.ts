/**
 * 마켓 메타데이터 수집 서비스
 * 모든 KRW 마켓의 상장일(일봉 개수) 정보를 수집하여 Redis에 저장
 */

import { UpbitClient, RateLimitError } from '../clients/upbit-client';
import { SlackClient } from '../clients/slack-client';
import { RedisClient } from '../utils/redis-client';
import logger from '../utils/logger';

export class MetadataCollector {
  private isRunning: boolean = false;
  private refreshTimer?: NodeJS.Timeout;
  private readonly MAX_RETRIES = 3; // 마켓별 최대 재시도 횟수

  constructor(
    private readonly upbitClient: UpbitClient,
    private readonly redisClient: RedisClient,
    private readonly slackClient?: SlackClient,
    private readonly refreshIntervalMs: number = 24 * 60 * 60 * 1000 // 24시간
  ) {}

  /**
   * 메타데이터 수집 시작
   * 즉시 한 번 수집 후 주기적으로 갱신
   */
  async start(): Promise<void> {
    if (this.isRunning) {
      logger.warn('MetadataCollector already running');
      return;
    }

    this.isRunning = true;
    logger.info('Starting metadata collection...');

    // 즉시 수집
    await this.collectMetadata();

    // 주기적 갱신 (24시간마다)
    this.refreshTimer = setInterval(() => {
      this.collectMetadata();
    }, this.refreshIntervalMs);

    logger.info(
      { refreshIntervalHours: this.refreshIntervalMs / (60 * 60 * 1000) },
      'MetadataCollector started'
    );
  }

  /**
   * 메타데이터 수집 중지
   */
  stop(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = undefined;
    }
    this.isRunning = false;
    logger.info('MetadataCollector stopped');
  }

  /**
   * 모든 마켓의 메타데이터 수집
   */
  private async collectMetadata(): Promise<void> {
    const startTime = Date.now();
    logger.info('Collecting market metadata...');

    try {
      // 1. 모든 KRW 마켓 조회
      const markets = await this.upbitClient.getMarkets();
      const marketCodes = markets.map((m) => m.market);

      logger.info({ marketCount: marketCodes.length }, 'Found KRW markets');

      // 2. 각 마켓의 일봉 조회하여 상장일 계산
      const metadata: Record<string, number> = {};
      let successCount = 0;
      let errorCount = 0;

      for (let i = 0; i < marketCodes.length; i++) {
        const market = marketCodes[i];
        const result = await this.collectMarketMetadata(market);

        if (result.success) {
          metadata[market] = result.listingDays!;
          successCount++;
        } else {
          errorCount++;
        }

        // 진행 상황 로깅 (50개마다)
        if ((i + 1) % 50 === 0) {
          logger.debug(
            { checked: i + 1, total: marketCodes.length, successCount },
            'Metadata collection progress'
          );
        }

        // Rate limit 방지: 200ms 대기
        if (i < marketCodes.length - 1) {
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
      }

      // 3. Redis에 저장 (24시간 TTL)
      if (Object.keys(metadata).length > 0) {
        await this.redisClient.setMarketMetadata(metadata, 86400);
      }

      const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);
      logger.info(
        {
          totalMarkets: marketCodes.length,
          successCount,
          errorCount,
          elapsedSec,
        },
        'Market metadata collection completed'
      );
    } catch (error) {
      logger.error({ error }, 'Failed to collect market metadata');

      if (this.slackClient) {
        await this.slackClient.sendError(
          'MetadataCollector.collectMetadata',
          (error as Error).message
        );
      }
    }
  }

  /**
   * 개별 마켓의 메타데이터 수집 (재시도 로직 포함)
   */
  private async collectMarketMetadata(
    market: string
  ): Promise<{ success: boolean; listingDays?: number }> {
    let retryCount = 0;

    while (retryCount <= this.MAX_RETRIES) {
      try {
        const candles = await this.upbitClient.getCandlesDay(market, 200);
        return { success: true, listingDays: candles.length };
      } catch (error) {
        if (error instanceof RateLimitError) {
          retryCount++;
          const waitTime = (error.retryAfter ?? 1) * 1000;

          logger.warn({
            event: 'rate_limit_hit',
            market,
            endpoint: error.endpoint,
            retryAfter: error.retryAfter,
            retryCount,
            maxRetries: this.MAX_RETRIES,
          });

          if (retryCount > this.MAX_RETRIES) {
            // 재시도 횟수 초과 - Slack 알림 후 스킵
            logger.error(
              { market, retryCount },
              'Max retries exceeded for market metadata collection'
            );

            if (this.slackClient) {
              await this.slackClient.sendError(
                'MetadataCollector.collectMarketMetadata',
                `${market} 메타데이터 수집 실패: Rate Limit으로 인해 ${this.MAX_RETRIES}회 재시도 후에도 실패. 다음 주기(12시간 후)에 재시도됩니다.`
              );
            }

            return { success: false };
          }

          // 대기 후 재시도
          await new Promise((resolve) => setTimeout(resolve, waitTime));
        } else {
          // Rate Limit 외 에러는 재시도하지 않고 바로 실패 처리
          logger.warn(
            { market, error: (error as Error).message },
            'Failed to get candles for market'
          );
          return { success: false };
        }
      }
    }

    return { success: false };
  }
}
