/**
 * Trade Executor Service
 * Executes trades based on signals
 */

import { UpbitTradeClient } from '../clients/upbit-trade-client';
import { RedisSubscriber } from '../clients/redis-subscriber';
import { SlackClient } from '../clients/slack-client';
import { env } from '../config/env';
import { TradeSignal, TradeCompletedMessage, OrderResponse } from '../types';
import logger, { loggers } from '../utils/logger';
import { RedisSellLock } from '../utils/redis-lock';

// 업비트 거래 수수료율 0.05%
const FEE_RATE = 0.0005;

// 체결 대기 타임아웃 (30초)
const ORDER_COMPLETION_TIMEOUT_MS = 30000;

export class TradeExecutor {
  private slackClient?: SlackClient;
  private sellLock: RedisSellLock;

  constructor(
    private upbitClient: UpbitTradeClient,
    private redisSubscriber: RedisSubscriber,
    sellLock: RedisSellLock,
    slackClient?: SlackClient
  ) {
    this.sellLock = sellLock;
    this.slackClient = slackClient;
  }

  /**
   * Execute trade based on signal
   */
  async executeSignal(signal: TradeSignal): Promise<void> {
    try {
      loggers.signal(signal.signalType as 'BUY' | 'SELL', signal.market, {
        reason: signal.reason,
        price: signal.price,
        amount: signal.amount,
        volume: signal.volume,
      });

      // SAFETY: Check if real trading is enabled
      if (!env.REAL_MODE) {
        logger.info(
          { event: 'simulation_trade', market: signal.market, signalType: signal.signalType },
          'Simulation trade (real trading disabled)'
        );

        // 모의투자: Upbit API 호출 없이 trade:completed 발행 (수수료 적용)
        let simulatedVolume: number;
        if (signal.signalType === 'BUY' && signal.amount) {
          // 매수: 수수료를 제외한 실제 매수 가능 수량
          // 실제 업비트: amount = (volume * price) + fee
          // volume = amount / price / (1 + FEE_RATE)
          simulatedVolume = signal.amount / signal.price / (1 + FEE_RATE);
        } else if (signal.signalType === 'SELL') {
          // 매도: Lock 적용 (중복 신호 방지)
          const lockAcquired = await this.sellLock.tryAcquire(signal.market);
          if (!lockAcquired) {
            logger.info(
              { event: 'simulation_sell_skipped_lock', market: signal.market },
              'Simulation: Skipping duplicate sell signal - lock held'
            );
            return;
          }

          try {
            simulatedVolume = signal.volume || 0;
            await this.publishTradeCompleted(signal, {
              price: String(signal.price),
              executed_volume: String(simulatedVolume),
              uuid: `sim-${Date.now()}`,
            } as OrderResponse);
          } finally {
            await this.sellLock.release(signal.market);
          }
          return;
        } else {
          simulatedVolume = signal.volume || 0;
        }
        await this.publishTradeCompleted(signal, {
          price: String(signal.price),
          executed_volume: String(simulatedVolume),
          uuid: `sim-${Date.now()}`,
        } as OrderResponse);
        return;
      }

      // Check if API keys are configured
      if (!this.upbitClient.isConfigured()) {
        logger.warn(
          { event: 'api_not_configured', market: signal.market },
          'Upbit API keys not configured, skipping trade'
        );
        return;
      }

      // Execute trade based on signal type
      let orderResponse: OrderResponse;

      if (signal.signalType === 'BUY') {
        if (!signal.amount || signal.amount <= 0) {
          logger.error(
            { event: 'invalid_order', side: 'BUY', market: signal.market, amount: signal.amount },
            'Invalid buy amount'
          );
          return;
        }

        // 업비트 최소 주문 금액 체크 (10,000원)
        const MIN_ORDER_AMOUNT = 10000;
        if (signal.amount < MIN_ORDER_AMOUNT) {
          loggers.orderBelowMinimum('BUY', signal.market, signal.amount, MIN_ORDER_AMOUNT);
          return;
        }

        try {
          const initialOrder = await this.upbitClient.marketBuy(signal.market, signal.amount);

          // 체결 완료까지 대기
          const { order: completedOrder, timedOut } = await this.upbitClient.waitForOrderComplete(
            initialOrder.uuid,
            ORDER_COMPLETION_TIMEOUT_MS
          );

          if (timedOut) {
            // 타임아웃 시 슬랙 알림
            if (this.slackClient) {
              await this.slackClient.sendMessage(
                `:warning: *주문 체결 지연* - ${signal.market} 매수\n` +
                `• UUID: ${initialOrder.uuid}\n` +
                `• 상태: ${completedOrder.state}\n` +
                `• 체결량: ${completedOrder.executed_volume}/${completedOrder.volume}\n` +
                `• 대기시간: ${ORDER_COMPLETION_TIMEOUT_MS / 1000}초 초과`
              );
            }
          }

          orderResponse = completedOrder;
        } catch (error: any) {
          // Slack 알림: 매수 실패
          if (this.slackClient) {
            await this.slackClient.sendUpbitApiError('BUY', signal.market, error.message || String(error), {
              amount: signal.amount,
              statusCode: error.response?.status,
            });
          }
          throw error;
        }
      } else if (signal.signalType === 'SELL') {
        // [Lock] 동시 매도 방지 - Redis Lock 획득
        const lockAcquired = await this.sellLock.tryAcquire(signal.market);
        if (!lockAcquired) {
          logger.info(
            { event: 'sell_skipped_lock', market: signal.market },
            'Skipping duplicate sell signal - lock held'
          );
          return;
        }

        try {
          // [잔고 확인] 실제 보유량 조회
          const currency = signal.market.replace('KRW-', '');
          let actualBalance: number;

          try {
            actualBalance = await this.upbitClient.getBalance(currency);
          } catch (error: any) {
            // Slack 알림: 잔고 조회 실패
            if (this.slackClient) {
              await this.slackClient.sendUpbitApiError('GET_BALANCE', signal.market, error.message || String(error), {
                statusCode: error.response?.status,
              });
            }
            throw error;
          }

          // 잔고 없으면 스킵 (이미 매도됨)
          if (actualBalance <= 0) {
            logger.warn(
              { event: 'sell_skipped_no_balance', market: signal.market, currency },
              'No balance to sell'
            );
            return;
          }

          // 전량 매도 (실제 잔고 전체)
          try {
            const initialOrder = await this.upbitClient.marketSell(signal.market, actualBalance);

            // 체결 완료까지 대기
            const { order: completedOrder, timedOut } = await this.upbitClient.waitForOrderComplete(
              initialOrder.uuid,
              ORDER_COMPLETION_TIMEOUT_MS
            );

            if (timedOut) {
              // 타임아웃 시 슬랙 알림
              if (this.slackClient) {
                await this.slackClient.sendMessage(
                  `:warning: *주문 체결 지연* - ${signal.market} 매도\n` +
                  `• UUID: ${initialOrder.uuid}\n` +
                  `• 상태: ${completedOrder.state}\n` +
                  `• 체결량: ${completedOrder.executed_volume}/${completedOrder.volume}\n` +
                  `• 대기시간: ${ORDER_COMPLETION_TIMEOUT_MS / 1000}초 초과`
                );
              }
            }

            orderResponse = completedOrder;
          } catch (error: any) {
            // Slack 알림: 매도 실패
            if (this.slackClient) {
              await this.slackClient.sendUpbitApiError('SELL', signal.market, error.message || String(error), {
                volume: actualBalance,
                statusCode: error.response?.status,
              });
            }
            throw error;
          }
        } finally {
          // 성공/실패 관계없이 항상 Lock 해제
          await this.sellLock.release(signal.market);
        }
      } else {
        logger.error(
          { event: 'unknown_signal_type', signalType: signal.signalType, market: signal.market },
          `Unknown signal type: ${signal.signalType}`
        );
        return;
      }

      // Publish trade completed event
      await this.publishTradeCompleted(signal, orderResponse);
    } catch (error) {
      loggers.error(error as Error, {
        context: 'TradeExecutor.executeSignal',
        market: signal.market,
        signalType: signal.signalType,
      });
    }
  }

