/**
 * Pino 기반 구조화 로거
 * 운영: JSON 로깅, 개발: 보기 좋은 출력
 */

import pino from 'pino';

const isDevelopment = process.env.NODE_ENV !== 'production';
const logLevel = process.env.LOG_LEVEL || 'info';

export const logger = pino({
  level: logLevel,
  // 개발: pino-pretty, 운영: JSON
  transport: isDevelopment
    ? {
        target: 'pino-pretty',
        options: {
          colorize: true,
          translateTime: 'HH:MM:ss',
          ignore: 'pid,hostname',
          messageFormat: '{msg}',
          hideObject: false,
        },
      }
    : undefined,
  // 모든 로그 기본 필드
  base: {
    service: 'market-data-service',
  },
  // 타임스탬프 형식
  timestamp: () => `,"time":"${new Date().toISOString()}"`,
});

// 공통 로그 패턴용 헬퍼 함수
export const loggers = {
  /**
   * 마켓 관련 정보 로깅
   */
  market: (market: string, message: string, data?: object) => {
    logger.info({ market, ...data }, message);
  },

  /**
   * 캔들 데이터 로깅
   */
  candle: (market: string, candle: any, message: string) => {
    logger.info(
      {
        market,
        timestamp: candle.candle_date_time_kst,
        price: candle.trade_price,
      },
      message
    );
  },

  /**
   * Redis 작업 로깅
   */
  redis: (operation: string, key?: string, data?: object) => {
    logger.info({ operation, key, ...data }, `Redis: ${operation}`);
  },

  /**
   * 컨텍스트 포함 에러 로깅
   */
  error: (error: Error, context?: object) => {
    logger.error(
      {
        error: {
          message: error.message,
          stack: error.stack,
          name: error.name,
        },
        ...context,
      },
      error.message
    );
  },
};

export default logger;
