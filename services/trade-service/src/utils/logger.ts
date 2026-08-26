/**
 * Structured Logger using Pino
 * Provides JSON logging for production and pretty printing for development
 */

import pino from 'pino';

const isDevelopment = process.env.NODE_ENV !== 'production';
const logLevel = process.env.LOG_LEVEL || 'info';

export const logger = pino({
  level: logLevel,
  // Use pino-pretty for development, JSON for production
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
  // Base fields for all logs
  base: {
    service: 'trade-service',
  },
  // Timestamp format
  timestamp: () => `,"time":"${new Date().toISOString()}"`,
});

// Export helper functions for common log patterns
export const loggers = {
  /**
   * Log trade-related information
   */
  trade: (market: string, message: string, data?: object) => {
    logger.info({ market, ...data }, message);
  },

  /**
   * Log order execution
   */
  order: (orderId: string, message: string, data?: object) => {
    logger.info({ orderId, ...data }, message);
  },

  /**
   * Log Kafka events (deprecated, kept for compatibility)
   */
  kafka: (event: string, data?: object) => {
    logger.info({ event, ...data }, `Kafka: ${event}`);
  },

  /**
   * Log Redis operations
   */
  redis: (operation: string, data?: object) => {
    logger.info({ operation, ...data }, `Redis: ${operation}`);
  },

  /**
   * Log Upbit API calls
   */
  upbit: (operation: string, data?: object) => {
    logger.info({ operation, ...data }, `Upbit: ${operation}`);
  },

  /**
   * Log errors with context
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

  /**
   * Log trading signal received
   */
  signal: (
    signalType: 'BUY' | 'SELL',
    market: string,
    data?: object
  ) => {
    logger.info(
      { event: 'signal_received', signalType, market, ...data },
      `Signal: ${signalType} ${market}`
    );
  },

  /**
   * Log order execution result
   */
  orderExecution: (
    market: string,
    side: 'BUY' | 'SELL',
    success: boolean,
    data?: object
  ) => {
    const level = success ? 'info' : 'error';
    logger[level](
      { event: 'order_execution', market, side, success, ...data },
      `Order ${success ? 'executed' : 'failed'}: ${side} ${market}`
    );
  },

  /**
   * Log trade completion
   */
  tradeComplete: (
    market: string,
    side: 'BUY' | 'SELL',
    data?: object
  ) => {
    logger.info(
      { event: 'trade_completed', market, side, ...data },
      `Trade completed: ${side} ${market}`
    );
  },

  /**
   * Log order validation warning
   */
  orderBelowMinimum: (
    side: 'BUY' | 'SELL',
    market: string,
    amount: number,
    minimum: number,
    data?: object
  ) => {
    logger.warn(
      { event: 'order_below_minimum', side, market, amount, minimum, ...data },
      `Order below minimum: ${side} ${market} ${amount.toLocaleString()}원 < ${minimum.toLocaleString()}원`
    );
  },
};

export default logger;
