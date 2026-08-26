/**
 * PostgreSQL 캔들 저장 클라이언트
 */

import { Pool } from 'pg';
import { env } from '../config/env';
import { KafkaCandleMessage } from '../types';
import logger, { loggers } from '../utils/logger';

export class PostgresClient {
    private pool: Pool | null = null;
    private isConnected: boolean = false;

    constructor() {
        if (!env.POSTGRES_ENABLED) {
            logger.info('[PostgresClient] PostgreSQL storage disabled');
            return;
        }

        this.pool = new Pool({
            host: env.POSTGRES_HOST,
            port: env.POSTGRES_PORT,
            database: env.POSTGRES_DB,
            user: env.POSTGRES_USER,
            password: env.POSTGRES_PASSWORD,
            max: 10,
            idleTimeoutMillis: 30000,
            connectionTimeoutMillis: 5000,
        });

        this.pool.on('error', (err) => {
            loggers.error(err, { context: 'PostgresClient.pool' });
        });
    }

    async connect(): Promise<void> {
        if (!env.POSTGRES_ENABLED || !this.pool) {
            return;
        }

        try {
            const client = await this.pool.connect();
            client.release();
            this.isConnected = true;
            logger.info('[PostgresClient] Connected to PostgreSQL', {
                host: env.POSTGRES_HOST,
                port: env.POSTGRES_PORT,
                database: env.POSTGRES_DB,
            });
        } catch (error) {
            loggers.error(error as Error, { context: 'PostgresClient.connect' });
            throw error;
        }
    }

    async disconnect(): Promise<void> {
        if (this.pool) {
            await this.pool.end();
            this.isConnected = false;
            logger.info('[PostgresClient] Disconnected from PostgreSQL');
        }
    }

    /**
     * 캔들 데이터 저장 (UPSERT)
     */
    async saveCandle(candle: KafkaCandleMessage): Promise<void> {
        if (!env.POSTGRES_ENABLED || !this.pool || !this.isConnected) {
            return;
        }

        try {
            const timestamp = new Date(candle.timestamp);

            await this.pool.query(
                `INSERT INTO candles (market, interval, timestamp, open, high, low, close, volume, quote_asset_volume)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                 ON CONFLICT (market, interval, timestamp)
                 DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    quote_asset_volume = EXCLUDED.quote_asset_volume`,
                [
                    candle.market,
                    candle.interval,
                    timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.quote_volume,
                ]
            );
        } catch (error) {
            loggers.error(error as Error, {
                context: 'PostgresClient.saveCandle',
                market: candle.market,
            });
        }
    }

    /**
     * 여러 캔들 일괄 저장
     */
    async saveCandles(candles: KafkaCandleMessage[]): Promise<void> {
        if (!env.POSTGRES_ENABLED || !this.pool || !this.isConnected) {
            return;
        }

        if (candles.length === 0) {
            return;
        }

        const client = await this.pool.connect();
        try {
            await client.query('BEGIN');

            for (const candle of candles) {
                const timestamp = new Date(candle.timestamp);

                await client.query(
                    `INSERT INTO candles (market, interval, timestamp, open, high, low, close, volume, quote_asset_volume)
                     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                     ON CONFLICT (market, interval, timestamp)
                     DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        quote_asset_volume = EXCLUDED.quote_asset_volume`,
                    [
                        candle.market,
                        candle.interval,
                        timestamp,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                        candle.quote_volume,
                    ]
                );
            }

            await client.query('COMMIT');

            logger.info(
                {
                    event: 'candles_saved_to_postgres',
                    candleCount: candles.length,
                },
                `Saved ${candles.length} candles to PostgreSQL`
            );
        } catch (error) {
            await client.query('ROLLBACK');
            loggers.error(error as Error, {
                context: 'PostgresClient.saveCandles',
            });
        } finally {
            client.release();
        }
    }
}
