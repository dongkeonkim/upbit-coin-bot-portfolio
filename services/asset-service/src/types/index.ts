/**
 * 타입 정의
 */

export interface Account {
  currency: string;
  balance: string;
  locked: string;
  avg_buy_price: string;
  avg_buy_price_modified: boolean;
  unit_currency: string;
}

export interface Portfolio {
  userId: string;
  timestamp: number;
  krw_balance: number;
  total_krw_value: number;
  holdings: Record<string, Holding>;
}

export interface Holding {
  currency: string;
  balance: number;
  locked: number;
  avg_buy_price: number;
  peak_profit_percent?: number;  // 트레일링 스탑용 고점 수익률
  current_price?: number;
  krw_value?: number;
}

export interface TradeCompletedMessage {
  userId: string;
  market: string;
  side: 'BUY' | 'SELL';
  price: number;
  volume: number;
  timestamp: number;
}
