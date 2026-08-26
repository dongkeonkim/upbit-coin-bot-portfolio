/**
 * Upbit 인증 API 클라이언트
 * 비공개 API 엔드포인트용 (계좌 정보, 주문)
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import crypto from 'crypto';
import { sign } from 'jsonwebtoken';
import { v4 as uuidv4 } from 'uuid';
import { env } from '../config/env';
import { Account } from '../types';

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

export class UpbitAuthClient {
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
      },
    });
  }

  /**
   * 인증용 JWT 토큰 생성
   */
  private generateToken(queryHash?: string): string {
    const payload = {
      access_key: this.accessKey,
      nonce: uuidv4(),
      timestamp: Date.now(),
    };

    if (queryHash) {
      Object.assign(payload, {
        query_hash: queryHash,
        query_hash_alg: 'SHA512',
      });
    }

    return sign(payload, this.secretKey);
  }

  /**
   * 요청 파라미터용 쿼리 해시 생성
   */
  private generateQueryHash(params: Record<string, any>): string {
    const query = new URLSearchParams(params).toString();
    return crypto.createHash('sha512').update(query).digest('hex');
  }

  /**
   * 전체 계좌 조회 (KRW + 보유 코인)
   * @throws RateLimitError 429 에러 발생 시
   */
  async getAccounts(): Promise<Account[]> {
    try {
      const token = this.generateToken();

      const response = await this.client.get('/v1/accounts', {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      return response.data;
    } catch (error) {
      // 429 Rate Limit 에러 감지
      if (error instanceof AxiosError && error.response?.status === 429) {
        const retryAfter = error.response.headers['retry-after']
          ? parseInt(error.response.headers['retry-after'], 10)
          : undefined;
        throw new RateLimitError('/v1/accounts', retryAfter);
      }

      console.error('[UpbitAuthClient] Failed to get accounts:',
        error instanceof AxiosError ? error.response?.data : (error as Error).message);
      throw error;
    }
  }

  /**
   * KRW 잔고 조회
   */
  async getKRWBalance(): Promise<number> {
    try {
      const accounts = await this.getAccounts();
      const krwAccount = accounts.find(acc => acc.currency === 'KRW');
      
      return krwAccount ? parseFloat(krwAccount.balance) : 0;
    } catch (error) {
      console.error('[UpbitAuthClient] Failed to get KRW balance:', error);
      return 0;
    }
  }

  /**
   * API 키 설정 여부 확인
   */
  isConfigured(): boolean {
    return !!(this.accessKey && this.secretKey);
  }
}
