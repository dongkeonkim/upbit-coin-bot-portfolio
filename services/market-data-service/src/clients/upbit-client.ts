/**
 * Upbit REST API 클라이언트
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import { env } from '../config/env';
import { Market, Ticker, Candle } from '../types';
import { loggers } from '../utils/logger';

export class RateLimitError extends Error {
  readonly endpoint: string;
  readonly retryAfter?: number;

  constructor(endpoint: string, retryAfter?: number) {
    super(`Rate limit exceeded for ${endpoint}`);
    this.name = 'RateLimitError';
    this.endpoint = endpoint;
    this.retryAfter = retryAfter;
  }
}

export class UpbitClient {
  private readonly client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: env.UPBIT_API_URL,
      timeout: 10000,
      headers: {
        Accept: 'application/json',
      },
    });
  }

  /**
   * 전체 KRW 마켓 조회
   */
  async getMarkets(): Promise<Market[]> {
    try {
      const response = await this.client.get('/v1/market/all', {
        params: { isDetails: false },
      });

      // KRW 마켓만 필터링
      return response.data.filter((m: Market) => m.market.startsWith('KRW-'));
    } catch (error) {
      loggers.error(error as Error, { context: 'UpbitClient.getMarkets' });
      throw error;
    }
  }

  /**
   * 현재 시세 조회 (가격, 거래량)
   */
  async getTickers(markets: string[]): Promise<Ticker[]> {
    try {
      const response = await this.client.get('/v1/ticker', {
        params: { markets: markets.join(',') },
      });

      return response.data;
    } catch (error) {
      loggers.error(error as Error, { context: 'UpbitClient.getTickers' });
      throw error;
    }
  }

  /**
   * 1분봉 캔들 조회
   * @param market 마켓 코드 (예: KRW-BTC)
   * @param count 조회할 캔들 수 (최대 200)
   * @param to 이 시간 이전의 캔들 조회 (ISO 8601 형식, 예: 2026-01-01T08:30:00)
   * @throws RateLimitError 429 에러 발생 시
   */
  async getCandles1m(market: string, count: number = 1, to?: string): Promise<Candle[]> {
    try {
      const params: Record<string, string | number> = {
        market,
        count: Math.min(count, 200),
      };

      if (to) {
        params.to = to;
      }

      const response = await this.client.get('/v1/candles/minutes/1', {
        params,
      });

      return response.data;
    } catch (error) {
      // 429 Rate Limit 에러 감지
      if (error instanceof AxiosError && error.response?.status === 429) {
        const retryAfter = error.response.headers['retry-after']
          ? parseInt(error.response.headers['retry-after'], 10)
          : undefined;
        throw new RateLimitError(`/v1/candles/minutes/1 (${market})`, retryAfter);
      }

      loggers.error(error as Error, {
        context: 'UpbitClient.getCandles1m',
        market,
      });
      throw error;
    }
  }

  /**
   * 일봉 캔들 조회 (상장일 확인용)
   * @param market 마켓 코드 (예: KRW-BTC)
   * @param count 조회할 캔들 수 (최대 200)
   * @throws RateLimitError 429 에러 발생 시
   */
  async getCandlesDay(market: string, count: number = 200): Promise<Candle[]> {
    try {
      const response = await this.client.get('/v1/candles/days', {
        params: {
          market,
          count: Math.min(count, 200),
        },
      });

      return response.data;
    } catch (error) {
      // 429 Rate Limit 에러 감지
      if (error instanceof AxiosError && error.response?.status === 429) {
        const retryAfter = error.response.headers['retry-after']
          ? parseInt(error.response.headers['retry-after'], 10)
          : undefined;
        throw new RateLimitError(`/v1/candles/days (${market})`, retryAfter);
      }

      loggers.error(error as Error, {
        context: 'UpbitClient.getCandlesDay',
        market,
      });
      throw error;
    }
  }

  /**
   * 24시간 거래량 상위 N개 마켓 조회
   */
  async getTopMarketsByVolume(count: number = 10): Promise<string[]> {
    try {
      const markets = await this.getMarkets();
      const marketCodes = markets.map((m) => m.market);

      const tickers = await this.getTickers(marketCodes);

      // 24시간 거래대금(acc_trade_price_24h) 기준 내림차순 정렬
      const sorted = tickers
        .sort((a, b) => b.acc_trade_price_24h - a.acc_trade_price_24h)
        .slice(0, count);

      return sorted.map((t) => t.market);
    } catch (error) {
      loggers.error(error as Error, {
        context: 'UpbitClient.getTopMarketsByVolume',
      });
      throw error;
    }
  }
}
