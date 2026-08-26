/**
 * Upbit Trade API Client
 * For executing buy/sell orders
 */

import axios, { AxiosInstance } from 'axios';
import crypto from 'crypto';
import { sign } from 'jsonwebtoken';
import { v4 as uuidv4 } from 'uuid';
import { env } from '../config/env';
import { OrderRequest, OrderResponse, Account } from '../types';
import { loggers } from '../utils/logger';

export class UpbitTradeClient {
  private client: AxiosInstance;
  private accessKey: string;
  private secretKey: string;

  constructor() {
    this.accessKey = env.UPBIT_ACCESS_KEY;
    this.secretKey = env.UPBIT_SECRET_KEY;

    this.client = axios.create({
      baseURL: env.UPBIT_API_URL,
      timeout: 10000,
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    });
  }

  /**
   * Generate JWT token for authentication
   */
  private generateToken(queryHash?: string): string {
    const payload: any = {
      access_key: this.accessKey,
      nonce: uuidv4(),
      timestamp: Date.now(),
    };

    if (queryHash) {
      payload.query_hash = queryHash;
      payload.query_hash_alg = 'SHA512';
    }

    return sign(payload, this.secretKey);
  }

  /**
   * Generate query hash for request parameters
   */
  private generateQueryHash(params: Record<string, any>): string {
    const query = new URLSearchParams(params).toString();
    return crypto.createHash('sha512').update(query).digest('hex');
  }

  /**
   * Place market buy order (buy with KRW amount)
   * @param market Market code (e.g., KRW-BTC)
   * @param price KRW amount to invest
   */
  async marketBuy(market: string, price: number): Promise<OrderResponse> {
    try {
      const orderRequest: OrderRequest = {
        market,
        side: 'bid',
        ord_type: 'price',
        price: price.toString(),
      };

      // Log request
      loggers.upbit('market_buy_request', {
        market: orderRequest.market,
        side: orderRequest.side,
        ord_type: orderRequest.ord_type,
        price
      });

      const queryHash = this.generateQueryHash(orderRequest);
      const token = this.generateToken(queryHash);

      const response = await this.client.post('/v1/orders', orderRequest, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      // Log response
      loggers.upbit('market_buy_response', {
        market,
        price,
        order: response.data
      });

      return response.data;
    } catch (error: any) {
      loggers.error(error, {
        context: 'UpbitTradeClient.marketBuy',
        market,
        price,
        response: error.response?.data
      });
      throw error;
    }
  }

  /**
   * Place market sell order (sell all volume)
   * @param market Market code (e.g., KRW-BTC)
   * @param volume Volume to sell
   */
  async marketSell(market: string, volume: number): Promise<OrderResponse> {
    try {
      const orderRequest: OrderRequest = {
        market,
        side: 'ask',
        ord_type: 'market',
        volume: volume.toString(),
      };

      // Log request
      loggers.upbit('market_sell_request', {
        market: orderRequest.market,
        side: orderRequest.side,
        ord_type: orderRequest.ord_type,
        volume
      });

      const queryHash = this.generateQueryHash(orderRequest);
      const token = this.generateToken(queryHash);

      const response = await this.client.post('/v1/orders', orderRequest, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      // Log response
      loggers.upbit('market_sell_response', {
        market,
        volume,
        order: response.data
      });

      return response.data;
    } catch (error: any) {
      loggers.error(error, {
        context: 'UpbitTradeClient.marketSell',
        market,
        volume,
        response: error.response?.data
      });
      throw error;
    }
  }

  /**
   * Get balance for a specific currency
   * @param currency Currency code (e.g., BTC, ETH)
   * @returns Balance amount (0 if not found)
   */
  async getBalance(currency: string): Promise<number> {
    try {
      const token = this.generateToken();

      const response = await this.client.get('/v1/accounts', {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      const accounts: Account[] = response.data;
      const account = accounts.find((acc) => acc.currency === currency);

      const balance = account ? parseFloat(account.balance) : 0;

      loggers.upbit('get_balance', {
        currency,
        balance,
        found: !!account,
      });

      return balance;
    } catch (error: any) {
      loggers.error(error, {
        context: 'UpbitTradeClient.getBalance',
        currency,
        response: error.response?.data,
      });
      throw error;
    }
  }

  /**
   * Get order status by UUID
   * @param uuid Order UUID
   */
  async getOrder(uuid: string): Promise<OrderResponse> {
    try {
      const params = { uuid };
      const queryHash = this.generateQueryHash(params);
      const token = this.generateToken(queryHash);

      const response = await this.client.get('/v1/order', {
        params,
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      return response.data;
    } catch (error: any) {
      loggers.error(error, {
        context: 'UpbitTradeClient.getOrder',
        uuid,
        response: error.response?.data,
      });
      throw error;
    }
  }

  /**
   * Wait for order to complete (polling)
   * @param uuid Order UUID
   * @param timeoutMs Timeout in milliseconds (default: 30000)
   * @param pollIntervalMs Poll interval in milliseconds (default: 500)
   * @returns Completed order response, or null if timeout
   */
  async waitForOrderComplete(
    uuid: string,
    timeoutMs: number = 30000,
    pollIntervalMs: number = 500
  ): Promise<{ order: OrderResponse; timedOut: boolean }> {
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
      const order = await this.getOrder(uuid);

      // done = 체결 완료, cancel = 취소
      if (order.state === 'done' || order.state === 'cancel') {
        loggers.upbit('order_completed', {
          uuid,
          state: order.state,
          executed_volume: order.executed_volume,
          elapsed_ms: Date.now() - startTime,
        });
        return { order, timedOut: false };
      }

      // 대기 후 재시도
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }

    // 타임아웃 - 마지막 상태 반환
    const finalOrder = await this.getOrder(uuid);
    loggers.upbit('order_timeout', {
      uuid,
      state: finalOrder.state,
      elapsed_ms: Date.now() - startTime,
    });
    return { order: finalOrder, timedOut: true };
  }

  /**
   * Check if API keys are configured
   */
  isConfigured(): boolean {
    return !!(this.accessKey && this.secretKey);
  }
}