  /**
   * 주문 응답에서 체결 가격 계산
   * 시장가 주문의 경우 price가 null이므로 trades에서 평균 가격 계산
   */
  private calculateExecutedPrice(orderResponse: OrderResponse, fallbackPrice: number): number {
    // 1. price 필드가 있으면 사용
    if (orderResponse.price) {
      const price = parseFloat(orderResponse.price);
      if (!isNaN(price) && price > 0) {
        return price;
      }
    }

    // 2. trades 배열에서 평균 가격 계산 (가중 평균)
    if (orderResponse.trades && orderResponse.trades.length > 0) {
      let totalFunds = 0;
      let totalVolume = 0;
      for (const trade of orderResponse.trades) {
        const funds = parseFloat(trade.funds);
        const volume = parseFloat(trade.volume);
        if (!isNaN(funds) && !isNaN(volume)) {
          totalFunds += funds;
          totalVolume += volume;
        }
      }
      if (totalVolume > 0) {
        return totalFunds / totalVolume;
      }
    }

    // 3. fallback: signal에서 전달받은 가격 사용
    return fallbackPrice;
  }

  /**
   * Publish trade completed event
   */
  private async publishTradeCompleted(
    signal: TradeSignal,
    orderResponse: OrderResponse
  ): Promise<void> {
    const executedPrice = this.calculateExecutedPrice(orderResponse, signal.price);
    const executedVolume = parseFloat(orderResponse.executed_volume);

    const message: TradeCompletedMessage = {
      userId: signal.userId,
      market: signal.market,
      side: signal.signalType,
      price: executedPrice,
      volume: executedVolume,
      timestamp: Date.now(),
    };

    await this.redisSubscriber.publishTradeCompleted(message);
    loggers.tradeComplete(message.market, message.side as 'BUY' | 'SELL', {
      price: message.price,
      volume: message.volume,
      userId: message.userId,
    });

    // SELL 시 실제 손익 계산 (실제 체결가 기준)
    let pnl: number | undefined;
    if (signal.signalType === 'SELL' && signal.avg_buy_price) {
      // 실제 손익 = (체결가 - 평균매수가) * 체결수량
      pnl = (executedPrice - signal.avg_buy_price) * executedVolume;
    }

    // Slack 주문 체결 알림
    if (this.slackClient) {
      await this.slackClient.sendOrderExecution(
        message.market,
        message.side as 'BUY' | 'SELL',
        message.price,
        message.volume,
        orderResponse.uuid,
        pnl
      );
    }
  }
}
