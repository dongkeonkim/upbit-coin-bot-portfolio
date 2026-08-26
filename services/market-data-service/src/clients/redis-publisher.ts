/**
 * 캔들 데이터용 Redis 퍼블리셔
 * Kafka 대체하여 캔들 데이터 저장
 */

import Redis from 'ioredis';
import { env } from '../config/env';
import { KafkaCandleMessage } from '../types';
import logger, { loggers } from '../utils/logger';

export class RedisPublisher {
    private readonly client: Redis;
    private isConnected: boolean = false;
    private readonly maxCandlesPerMarket: number = 500; // 마켓당 최대 캔들 수 (ML 전략 480개 필요)
    private readonly ttlSeconds: number = 86400; // 24시간

    constructor() {
        this.client = new Redis({
            host: env.REDIS_HOST,
            port: env.REDIS_PORT,
            password: env.REDIS_PASSWORD || undefined,
            db: env.REDIS_DB,
            retryStrategy: (times) => {
                const delay = Math.min(times * 50, 2000);
                return delay;
            },
            maxRetriesPerRequest: 3,
        });

        this.setupEventHandlers();
    }

    /**
     * Redis 이벤트 핸들러 설정
     */
    private setupEventHandlers(): void {
        // 연결 성공 시
        this.client.on('connect', () => {
            this.isConnected = true;
            logger.info('[RedisPublisher] Connected to Redis', {
                host: env.REDIS_HOST,
                port: env.REDIS_PORT,
            });
        });

        // 에러 발생 시
        this.client.on('error', (error) => {
            loggers.error(error, { context: 'RedisPublisher' });
        });

        // 연결 종료 시
        this.client.on('close', () => {
            this.isConnected = false;
            logger.info('[RedisPublisher] Connection closed');
        });
    }

    /**
     * Redis 연결 (API 호환성 유지용)
     */
    async connect(): Promise<void> {
        // Redis 클라이언트는 자동 연결, API 호환성을 위한 메서드
        if (this.client.status === 'ready') {
            this.isConnected = true;
            logger.info('[RedisPublisher] Already connected');
        } else {
            // 연결 대기
            await new Promise<void>((resolve, reject) => {
                const timeout = setTimeout(() => {
                    reject(new Error('Redis connection timeout'));
                }, 5000);

                this.client.once('ready', () => {
                    clearTimeout(timeout);
                    this.isConnected = true;
                    resolve();
                });

                this.client.once('error', (err) => {
                    clearTimeout(timeout);
                    reject(err);
                });
            });
        }

        logger.info({ operation: 'connect' }, 'Redis: connect');
    }

    /**
     * Redis 연결 해제
     */
    async disconnect(): Promise<void> {
        try {
            await this.client.quit();
            this.isConnected = false;
            logger.info({ operation: 'disconnect' }, 'Redis: disconnect');
        } catch (error) {
            loggers.error(error as Error, {
                context: 'RedisPublisher.disconnect',
            });
        }
    }

    /**
     * Redis에 캔들 데이터 저장
     * LPUSH로 Redis List에 저장 (최신이 앞에)
     */
    async publishCandle(candle: KafkaCandleMessage): Promise<void> {
        if (!this.isConnected) {
            logger.warn('[RedisPublisher] Not connected, skipping publish');
            return;
        }

        try {
            const key = `candles:1m:${candle.market}`;
            const candleJson = JSON.stringify(candle);

            // 파이프라인으로 원자적 실행
            const pipeline = this.client.pipeline();

            // LPUSH: 최신 캔들을 앞에 추가
            pipeline.lpush(key, candleJson);

            // LTRIM: 최신 N개만 유지
            pipeline.ltrim(key, 0, this.maxCandlesPerMarket - 1);

            // EXPIRE: 24시간 TTL 설정
            pipeline.expire(key, this.ttlSeconds);

            await pipeline.exec();

            // Analysis 서비스에 알림 이벤트 발행
            await this.client.publish(
                'candles:new',
                JSON.stringify({
                    market: candle.market,
                    timestamp: candle.timestamp,
                })
            );

            loggers.candle(candle.market, candle, 'Published candle to Redis');
        } catch (error) {
            loggers.error(error as Error, {
                context: 'RedisPublisher.publishCandle',
                market: candle.market,
            });
            throw error;
        }
    }

    /**
     * 여러 캔들 일괄 저장
     */
    async publishCandles(candles: KafkaCandleMessage[]): Promise<void> {
        if (!this.isConnected) {
            logger.warn(
                '[RedisPublisher] Not connected, skipping batch publish'
            );
            return;
        }

        try {
            const pipeline = this.client.pipeline();

            for (const candle of candles) {
                const key = `candles:1m:${candle.market}`;
                const candleJson = JSON.stringify(candle);

                pipeline.lpush(key, candleJson);
                pipeline.ltrim(key, 0, this.maxCandlesPerMarket - 1);
                pipeline.expire(key, this.ttlSeconds);
            }

            await pipeline.exec();

            // Analysis 서비스에 알림 이벤트 발행
            for (const candle of candles) {
                await this.client.publish(
                    'candles:new',
                    JSON.stringify({
                        market: candle.market,
                        timestamp: candle.timestamp,
                    })
                );
            }

            // 저장된 캔들 로깅 (요약만 info, 상세는 debug)
            logger.info(
                {
                    event: 'candles_published',
                    candleCount: candles.length,
                    markets: candles.map((c) => c.market),
                },
                `Published ${candles.length} candles to Redis`
            );

            // 상세 캔들 데이터는 debug 레벨로
            logger.debug(
                {
                    event: 'candles_published_detail',
                    candles: candles.map((c) => ({
                        market: c.market,
                        timestamp: c.timestamp,
                        close: c.close,
                        volume: c.volume,
                    })),
                },
                'Candle details'
            );
        } catch (error) {
            loggers.error(error as Error, {
                context: 'RedisPublisher.publishCandles',
            });
            throw error;
        }
    }
}
