/**
 * Type Definitions
 */

export interface TradeSignal {
  userId: string;
  signalType: 'BUY' | 'SELL';
  market: string;
  price: number;
  reason: string;
  timestamp: number;
  amount?: number;  // For BUY: investment amount in KRW
  volume?: number;  // For SELL: volume to sell
  avg_buy_price?: number;  // For SELL: 평균 매수가 (손익 계산용)
}

export interface OrderRequest {
  market: string;
  side: 'bid' | 'ask';  // bid = buy, ask = sell
  volume?: string;      // For limit orders
  price?: string;       // For limit orders
  ord_type: 'price' | 'market' | 'limit';
}

export interface OrderTrade {
  market: string;
  uuid: string;
  price: string;
  volume: string;
  funds: string;
  side: string;
}

export interface OrderResponse {
  uuid: string;
  side: 'bid' | 'ask';
  ord_type: string;
  price: string | null;  // 시장가 주문의 경우 null
  state: string;
  market: string;
  created_at: string;
  volume: string;
  remaining_volume: string;
  reserved_fee: string;
  remaining_fee: string;
  paid_fee: string;
  locked: string;
  executed_volume: string;
  trades_count: number;
  trades?: OrderTrade[];  // 체결 내역 (주문 조회 시 포함)
}

export interface TradeCompletedMessage {
  userId: string;
  market: string;
  side: 'BUY' | 'SELL';
  price: number;
  volume: number;
  timestamp: number;
}

export interface Account {
  currency: string;
  balance: string;
  locked: string;
  avg_buy_price: string;
  avg_buy_price_modified: boolean;
  unit_currency: string;
}
