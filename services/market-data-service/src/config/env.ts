/**
 * 환경 설정
 */

export const env = {
  // 서비스
  NODE_ENV: process.env.NODE_ENV || 'development',
  PORT: parseInt(process.env.PORT || '3001', 10),
  LOG_LEVEL: process.env.LOG_LEVEL || 'info',

  // Trading Mode (true = real trading, false = simulation)
  REAL_MODE: process.env.REAL_MODE === 'true',

  // Upbit API
  UPBIT_API_URL: process.env.UPBIT_API_URL || 'https://api.upbit.com',
  UPBIT_ACCESS_KEY: process.env.UPBIT_ACCESS_KEY || '',
  UPBIT_SECRET_KEY: process.env.UPBIT_SECRET_KEY || '',

  // Redis
  REDIS_HOST: process.env.REDIS_HOST || 'localhost',
  REDIS_PORT: parseInt(process.env.REDIS_PORT || '6379', 10),
  REDIS_PASSWORD: process.env.REDIS_PASSWORD || '',
  REDIS_DB: parseInt(process.env.REDIS_DB || '0', 10),

  // 마켓 선택
  TOP_MARKETS_COUNT: parseInt(process.env.TOP_MARKETS_COUNT || '10', 10),
  CANDLE_INTERVAL_MINUTES: parseInt(process.env.CANDLE_INTERVAL_MINUTES || '1', 10),
  ALLOWED_MARKETS: process.env.ALLOWED_MARKETS || '',  // 추가 수집할 마켓 (쉼표 구분)

  // PostgreSQL
  POSTGRES_HOST: process.env.POSTGRES_HOST || 'localhost',
  POSTGRES_PORT: parseInt(process.env.POSTGRES_PORT || '5432', 10),
  POSTGRES_DB: process.env.POSTGRES_DB || 'coinbot',
  POSTGRES_USER: process.env.POSTGRES_USER || 'coinbot',
  POSTGRES_PASSWORD: process.env.POSTGRES_PASSWORD || '',
  POSTGRES_ENABLED: process.env.POSTGRES_ENABLED === 'true',  // PostgreSQL 저장 활성화

  // Slack
  SLACK_WEBHOOK_URL: process.env.SLACK_WEBHOOK_URL || '',
};
