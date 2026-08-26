/**
 * Asset Service - 메인 애플리케이션
 * 포트폴리오 모니터링 및 주기적/거래 완료 시 Redis 동기화
 */

import { env } from './config/env';
import { UpbitAuthClient, RateLimitError } from './clients/upbit-auth-client';
import { RedisPubSubClient } from './clients/redis-pubsub-client';
import { RedisClient } from './utils/redis-client';
import { AssetSyncService } from './services/asset-sync-service';
import { SlackClient } from './clients/slack-client';
import { TradeCompletedMessage } from './types';

class AssetService {
    private upbitClient!: UpbitAuthClient;
    private redisPubSub!: RedisPubSubClient;
    private redisClient!: RedisClient;
    private assetSync!: AssetSyncService;
    private slackClient!: SlackClient;
    private syncTimer?: NodeJS.Timeout;
    private isShuttingDown = false;

    // 설정
    private readonly SYNC_INTERVAL = env.SYNC_INTERVAL_MINUTES * 60 * 1000;

    /**
     * 서비스 초기화
     */
    async initialize(): Promise<void> {
        console.log('='.repeat(60));
        console.log('Asset Service Starting');
        console.log('='.repeat(60));

        // 클라이언트 초기화
        this.upbitClient = new UpbitAuthClient();
        this.redisClient = new RedisClient();
        this.slackClient = new SlackClient(env.SLACK_WEBHOOK_URL);

        this.redisPubSub = new RedisPubSubClient();
        await this.redisPubSub.connect();

        // 서비스 초기화
        this.assetSync = new AssetSyncService(
            this.upbitClient,
            this.redisClient,
            env.USER_ID
        );

        // 종료 핸들러 등록
        this.registerShutdownHandlers();

        console.log('✓ Initialization complete\n');

        // Slack 알림
        await this.slackClient.sendServiceStarted({
            mode: env.REAL_MODE ? 'LIVE' : 'SIMULATION',
        });
    }

    /**
     * 서비스 시작
     */
    async start(): Promise<void> {
        console.log('💰 Asset Service Running...\n');

        // 초기 포트폴리오 동기화
        await this.syncPortfolio();

        // 주기적 동기화 시작 (N분마다)
        this.syncTimer = setInterval(() => {
            console.log(
                `[App] Periodic sync triggered (interval: ${env.SYNC_INTERVAL_MINUTES}m)`
            );
            this.syncPortfolio();
        }, this.SYNC_INTERVAL);

        console.log(
            `[App] Timer set - next sync in ${env.SYNC_INTERVAL_MINUTES} minute(s)`
        );

        // Redis Pub/Sub 소비자 시작 (거래 완료 시 동기화)
        await this.redisPubSub.start(async (message: TradeCompletedMessage) => {
            await this.handleTradeCompleted(message);
        });

        console.log(
            `✓ Periodic sync interval: ${env.SYNC_INTERVAL_MINUTES} minute(s)`
        );
        console.log('✓ Trade completion listener active (Redis Pub/Sub)');
        console.log('✓ Service started\n');
    }

    /**
     * Upbit에서 Redis로 포트폴리오 동기화
     */
    private async syncPortfolio(): Promise<void> {
        try {
            console.log('[App] Starting portfolio sync...');
            await this.assetSync.syncPortfolio();
            console.log('[App] Portfolio sync completed\n');
        } catch (error) {
            // 429 Rate Limit 에러 처리
            if (error instanceof RateLimitError) {
                console.error('[App] Rate limit hit:', error.endpoint);
                await this.slackClient.sendRateLimitAlert(error.endpoint, error.retryAfter);
                return;
            }
            // 기타 에러 (401 등) Slack 알림
            const errorMessage = error instanceof Error ? error.message : String(error);
            console.error('[App] Portfolio sync failed:', error);
            await this.slackClient.sendError('Portfolio Sync', errorMessage);
        }
    }

    /**
     * 거래 완료 이벤트 처리
     */
    private async handleTradeCompleted(
        message: TradeCompletedMessage
    ): Promise<void> {
        try {
            console.log('[App] Trade completed event received:');
            console.log(`  - User: ${message.userId}`);
            console.log(`  - Market: ${message.market}`);
            console.log(`  - Side: ${message.side}`);
            console.log(`  - Price: ${message.price}`);
            console.log(`  - Volume: ${message.volume}`);

            // 해당 유저의 거래면 포트폴리오 동기화
            if (message.userId === env.USER_ID) {
                console.log('[App] Syncing portfolio after trade...');
                // 거래 이벤트 전달 (모의투자 포트폴리오 업데이트용)
                await this.assetSync.syncPortfolio({
                    side: message.side as 'BUY' | 'SELL',
                    market: message.market,
                    price: message.price,
                    volume: message.volume,
                });
            }
        } catch (error) {
            console.error('[App] Failed to handle trade completed:', error);
        }
    }

    /**
     * 정상 종료 핸들러 등록
     */
    private registerShutdownHandlers(): void {
        const shutdown = async (signal: string) => {
            if (this.isShuttingDown) {
                return;
            }

            this.isShuttingDown = true;
            console.log(
                `\n\n[App] ${signal} received, shutting down gracefully...`
            );

            // Slack 알림
            await this.slackClient.sendServiceStopping(`graceful shutdown (${signal})`);

            // 타이머 정지
            if (this.syncTimer) {
                clearInterval(this.syncTimer);
            }

            // 클라이언트 연결 해제
            console.log('[App] Disconnecting clients...');
            await this.redisPubSub.disconnect();
            await this.redisClient.disconnect();

            console.log('[App] Shutdown complete\n');
            process.exit(0);
        };

        process.on('SIGINT', () => shutdown('SIGINT'));
        process.on('SIGTERM', () => shutdown('SIGTERM'));
    }
}

/**
 * 메인 진입점
 */
async function main() {
    const service = new AssetService();

    try {
        await service.initialize();
        await service.start();
    } catch (error) {
        console.error('Failed to start service:', error);
        process.exit(1);
    }
}

// 서비스 실행
main();
