/**
 * Trade Service - Main Application
 * Executes trades based on signals from analysis service
 */

import { env } from './config/env';
import { UpbitTradeClient } from './clients/upbit-trade-client';
import { RedisSubscriber } from './clients/redis-subscriber';
import { SlackClient } from './clients/slack-client';
import { TradeExecutor } from './services/trade-executor';
import { TradeSignal } from './types';
import logger, { loggers } from './utils/logger';
import { RedisSellLock } from './utils/redis-lock';

class TradeService {
  private upbitClient!: UpbitTradeClient;
  private redisSubscriber!: RedisSubscriber;
  private slackClient?: SlackClient;
  private tradeExecutor!: TradeExecutor;
  private isShuttingDown = false;

  /**
   * Initialize the service
   */
  async initialize(): Promise<void> {
    logger.info('Trade Service Starting');

    // Initialize clients
    this.upbitClient = new UpbitTradeClient();
    this.redisSubscriber = new RedisSubscriber();
    await this.redisSubscriber.connect();

    // Initialize Slack client (optional)
    if (env.SLACK_WEBHOOK_URL) {
      this.slackClient = new SlackClient(env.SLACK_WEBHOOK_URL, 'trade-service');
    }

    // Initialize Redis Sell Lock (for preventing duplicate sell signals)
    const sellLock = new RedisSellLock(this.redisSubscriber.getClient());

    // Initialize trade executor with Sell Lock and Slack client
    this.tradeExecutor = new TradeExecutor(
      this.upbitClient,
      this.redisSubscriber,
      sellLock,
      this.slackClient
    );

    // Register shutdown handlers
    this.registerShutdownHandlers();

    logger.info('Initialization complete');
  }

  /**
   * Start the service
   */
  async start(): Promise<void> {
    logger.info('Trade Service Running');

    // Display configuration and warn if trading is disabled (shown once at startup)
    logger.info(
      {
        event: 'config_loaded',
        realTrading: env.REAL_MODE,
        apiConfigured: this.upbitClient.isConfigured(),
      },
      'Configuration loaded'
    );

    if (!env.REAL_MODE) {
      logger.warn(
        { event: 'trading_disabled' },
        'Real trading is DISABLED - signals will be logged but not executed'
      );
    }

    // Start consuming trade signals from Redis
    await this.redisSubscriber.start(async (signal: TradeSignal) => {
      await this.handleTradeSignal(signal);
    });

    logger.info('Service started');

    // Slack 서비스 시작 알림
    if (this.slackClient) {
      await this.slackClient.sendServiceStarted({
        mode: env.REAL_MODE ? 'LIVE' : 'SIMULATION',
      });
    }
  }

  /**
   * Handle trade signal from Redis
   */
  private async handleTradeSignal(signal: TradeSignal): Promise<void> {
    try {
      // Signal details are logged by TradeExecutor.executeSignal
      await this.tradeExecutor.executeSignal(signal);
    } catch (error) {
      loggers.error(error as Error, {
        context: 'TradeService.handleTradeSignal',
        market: signal.market,
        signalType: signal.signalType,
      });
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
      logger.info({ event: 'shutdown_started', signal }, `${signal} received, shutting down gracefully`);

      // Slack 서비스 종료 알림
      if (this.slackClient) {
        await this.slackClient.sendServiceStopping(`graceful shutdown (${signal})`);
      }

      // Disconnect clients
      logger.info('Disconnecting clients');
      await this.redisSubscriber.disconnect();

      logger.info({ event: 'shutdown_complete' }, 'Shutdown complete');
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
  const service = new TradeService();

  try {
    await service.initialize();
    await service.start();
  } catch (error) {
    loggers.error(error as Error, { context: 'main' });
    process.exit(1);
  }
}

// Run the service
main();
