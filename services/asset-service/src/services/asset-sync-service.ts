/**
 * 자산 동기화 서비스
 * Upbit API에서 Redis로 포트폴리오 동기화
 * 모의투자 모드 지원
 */

import { UpbitAuthClient } from '../clients/upbit-auth-client';
import { RedisClient } from '../utils/redis-client';
import { Portfolio, Holding } from '../types';
import { env } from '../config/env';
import logger, { loggers } from '../utils/logger';

// 업비트 거래 수수료율 0.05%
const FEE_RATE = 0.0005;

export class AssetSyncService {
    private simulationMode: boolean;
    private initialBalance: number;

    constructor(
        private upbitClient: UpbitAuthClient,
        private redisClient: RedisClient,
        private userId: string
    ) {
        this.simulationMode = !env.REAL_MODE;
        this.initialBalance = env.INITIAL_BALANCE;
    }

    /**
     * Upbit에서 Redis로 포트폴리오 동기화 (또는 모의투자 포트폴리오 초기화)
     * @param lastTradeEvent - 고점 수익률 초기화용 거래 이벤트 (선택)
     */
    async syncPortfolio(lastTradeEvent?: {
        side: 'BUY' | 'SELL';
        market: string;
        price?: number;
        volume?: number;
    }): Promise<void> {
        try {
            logger.info(
                { userId: this.userId, simulationMode: this.simulationMode },
                'Starting portfolio sync'
            );

            // 모의투자 모드: 가상 잔고 사용
            if (this.simulationMode) {
                await this.syncSimulationPortfolio(lastTradeEvent);
                return;
            }

            // API 키 설정 확인
            if (!this.upbitClient.isConfigured()) {
                logger.warn(
                    { userId: this.userId },
                    'Upbit API keys not configured, switching to simulation mode'
                );
                await this.syncSimulationPortfolio();
                return;
            }

            // 고점 수익률 추적용 이전 포트폴리오 조회
            const previousPortfolio = await this.redisClient.getPortfolio(
                this.userId
            );

            // Upbit에서 계좌 정보 조회
            const accounts = await this.upbitClient.getAccounts();

            // Upbit API 응답 로깅
            loggers.upbit('get_accounts', {
                userId: this.userId,
                accountCount: accounts.length,
                accounts,
            });

            // 포트폴리오 구성
            const portfolio: Portfolio = {
                userId: this.userId,
                timestamp: Date.now(),
                krw_balance: 0,
                total_krw_value: 0,
                holdings: {},
            };

            // 각 계좌 처리
            for (const account of accounts) {
                if (account.currency === 'KRW') {
                    // KRW 잔고
                    portfolio.krw_balance = parseFloat(account.balance);
                    portfolio.total_krw_value += portfolio.krw_balance;
                } else {
                    // 코인 보유
                    const balance = parseFloat(account.balance);
                    const locked = parseFloat(account.locked);
                    const avgBuyPrice = parseFloat(account.avg_buy_price);

                    if (balance > 0) {
                        // 이전 고점 수익률 조회 또는 초기화
                        let peakProfitPercent: number | undefined;

                        const previousHolding =
                            previousPortfolio?.holdings[account.currency];

                        if (previousHolding) {
                            // 기존 고점 수익률 유지
                            peakProfitPercent =
                                previousHolding.peak_profit_percent;
                        } else if (
                            lastTradeEvent?.side === 'BUY' &&
                            lastTradeEvent.market === `KRW-${account.currency}`
                        ) {
                            // 신규 매수 시 고점 수익률 0%로 초기화
                            peakProfitPercent = 0.0;
                            logger.info(
                                {
                                    userId: this.userId,
                                    currency: account.currency,
                                },
                                'Initialized peak_profit_percent to 0% for new purchase'
                            );
                        } else {
                            // 거래 이벤트 없는 신규 보유 - 현재 수익률로 초기화
                            if (avgBuyPrice > 0) {
                                peakProfitPercent = 0.0; // 보수적: 지금부터 추적 시작
                            }
                        }

                        // Redis에서 현재가 조회
                        let currentPrice = avgBuyPrice; // 기본값: 평균매입가
                        try {
                            const candleKey = `candles:1m:KRW-${account.currency}`;
                            const latestCandle = await this.redisClient.getLatestCandle(candleKey);
                            if (latestCandle?.close) {
                                currentPrice = latestCandle.close;
                            }
                        } catch (e) {
                            // 캔들 데이터 없으면 평균매입가 사용
                        }

                        // 평가금액 합산
                        const krwValue = balance * currentPrice;
                        portfolio.total_krw_value += krwValue;

                        const holding: Holding = {
                            currency: account.currency,
                            balance,
                            locked,
                            avg_buy_price: avgBuyPrice,
                            current_price: currentPrice,
                            krw_value: krwValue,
                            peak_profit_percent: peakProfitPercent,
                        };

                        portfolio.holdings[account.currency] = holding;
                    }
                }
            }

            // 저장 전 포트폴리오 로깅
            loggers.portfolio(this.userId, 'Portfolio constructed', {
                timestamp: new Date(portfolio.timestamp).toISOString(),
                krw_balance: portfolio.krw_balance,
                total_krw_value: portfolio.total_krw_value,
                holdingCount: Object.keys(portfolio.holdings).length,
                holdings: portfolio.holdings,
            });

            // Redis에 저장
            await this.redisClient.savePortfolio(portfolio);

            logger.info(
                { userId: this.userId },
                'Portfolio synced successfully to Redis'
            );
        } catch (error) {
            loggers.error(error as Error, {
                context: 'AssetSyncService.syncPortfolio',
                userId: this.userId,
            });
            throw error;
        }
    }

