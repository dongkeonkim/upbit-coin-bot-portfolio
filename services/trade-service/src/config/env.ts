/**
 * Environment Configuration
 */

export const env = {
  // Service
  NODE_ENV: process.env.NODE_ENV || 'development',
  PORT: parseInt(process.env.PORT || '3003', 10),
  LOG_LEVEL: process.env.LOG_LEVEL || 'info',

  // Upbit API
  UPBIT_API_URL: process.env.UPBIT_API_URL || 'https://api.upbit.com',
  UPBIT_ACCESS_KEY: process.env.UPBIT_ACCESS_KEY || '',
  UPBIT_SECRET_KEY: process.env.UPBIT_SECRET_KEY || '',

  // Redis
  REDIS_HOST: process.env.REDIS_HOST || 'localhost',
  REDIS_PORT: parseInt(process.env.REDIS_PORT || '6379', 10),
  REDIS_PASSWORD: process.env.REDIS_PASSWORD || '',
  REDIS_DB: parseInt(process.env.REDIS_DB || '0', 10),

  // Trading Mode (true = real trading, false = simulation)
  REAL_MODE: process.env.REAL_MODE === 'true',

  // Slack
  SLACK_WEBHOOK_URL: process.env.SLACK_WEBHOOK_URL || '',
};
