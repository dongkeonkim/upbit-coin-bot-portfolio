/**
 * Redis-based Sell Lock
 * Prevents duplicate sell signals for the same market
 */

import Redis from 'ioredis';
import logger from './logger';

const LOCK_PREFIX = 'sell_lock:';
const LOCK_TTL_SECONDS = 30; // 30 seconds TTL for auto-expiration

export class RedisSellLock {
  constructor(private redis: Redis) {}

  /**
   * Try to acquire lock for a market
   * @param market Market code (e.g., KRW-BTC)
   * @returns true if lock acquired, false if already locked
   */
  async tryAcquire(market: string): Promise<boolean> {
    const key = `${LOCK_PREFIX}${market}`;
    // SET key value EX seconds NX - only sets if key doesn't exist
    const result = await this.redis.set(key, Date.now().toString(), 'EX', LOCK_TTL_SECONDS, 'NX');
    const acquired = result === 'OK';

    if (acquired) {
      logger.debug({ event: 'sell_lock_acquired', market }, `Lock acquired for ${market}`);
    } else {
      logger.info(
        { event: 'sell_lock_already_held', market },
        `Lock already held for ${market}, skipping`
      );
    }

    return acquired;
  }

  /**
   * Release lock for a market
   * @param market Market code
   */
  async release(market: string): Promise<void> {
    const key = `${LOCK_PREFIX}${market}`;
    await this.redis.del(key);
    logger.debug({ event: 'sell_lock_released', market }, `Lock released for ${market}`);
  }

  /**
   * Check if market is currently locked
   * @param market Market code
   */
  async isLocked(market: string): Promise<boolean> {
    const key = `${LOCK_PREFIX}${market}`;
    return (await this.redis.exists(key)) === 1;
  }
}
