/**
 * 환경 설정
 */

export const env = {
  // 서비스
  NODE_ENV: process.env.NODE_ENV || 'development',
  PORT: parseInt(process.env.PORT || '3002', 10),
  LOG_LEVEL: process.env.LOG_LEVEL || 'info',

  // Trading Mode (true = real trading, false = simulation)
  REAL_MODE: process.env.REAL_MODE === 'true',
  INITIAL_BALANCE: parseFloat(process.env.INITIAL_BALANCE || '10000000'),

  // Upbit API
  UPBIT_API_URL: process.env.UPBIT_API_URL || 'https://api.upbit.com',
  UPBIT_ACCESS_KEY: process.env.UPBIT_ACCESS_KEY || '',
  UPBIT_SECRET_KEY: process.env.UPBIT_SECRET_KEY || '',

  // Redis
  REDIS_HOST: process.env.REDIS_HOST || 'localhost',
  REDIS_PORT: parseInt(process.env.REDIS_PORT || '6379', 10),
  REDIS_PASSWORD: process.env.REDIS_PASSWORD || '',
  REDIS_DB: parseInt(process.env.REDIS_DB || '0', 10),

  // 자산 모니터링
  USER_ID: process.env.USER_ID || 'default_user',
  SYNC_INTERVAL_MINUTES: parseInt(process.env.SYNC_INTERVAL_MINUTES || '5', 10),

  // Slack
  SLACK_WEBHOOK_URL: process.env.SLACK_WEBHOOK_URL || '',
};