    /**
     * 모의투자 포트폴리오 동기화
     * Redis에 가상 포트폴리오 생성 또는 유지
     */
    private async syncSimulationPortfolio(lastTradeEvent?: {
        side: 'BUY' | 'SELL';
        market: string;
        price?: number;
        volume?: number;
    }): Promise<void> {
        // 기존 포트폴리오 존재 여부 확인
        const existingPortfolio = await this.redisClient.getPortfolio(
            this.userId
        );

        if (!existingPortfolio) {
            // 초기 잔고로 신규 모의투자 포트폴리오 생성
            const portfolio: Portfolio = {
                userId: this.userId,
                timestamp: Date.now(),
                krw_balance: this.initialBalance,
                total_krw_value: this.initialBalance,
                holdings: {},
            };

            await this.redisClient.savePortfolio(portfolio);

            logger.info(
                {
                    userId: this.userId,
                    initialBalance: this.initialBalance,
                },
                'Simulation portfolio initialized'
            );

            loggers.portfolio(this.userId, 'Simulation portfolio created', {
                timestamp: new Date(portfolio.timestamp).toISOString(),
                krw_balance: portfolio.krw_balance,
                total_krw_value: portfolio.total_krw_value,
                holdingCount: 0,
                mode: 'SIMULATION',
            });
            return;
        }

        // 거래 이벤트가 없으면 상태 유지
        if (!lastTradeEvent) {
            logger.info(
                {
                    userId: this.userId,
                    krw_balance: existingPortfolio.krw_balance,
                    holdingCount: Object.keys(existingPortfolio.holdings).length,
                },
                'Simulation portfolio already exists, preserving state'
            );
            return;
        }

        // 거래 이벤트에 따라 포트폴리오 업데이트
        const currency = lastTradeEvent.market.replace('KRW-', '');
        const price = lastTradeEvent.price || 0;
        const volume = lastTradeEvent.volume || 0;
        const tradeAmount = price * volume;

        if (lastTradeEvent.side === 'BUY') {
            // 매수: holdings에 추가, KRW 잔고 감소
            const currentHolding = existingPortfolio.holdings[currency];

            if (currentHolding) {
                // 기존 보유 - 평단가 계산
                const totalVolume = currentHolding.balance + volume;
                const totalCost = (currentHolding.balance * currentHolding.avg_buy_price) + tradeAmount;
                currentHolding.balance = totalVolume;
                currentHolding.avg_buy_price = totalCost / totalVolume;
            } else {
                // 신규 보유
                existingPortfolio.holdings[currency] = {
                    currency,
                    balance: volume,
                    locked: 0,
                    avg_buy_price: price,
                    peak_profit_percent: 0,
                };
            }

            // 매수: 실제 지불 금액 = 코인 가치 + 수수료
            // tradeAmount는 받은 코인의 가치, 실제 지불은 수수료 포함
            const actualKrwSpent = tradeAmount * (1 + FEE_RATE);
            existingPortfolio.krw_balance -= actualKrwSpent;

            logger.info(
                {
                    userId: this.userId,
                    side: 'BUY',
                    market: lastTradeEvent.market,
                    price,
                    volume,
                    tradeAmount,
                    fee: tradeAmount * FEE_RATE,
                    actualKrwSpent,
                    newBalance: existingPortfolio.holdings[currency].balance,
                    newAvgPrice: existingPortfolio.holdings[currency].avg_buy_price,
                },
                'Simulation BUY executed (with fee)'
            );
        } else if (lastTradeEvent.side === 'SELL') {
            // 매도: holdings에서 제거, KRW 잔고 증가
            const currentHolding = existingPortfolio.holdings[currency];

            if (currentHolding) {
                currentHolding.balance -= volume;

                if (currentHolding.balance <= 0) {
                    // 전량 매도
                    delete existingPortfolio.holdings[currency];
                }

                // 매도: 실제 수령 금액 = 코인 가치 - 수수료
                const actualKrwReceived = tradeAmount * (1 - FEE_RATE);
                existingPortfolio.krw_balance += actualKrwReceived;

                logger.info(
                    {
                        userId: this.userId,
                        side: 'SELL',
                        market: lastTradeEvent.market,
                        price,
                        volume,
                        tradeAmount,
                        fee: tradeAmount * FEE_RATE,
                        actualKrwReceived,
                        remainingBalance: currentHolding.balance,
                        krwBalance: existingPortfolio.krw_balance,
                    },
                    'Simulation SELL executed (with fee)'
                );
            }
        }

        // 업데이트된 포트폴리오 저장
        existingPortfolio.timestamp = Date.now();
        await this.redisClient.savePortfolio(existingPortfolio);

        loggers.portfolio(this.userId, 'Simulation portfolio updated', {
            timestamp: new Date(existingPortfolio.timestamp).toISOString(),
            krw_balance: existingPortfolio.krw_balance,
            holdingCount: Object.keys(existingPortfolio.holdings).length,
            mode: 'SIMULATION',
            lastTrade: lastTradeEvent,
        });
    }

    /**
     * Redis에서 현재 포트폴리오 조회
     */
    async getCurrentPortfolio(): Promise<Portfolio | null> {
        return await this.redisClient.getPortfolio(this.userId);
    }
}
