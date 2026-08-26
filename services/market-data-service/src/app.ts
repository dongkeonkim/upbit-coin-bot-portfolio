/**
 * Market Data Service - Main Application
 * Fetches 1-minute candles for top 10 + held coins and publishes to Redis
 */

import { env } from './config/env';
import { UpbitClient } from './clients/upbit-client';
import { RedisPublisher } from './clients/redis-publisher';
import { PostgresClient } from './clients/postgres-client';
import { SlackClient } from './clients/slack-client';
import { RedisClient } from './utils/redis-client';
import { MarketSelector } from './services/market-selector';
import { CandleCollector } from './services/candle-collector';
import { MetadataCollector } from './services/metadata-collector';
import logger from './utils/logger';

class MarketDataService {
  private upbitClient!: UpbitClient;
  private redisPublisher!: RedisPublisher;
  private postgresClient!: PostgresClient;
  private slackClient?: SlackClient;
  private redisClient!: RedisClient;
  private marketSelector!: MarketSelector;
  private candleCollector!: CandleCollector;
  private metadataCollector!: MetadataCollector;
  private marketRefreshTimer?: NodeJS.Timeout;
  private isShuttingDown = false;

  // Configuration
  private readonly USER_ID = 'default_user';
  private readonly MARKET_REFRESH_INTERVAL = 60000; // 1 minute
  private readonly CANDLE_INTERVAL = env.CANDLE_INTERVAL_MINUTES * 60000;

  /**
   * Initialize the service
   */
  async initialize(): Promise<void> {
    logger.info('='.repeat(60));
    logger.info('Market Data Service Starting');
    logger.info('='.repeat(60));

    // Initialize clients
    this.upbitClient = new UpbitClient();
    this.redisClient = new RedisClient();

    this.redisPublisher = new RedisPublisher();
    await this.redisPublisher.connect();

    // Initialize PostgreSQL client (optional)
    this.postgresClient = new PostgresClient();
    if (env.POSTGRES_ENABLED) {
      await this.postgresClient.connect();
    }

    // Initialize Slack client (optional)
    if (env.SLACK_WEBHOOK_URL) {
      this.slackClient = new SlackClient(
        env.SLACK_WEBHOOK_URL,
        'market-data-service'
      );
    }

    // Initialize services
    this.marketSelector = new MarketSelector(
      this.upbitClient,
      this.redisClient
    );
    this.candleCollector = new CandleCollector(
      this.upbitClient,
      this.redisPublisher,
      this.redisClient,
      this.USER_ID,
      this.CANDLE_INTERVAL,
      this.slackClient, // 429 에러 알림용
      env.POSTGRES_ENABLED ? this.postgresClient : undefined
    );
    this.metadataCollector = new MetadataCollector(
      this.upbitClient,
      this.redisClient,
      this.slackClient,
      12 * 60 * 60 * 1000 // 12시간마다 갱신 (TTL 24시간의 절반 주기로 안전 마진 확보)
    );

    // Register shutdown handlers
    this.registerShutdownHandlers();

    logger.info('✓ Initialization complete\n');
  }

  /**
   * Start the service
   */
  async start(): Promise<void> {
    logger.info('📡 Market Data Service Running...\n');

    // Initial market selection (save to Redis)
    await this.refreshMarkets();

    // 1. 메타데이터 수집 먼저 완료 (블로킹)
    // - Trading Engine이 메타데이터를 먼저 기다림 (신규 코인 필터링 필요)
    // - 캔들 수집과 동시 실행 시 429 에러 발생하므로 순차 실행
    logger.info('Starting metadata collection (blocking)...');
    await this.metadataCollector.start();
    logger.info('✓ Metadata collection completed');

    // 2. 메타데이터 완료 후 캔들 수집 시작
    // - 이제 API 요청이 겹치지 않아 429 에러 방지
    logger.info('Starting candle collection...');
    this.candleCollector.start();

    // Periodic market refresh (only updates Redis)
    this.marketRefreshTimer = setInterval(() => {
      this.refreshMarkets();
    }, this.MARKET_REFRESH_INTERVAL);

    logger.info(
      {
        marketRefreshIntervalSec: this.MARKET_REFRESH_INTERVAL / 1000,
        candleIntervalSec: this.CANDLE_INTERVAL / 1000,
      },
      '✓ Service started'
    );

    // Slack 서비스 시작 알림
    if (this.slackClient) {
      await this.slackClient.sendServiceStarted({
        mode: env.REAL_MODE ? 'LIVE' : 'SIMULATION',
      });
    }
  }

  /**
   * Refresh target markets and save to Redis
   * The candle collector will automatically pick up changes on next cycle
   */
  private async refreshMarkets(): Promise<void> {
    try {
      logger.info('Refreshing target markets...');

      const markets = await this.marketSelector.getTargetMarkets(
        this.USER_ID,
        env.TOP_MARKETS_COUNT
      );

      if (markets.length === 0) {
        logger.warn('No markets to monitor');
        return;
      }

      // Save to Redis - candle collector will read on next batch cycle
      await this.redisClient.setTargetMarkets(this.USER_ID, markets);

      logger.info(
        { marketCount: markets.length, markets },
        'Target markets updated in Redis'
      );
    } catch (error) {
      logger.error({ error }, 'Failed to refresh markets');
    }
  }

  /**
   * Register graceful shutdown handlers
   */
  private registerShutdownHandlers(): void {
    const shutdown = async (signal: string) => {
      if (this.isShuttingDown) {
        return;
      }

      this.isShuttingDown = true;
      logger.info({ signal }, 'Shutting down gracefully...');

      // Slack 서비스 종료 알림
      if (this.slackClient) {
        await this.slackClient.sendServiceStopping(
          `graceful shutdown (${signal})`
        );
      }

      // Stop timers
      if (this.marketRefreshTimer) {
        clearInterval(this.marketRefreshTimer);
      }

      // Stop collectors
      this.candleCollector.stop();
      this.metadataCollector.stop();

      // Disconnect clients
      logger.info('Disconnecting clients...');
      await this.redisPublisher.disconnect();
      await this.redisClient.disconnect();
      if (env.POSTGRES_ENABLED) {
        await this.postgresClient.disconnect();
      }

      logger.info('Shutdown complete');
      process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
  }
}

/**
 * Main entry point
 */
async function main() {
  const service = new MarketDataService();

  try {
    await service.initialize();
    await service.start();
  } catch (error) {
    logger.error({ error }, 'Failed to start service');
    process.exit(1);
  }
}

// Run the service
main();
