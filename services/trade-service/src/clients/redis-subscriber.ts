/**
 * Redis Subscriber for Trade Signals
 * Replaces Kafka for signal consumption
 */

import Redis from 'ioredis';
import { env } from '../config/env';
import { TradeSignal } from '../types';
import { loggers } from '../utils/logger';

export class RedisSubscriber {
  private readonly client: Redis;
  private readonly pubsubClient: Redis;
  private isConnected: boolean = false;
  private signalHandler?: (signal: TradeSignal) => Promise<void>;
  private readonly signalsChannel = 'trade:signals';

  constructor() {
    // Main client for general operations
    this.client = new Redis({
      host: env.REDIS_HOST,
      port: env.REDIS_PORT,
      password: env.REDIS_PASSWORD || undefined,
      db: env.REDIS_DB,
      retryStrategy: (times) => {
        const delay = Math.min(times * 50, 2000);
        return delay;
      },
    });

    // Dedicated client for pub/sub (required by ioredis)
    this.pubsubClient = new Redis({
      host: env.REDIS_HOST,
      port: env.REDIS_PORT,
      password: env.REDIS_PASSWORD || undefined,
      db: env.REDIS_DB,
      retryStrategy: (times) => {
        const delay = Math.min(times * 50, 2000);
        return delay;
      },
    });

    this.setupEventHandlers();
  }

  /**
   * Setup Redis event handlers
   */
  private setupEventHandlers(): void {
    this.pubsubClient.on('message', (channel, message) => {
      this.handleMessage(channel, message);
    });

    this.pubsubClient.on('error', (error) => {
      loggers.error(error, { context: 'RedisSubscriber.pubsubClient' });
    });

    this.client.on('error', (error) => {
      loggers.error(error, { context: 'RedisSubscriber.client' });
    });
  }

  /**
   * Connect and subscribe to trade signals channel
   */
  async connect(): Promise<void> {
    try {
      // Subscribe to signals channel
      await this.pubsubClient.subscribe(this.signalsChannel);
      this.isConnected = true;

      loggers.redis('subscribe', {
        channel: this.signalsChannel,
      });
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisSubscriber.connect' });
      throw error;
    }
  }

  /**
   * Start consuming trade signals
   */
  async start(handler: (signal: TradeSignal) => Promise<void>): Promise<void> {
    if (!this.isConnected) {
      throw new Error('Client not connected');
    }

    this.signalHandler = handler;

    loggers.redis('start_consuming', {
      channel: this.signalsChannel,
    });
  }

  /**
   * Handle incoming Redis pub/sub message
   */
  private async handleMessage(channel: string, message: string): Promise<void> {
    try {
      if (channel !== this.signalsChannel) {
        return;
      }

      const signal: TradeSignal = JSON.parse(message);

      loggers.redis('receive_signal', {
        signalType: signal.signalType,
        market: signal.market,
        signal,
      });

      if (this.signalHandler) {
        await this.signalHandler(signal);
      }
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisSubscriber.handleMessage',
        channel,
      });
    }
  }

  /**
   * Publish trade completed event (optional - for future use)
   */
  async publishTradeCompleted(message: any): Promise<void> {
    if (!this.isConnected) {
      loggers.redis('not_connected_skip_publish');
      return;
    }

    try {
      const channel = 'trade:completed';
      await this.client.publish(channel, JSON.stringify(message));

      loggers.redis('publish_trade_completed', {
        channel,
        message,
        timestamp: message.timestamp,
      });
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisSubscriber.publishTradeCompleted',
        message,
      });
      throw error;
    }
  }

  /**
   * Get the Redis client instance for external use
   */
  getClient(): Redis {
    return this.client;
  }

  /**
   * Disconnect from Redis
   */
  async disconnect(): Promise<void> {
    try {
      await this.pubsubClient.unsubscribe(this.signalsChannel);
      await this.pubsubClient.quit();
      await this.client.quit();
      this.isConnected = false;
      loggers.redis('disconnect');
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisSubscriber.disconnect',
      });
    }
  }
}
