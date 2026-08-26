/**
 * 캔들 수집 서비스
 * 주기적으로 1분봉 조회 후 Redis에 저장
 */

import { UpbitClient, RateLimitError } from '../clients/upbit-client';
import { RedisPublisher } from '../clients/redis-publisher';
import { PostgresClient } from '../clients/postgres-client';
import { SlackClient } from '../clients/slack-client';
import { KafkaCandleMessage } from '../types';
import logger, { loggers } from '../utils/logger';
import { RedisClient } from '../utils/redis-client';

export class CandleCollector {
  private intervalTimer?: NodeJS.Timeout;
  private isRunning: boolean = false;
  private rateLimitAlertSent: boolean = false; // 중복 알림 방지

  constructor(
    private readonly upbitClient: UpbitClient,
    private readonly redisPublisher: RedisPublisher,
    private readonly redisClient: RedisClient,
    private readonly userId: string,
    private readonly intervalMs: number = 60000, // 1분
    private readonly slackClient?: SlackClient,
    private readonly postgresClient?: PostgresClient
  ) {}

  /**
   * 주기적 캔들 수집 시작
   */
  start(): void {
    if (this.isRunning) {
      logger.warn('Already running');
      return;
    }

    this.isRunning = true;
    logger.info({ intervalMs: this.intervalMs }, 'Starting candle collection');

    // 매 분 시작 시점(1초)에 실행되도록 스케줄링 (시작 시 즉시 수집 안 함)
    this.scheduleNextCollection();
  }

  /**
   * 다음 분 시작 시점에 수집 스케줄링
   */
  private scheduleNextCollection(): void {
    const now = new Date();
    const nextMinute = new Date(now);
    nextMinute.setMinutes(nextMinute.getMinutes() + 1);
    nextMinute.setSeconds(1); // 매 분 1초에 실행
    nextMinute.setMilliseconds(0);

    const delay = nextMinute.getTime() - now.getTime();

    logger.info({
      currentTime: now.toISOString(),
      nextRun: nextMinute.toISOString(),
      delayMs: delay
    }, 'Scheduled next candle collection');

    this.intervalTimer = setTimeout(() => {
      this.collectCandles();
      // 다음 분에 재스케줄링
      this.scheduleNextCollection();
    }, delay);
  }

  /**
   * 캔들 수집 중지
   */
  stop(): void {
    if (this.intervalTimer) {
      clearTimeout(this.intervalTimer);
      this.intervalTimer = undefined;
    }

    this.isRunning = false;
    logger.info('Stopped candle collection');
  }

  /**
   * 전체 마켓 캔들 수집 후 Redis에 저장
   * 내부 타이머에 의해서만 트리거되는 순수 배치 작업
   */
  private async collectCandles(): Promise<void> {
    const timestamp = Date.now();

    // Redis에서 타겟 마켓 조회 (market-selector가 업데이트)
    const markets = await this.redisClient.getTargetMarkets(this.userId);

    if (!markets || markets.length === 0) {
      logger.warn('No target markets found in Redis');
      return;
    }

    logger.info(
      {
        timestamp: new Date(timestamp).toISOString(),
        marketCount: markets.length,
      },
      'Collecting candles'
    );

    const candles: KafkaCandleMessage[] = [];

    // 각 마켓별 캔들 조회 (Rate limiting 적용)
    // Upbit API 제한: 10 req/sec (public), 200ms 딜레이로 보수적 설정

    // 직전 분의 완료된 캔들을 조회하기 위해 현재 분 시작 시간을 to 파라미터로 사용
    const now = new Date();
    now.setSeconds(0, 0);
    const currentMinute = now.getTime();
    const toTime = now.toISOString().replace('.000Z', '');  // 2026-01-01T08:30:00 형식

    // 조회 대상: 직전 분 (currentMinute - 60000)
    const targetMinute = currentMinute - 60000;

    for (let i = 0; i < markets.length; i++) {
      const market = markets[i];

      try {
        // to 파라미터로 현재 분 시작 시간 전달 → 직전 분 캔들 반환
        const upbitCandles = await this.upbitClient.getCandles1m(market, 1, toTime);

        if (upbitCandles.length === 0) {
          continue;
        }

        const candle = upbitCandles[0];
        const candleTime = new Date(candle.candle_date_time_utc).getTime();

        // 거래가 없었던 분인지 확인 (Upbit 반환 캔들이 요청한 분보다 이전)
        const noTradeInTargetMinute = candleTime < targetMinute;

        // 우리 형식으로 변환
        // candle_time: 항상 요청한 분(targetMinute)으로 설정
        // volume: 거래가 없었으면 0으로 설정
        const kafkaCandle: KafkaCandleMessage = {
          market,
          timestamp: candle.timestamp,  // API 응답시간
          candle_time: targetMinute,    // 항상 요청한 분으로 설정
          open: candle.opening_price,
          high: candle.high_price,
          low: candle.low_price,
          close: candle.trade_price,
          volume: noTradeInTargetMinute ? 0 : candle.candle_acc_trade_volume,
          quote_volume: noTradeInTargetMinute ? 0 : candle.candle_acc_trade_price,
          interval: '1m',
        };

        candles.push(kafkaCandle);
        // 성공 시 rate limit 알림 플래그 리셋
        this.rateLimitAlertSent = false;
      } catch (error) {
        // 429 Rate Limit 에러 처리
        if (error instanceof RateLimitError) {
          logger.warn({
            event: 'rate_limit_hit',
            endpoint: error.endpoint,
            retryAfter: error.retryAfter,
          });

          // 중복 알림 방지: 한 번만 전송
          if (this.slackClient && !this.rateLimitAlertSent) {
            await this.slackClient.sendRateLimitAlert(error.endpoint, error.retryAfter);
            this.rateLimitAlertSent = true;
          }

          // Rate limit 시 추가 대기 (기본 1초, retry-after 있으면 해당 시간)
          const waitTime = (error.retryAfter ?? 1) * 1000;
          await new Promise((resolve) => setTimeout(resolve, waitTime));
          continue;
        }

        loggers.error(error as Error, {
          context: 'CandleCollector.collectCandles',
          market,
        });

        // 통신 실패 시 Slack 알림
        if (this.slackClient) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          await this.slackClient.sendError(
            `CandleCollector.collectCandles (${market})`,
            errorMessage
          );
        }
      }

      // Rate limit: 요청 간 200ms 대기 (~5 req/sec)
      if (i < markets.length - 1) {
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
    }

    // 수집된 캔들 일괄 저장
    if (candles.length > 0) {
      try {
        // Redis에 저장
        await this.redisPublisher.publishCandles(candles);
        logger.info({ candleCount: candles.length }, 'Published candles to Redis');

        // PostgreSQL에 저장 (활성화된 경우)
        if (this.postgresClient) {
          await this.postgresClient.saveCandles(candles);
        }
      } catch (error) {
        loggers.error(error as Error, {
          context: 'CandleCollector.publishCandles',
        });
      }
    }
  }
}
