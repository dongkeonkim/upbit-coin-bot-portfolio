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
    service: 'asset-service',
  },
  // 타임스탬프 형식
  timestamp: () => `,"time":"${new Date().toISOString()}"`,
});

// 공통 로그 패턴용 헬퍼 함수
export const loggers = {
  /**
   * 자산 관련 정보 로깅
   */
  asset: (currency: string, message: string, data?: object) => {
    logger.info({ currency, ...data }, message);
  },

  /**
   * 포트폴리오 업데이트 로깅
   */
  portfolio: (userId: string, message: string, data?: object) => {
    logger.info({ userId, ...data }, message);
  },

  /**
   * Kafka 이벤트 로깅
   */
  kafka: (event: string, data?: object) => {
    logger.info({ event, ...data }, `Kafka: ${event}`);
  },

  /**
   * Redis 작업 로깅
   */
  redis: (operation: string, key?: string, data?: object) => {
    logger.info({ operation, key, ...data }, `Redis: ${operation}`);
  },

  /**
   * Upbit API 호출 로깅
   */
  upbit: (operation: string, data?: object) => {
    logger.info({ operation, ...data }, `Upbit: ${operation}`);
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
