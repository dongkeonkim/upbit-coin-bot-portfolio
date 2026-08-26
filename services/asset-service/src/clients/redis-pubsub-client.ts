/**
 * 거래 완료 이벤트용 Redis Pub/Sub 클라이언트
 * Kafka consumer 대체
 */

import Redis from 'ioredis';
import { env } from '../config/env';
import { TradeCompletedMessage } from '../types';

export class RedisPubSubClient {
    private readonly pubsubClient: Redis;
    private isConnected: boolean = false;
    private messageHandler?: (message: TradeCompletedMessage) => Promise<void>;
    private readonly tradeCompletedChannel = 'trade:completed';

    constructor() {
        // pub/sub 전용 클라이언트
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
     * Redis 이벤트 핸들러 설정
     */
    private setupEventHandlers(): void {
        // 메시지 수신 시
        this.pubsubClient.on('message', (channel, message) => {
            this.handleMessage(channel, message);
        });

        // 연결 에러 시
        this.pubsubClient.on('error', (error) => {
            console.error('[RedisPubSub] Error:', error);
        });

        // 연결 성공 시
        this.pubsubClient.on('connect', () => {
            console.log('[RedisPubSub] Connected to Redis');
        });
    }

    /**
     * 연결 및 거래 완료 채널 구독
     */
    async connect(): Promise<void> {
        try {
            // 거래 완료 채널 구독
            await this.pubsubClient.subscribe(this.tradeCompletedChannel);
            this.isConnected = true;

            console.log(
                '[RedisPubSub] Subscribed to channel:',
                this.tradeCompletedChannel
            );
        } catch (error) {
            console.error('[RedisPubSub] Failed to connect:', error);
            throw error;
        }
    }

    /**
     * 거래 완료 이벤트 소비 시작
     */
    async start(
        handler: (message: TradeCompletedMessage) => Promise<void>
    ): Promise<void> {
        if (!this.isConnected) {
            throw new Error('Client not connected');
        }

        this.messageHandler = handler;
        console.log('[RedisPubSub] Started consuming messages');
    }

    /**
     * Redis pub/sub 메시지 처리
     */
    private async handleMessage(
        channel: string,
        message: string
    ): Promise<void> {
        try {
            if (channel !== this.tradeCompletedChannel) {
                return;
            }

            const tradeMessage: TradeCompletedMessage = JSON.parse(message);

            console.log(
                `[RedisPubSub] Received trade: ${tradeMessage.market} ${tradeMessage.side}`
            );

            if (this.messageHandler) {
                await this.messageHandler(tradeMessage);
            }
        } catch (error) {
            console.error('[RedisPubSub] Failed to process message:', error);
        }
    }

    /**
     * Redis 연결 해제
     */
    async disconnect(): Promise<void> {
        try {
            await this.pubsubClient.unsubscribe(this.tradeCompletedChannel);
            await this.pubsubClient.quit();
            this.isConnected = false;
            console.log('[RedisPubSub] Disconnected from Redis');
        } catch (error) {
            console.error('[RedisPubSub] Failed to disconnect:', error);
        }
    }
}
