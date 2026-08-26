/**
 * 마켓 선택 서비스
 * 거래량 상위 N개 마켓 + ALLOWED_MARKETS + 사용자 보유 코인 선택
 */

import { UpbitClient } from '../clients/upbit-client';
import { RedisClient } from '../utils/redis-client';
import { env } from '../config/env';
import logger, { loggers } from '../utils/logger';

export class MarketSelector {
    constructor(
        private readonly upbitClient: UpbitClient,
        private readonly redisClient: RedisClient
    ) {}

    /**
     * 모니터링할 마켓 조회: 상위 N개 + 사용자 보유 코인
     * 신규 코인 필터링은 trading-engine에서 수행
     * @param userId 사용자 ID
     * @param topCount 포함할 상위 마켓 수
     */
    async getTargetMarkets(
        userId: string,
        topCount: number
    ): Promise<string[]> {
        try {
            // 1. 거래량 상위 N개 마켓 조회 (Redis 캐시 활용)
            let topMarkets = await this.redisClient.getTopMarkets();

            if (!topMarkets) {
                // 캐시 미스: Upbit API에서 조회 후 캐시
                loggers.market(
                    'ALL',
                    'Cache miss: fetching top markets from Upbit API'
                );
                topMarkets = await this.upbitClient.getTopMarketsByVolume(
                    topCount
                );
                await this.redisClient.setTopMarkets(topMarkets, 60); // 1분 TTL
            } else {
                loggers.market('ALL', 'Cache hit: using cached top markets');
            }

            // 상위 N개 선택
            const selectedTopMarkets = topMarkets.slice(0, topCount);

            // 2. ALLOWED_MARKETS 추가 (환경변수)
            const allowedMarkets = env.ALLOWED_MARKETS
                ? env.ALLOWED_MARKETS.split(',').map((m) => m.trim().toUpperCase())
                : [];
            const allowedOnly = allowedMarkets.filter(
                (m) => !selectedTopMarkets.includes(m)
            );
            if (allowedOnly.length > 0) {
                logger.info(
                    {
                        event: 'allowed_markets_added',
                        top5_count: selectedTopMarkets.length,
                        allowed_added: allowedOnly,
                    },
                    `Added ${allowedOnly.length} allowed markets`
                );
            }

            // 3. 사용자 보유 코인 조회
            const heldCurrencies = await this.redisClient.getHeldCurrencies(
                userId
            );
            const heldMarkets = heldCurrencies.map(
                (currency) => `KRW-${currency}`
            );

            // 4. 병합 및 중복 제거 (TOP N + ALLOWED + 보유)
            const allMarkets = [
                ...new Set([...selectedTopMarkets, ...allowedOnly, ...heldMarkets]),
            ];

            // info 레벨: 개수만 로깅
            loggers.market('ALL', `Selected ${allMarkets.length} markets`, {
                event: 'markets_selected',
                topCount,
                topMarketsCount: selectedTopMarkets.length,
                heldMarketsCount: heldMarkets.length,
            });

            // debug 레벨: 상세 마켓 목록
            logger.debug(
                {
                    event: 'markets_selected_detail',
                    markets: allMarkets,
                },
                'Selected markets detail'
            );

            return allMarkets;
        } catch (error) {
            loggers.error(error as Error, {
                context: 'MarketSelector.getTargetMarkets',
            });
            throw error;
        }
    }
}
