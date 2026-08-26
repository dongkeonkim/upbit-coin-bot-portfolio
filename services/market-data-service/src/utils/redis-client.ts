/**
 * Redis Client
 */

import Redis from 'ioredis';
import { env } from '../config/env';
import { loggers } from './logger';

export class RedisClient {
  private readonly client: Redis;

  constructor() {
    this.client = new Redis({
      host: env.REDIS_HOST,
      port: env.REDIS_PORT,
      password: env.REDIS_PASSWORD || undefined,
      db: env.REDIS_DB,
      retryStrategy: (times: number) => {
        const delay = Math.min(times * 50, 2000);
        return delay;
      },
    });

    this.client.on('connect', () => {
      loggers.redis('connect');
    });

    this.client.on('error', (error) => {
      loggers.error(error, { context: 'RedisClient' });
    });
  }

  /**
   * Get portfolio holdings for a user
   * Redis key format: portfolio:{userId}
   */
  async getPortfolio(userId: string): Promise<Record<string, any> | null> {
    try {
      const key = `portfolio:${userId}`;
      const data = await this.client.get(key);

      if (!data) {
        return null;
      }

      return JSON.parse(data);
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisClient.getPortfolio',
        userId,
      });
      return null;
    }
  }

  /**
   * Get held currencies from portfolio
   */
  async getHeldCurrencies(userId: string): Promise<string[]> {
    try {
      const portfolio = await this.getPortfolio(userId);

      if (!portfolio || !portfolio.holdings) {
        return [];
      }

      // Extract currencies with balance > 0
      return Object.keys(portfolio.holdings).filter((currency) => {
        const holding = portfolio.holdings[currency];
        return holding && holding.balance > 0;
      });
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisClient.getHeldCurrencies',
        userId,
      });
      return [];
    }
  }

  /**
   * Get cached top markets by volume
   * Redis key: market:top10
   */
  async getTopMarkets(): Promise<string[] | null> {
    try {
      const key = 'market:top10';
      const data = await this.client.get(key);

      if (!data) {
        return null;
      }

      return JSON.parse(data);
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.getTopMarkets' });
      return null;
    }
  }

  /**
   * Cache top markets by volume
   * @param markets Array of market codes
   * @param ttl Time to live in seconds (default: 300s = 5 minutes)
   */
  async setTopMarkets(markets: string[], ttl: number = 300): Promise<void> {
    try {
      const key = 'market:top10';
      const marketsJson = JSON.stringify(markets);

      await this.client.setex(key, ttl, marketsJson);

      loggers.redis('cache_top_markets', key, {
        ttl,
        marketCount: markets.length,
        value: marketsJson  // Actual value stored in Redis
      });
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.setTopMarkets' });
    }
  }

  /**
   * Get target markets for candle collection
   * Redis key: market:target:{userId}
   */
  async getTargetMarkets(userId: string): Promise<string[] | null> {
    try {
      const key = `market:target:${userId}`;
      const data = await this.client.get(key);

      if (!data) {
        return null;
      }

      return JSON.parse(data);
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisClient.getTargetMarkets',
        userId,
      });
      return null;
    }
  }

  /**
   * Set target markets for candle collection
   * @param userId User ID
   * @param markets Array of market codes to monitor
   */
  async setTargetMarkets(userId: string, markets: string[]): Promise<void> {
    try {
      const key = `market:target:${userId}`;
      const marketsJson = JSON.stringify(markets);

      await this.client.set(key, marketsJson);

      loggers.redis('set_target_markets', key, {
        userId,
        marketCount: markets.length,
        value: marketsJson  // Actual value stored in Redis
      });
    } catch (error) {
      loggers.error(error as Error, {
        context: 'RedisClient.setTargetMarkets',
        userId,
      });
    }
  }

  /**
   * 마켓 메타데이터 일괄 저장
   * Redis key: market:metadata
   * @param metadata { market: listingDays } 형태
   * @param ttl TTL in seconds (default: 24시간)
   */
  async setMarketMetadata(
    metadata: Record<string, number>,
    ttl: number = 86400
  ): Promise<void> {
    try {
      const key = 'market:metadata';
      const metadataJson = JSON.stringify(metadata);

      await this.client.setex(key, ttl, metadataJson);

      loggers.redis('set_market_metadata', key, {
        ttl,
        marketCount: Object.keys(metadata).length,
      });
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.setMarketMetadata' });
    }
  }

  /**
   * 마켓 메타데이터 조회
   * @returns { market: listingDays } 형태 또는 null
   */
  async getMarketMetadata(): Promise<Record<string, number> | null> {
    try {
      const key = 'market:metadata';
      const data = await this.client.get(key);

      if (!data) {
        return null;
      }

      return JSON.parse(data);
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.getMarketMetadata' });
      return null;
    }
  }

  /**
   * 유효 마켓 목록 조회 (상장일 기준 필터링)
   * @param minListingDays 최소 상장일 수
   * @returns 조건 충족하는 마켓 목록
   */
  async getValidMarkets(minListingDays: number): Promise<string[]> {
    try {
      const metadata = await this.getMarketMetadata();

      if (!metadata) {
        return [];
      }

      return Object.entries(metadata)
        .filter(([_, days]) => days >= minListingDays)
        .map(([market]) => market);
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.getValidMarkets' });
      return [];
    }
  }

  /**
   * Disconnect from Redis
   */
  async disconnect(): Promise<void> {
    await this.client.quit();
    loggers.redis('disconnect');
  }

  /**
   * Get raw Redis client
   */
  getClient(): Redis {
    return this.client;
  }
}
