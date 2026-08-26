/**
 * Type Definitions
 */

export interface Market {
  market: string;
  korean_name: string;
  english_name: string;
}

export interface Ticker {
  market: string;
  trade_price: number;
  acc_trade_price_24h: number;
  signed_change_rate: number;
  timestamp: number;
}

export interface Candle {
  market: string;
  candle_date_time_utc: string;
  candle_date_time_kst: string;
  opening_price: number;
  high_price: number;
  low_price: number;
  trade_price: number;
  timestamp: number;
  candle_acc_trade_price: number;
  candle_acc_trade_volume: number;
  unit: number;
}

export interface PortfolioHolding {
  currency: string;
  balance: number;
  locked: number;
  avg_buy_price: number;
  unit_currency: string;
}

export interface KafkaCandleMessage {
  market: string;
  timestamp: number;        // API 응답시간
  candle_time: number;      // 캔들 시작시간 (피처 계산, stale 체크용)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  quote_volume: number;
  interval: string;
}
