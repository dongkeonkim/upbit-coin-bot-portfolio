/**
 * 포트폴리오 저장용 Redis 클라이언트
 */

import Redis from 'ioredis';
import { env } from '../config/env';
import { Portfolio } from '../types';
import { loggers } from './logger';

export class RedisClient {
  private client: Redis;

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
      loggers.error(error as Error, { context: 'RedisClient' });
    });
  }

  /**
   * Redis에 포트폴리오 저장
   * 키 형식: portfolio:{userId}
   */
  async savePortfolio(portfolio: Portfolio): Promise<void> {
    try {
      const key = `portfolio:${portfolio.userId}`;
      const portfolioJson = JSON.stringify(portfolio);

      await this.client.set(key, portfolioJson);

      loggers.redis('save_portfolio', key, {
        userId: portfolio.userId,
        dataSize: portfolioJson.length,
        value: portfolioJson  // Redis에 저장된 실제 값
      });
    } catch (error) {
      loggers.error(error as Error, { context: 'RedisClient.savePortfolio' });
      throw error;
    }
  }

  /**
   * Redis에서 포트폴리오 조회
   */
  async getPortfolio(userId: string): Promise<Portfolio | null> {
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
        userId
      });
      return null;
    }
  }

  /**
   * Redis에서 최신 캔들 조회
   */
  async getLatestCandle(key: string): Promise<{ close: number } | null> {
    try {
      const data = await this.client.lindex(key, 0);
      if (!data) {
        return null;
      }
      return JSON.parse(data);
    } catch (error) {
      return null;
    }
  }

  /**
   * Redis 연결 해제
   */
  async disconnect(): Promise<void> {
    await this.client.quit();
    loggers.redis('disconnect');
  }
}
