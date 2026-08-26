"""백테스트 로그 작성기 (optimizer-go/reporter/logger.go와 동일 패턴)"""

import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Params


def format_currency(amount: float) -> str:
    """금액을 천 단위 구분 기호와 함께 포맷팅"""
    if amount < 0:
        return f"₩-{abs(int(amount)):,}"
    return f"₩{int(amount):,}"


def generate_log_filename(period_name: str) -> str:
    """날짜+시간+기간으로 로그 파일명 생성

    예: 20251231_143052_P2.log
    """
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    period_upper = period_name.upper() if period_name.startswith("p") else period_name
    return f"{timestamp}_{period_upper}.log"


class Logger:
    """백테스트 로그 작성기"""

    def __init__(self, log_file_path: Optional[str] = None):
        self.file = None
        self.buffer: List[str] = []
        self.last_date = ""
        self.trades_by_day: Dict[str, List[Dict]] = defaultdict(list)
        self.balance_by_day: Dict[str, float] = {}
        self.initial_balance = 0.0

        if log_file_path:
            log_dir = os.path.dirname(log_file_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            self.file = open(log_file_path, "w", encoding="utf-8")

    def write(self, message: str = ""):
        """로그 메시지 버퍼에 추가 및 즉시 파일에 기록"""
        self.buffer.append(message)
        # 실시간 기록
        if self.file:
            self.file.write(message + "\n")
            self.file.flush()

    def flush(self):
        """버퍼의 내용을 파일에 쓰기"""
        if self.file:
            for line in self.buffer:
                self.file.write(line + "\n")
            self.file.flush()

    def close(self):
        """로그 파일 닫기"""
        if self.file:
            self.file.close()

    def print_to_console(self):
        """버퍼 내용을 콘솔에 출력"""
        for line in self.buffer:
            print(line)

    def print_strategy_params(
        self,
        params: Params,
        start_time: datetime,
        end_time: datetime,
        markets: List[str],
        initial_balance: float,
        total_candles: int,
    ):
        """전략 파라미터 출력 (기본 헤더만)"""
        self.initial_balance = initial_balance

        self.write("==================================================================================")
        self.write("🚀 Backtest Starting")
        self.write("==================================================================================")
        self.write(f"Period: {start_time.strftime('%Y-%m-%d %H:%M:%S')} ~ {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if len(markets) > 5:
            market_list = f"{', '.join(markets[:5])}..."
        else:
            market_list = ", ".join(markets)
        self.write(f"Markets: {len(markets)} ({market_list})")
        self.write(f"Initial Balance: ₩{initial_balance:,.0f}")
        self.write("")

        self.write("📊 Using pre-loaded candle data...")
        self.write(f"  ✓ Loaded: {total_candles} candles for {len(markets)} markets")
        self.write("")

    def print_ml_strategy_params(
        self,
        model_count: int,
        feature_count: int,
        coin_params: Dict[str, Dict],
        trailing_tiers: list,
        stop_loss: float,
        investment_ratio: float,
    ):
        """ML 전략 파라미터 출력"""
        self.write("⚙️  ML Strategy Parameters")
        self.write("==================================================================================")
        self.write("")
        self.write("Model Info:")
        self.write(f"  Total Models: {model_count}개 코인")
        self.write(f"  Features: {feature_count}개")
        self.write(f"  Investment Ratio: {investment_ratio * 100:.1f}%")
        self.write("")
        self.write("Exit Conditions:")
        self.write(f"  Trailing Stop (Tiered):")
        for tier_pct, drop_pct in trailing_tiers:
            self.write(f"    +{tier_pct}% 도달 → 고점 대비 -{drop_pct}% 하락 시 매도")
        self.write(f"  Stop Loss: {stop_loss:.1f}%")
        self.write("")
        self.write("Loaded Models:")

        # 코인 정렬 (BTC, ETH 우선)
        priority = ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL']
        sorted_coins = []
        for coin in priority:
            if coin in coin_params:
                sorted_coins.append(coin)
        for coin in sorted(coin_params.keys()):
            if coin not in sorted_coins:
                sorted_coins.append(coin)

        for market in sorted_coins:
            p = coin_params[market]
            self.write(f"  {market}: hold={p['hold_minutes']}분, threshold={p['threshold']}")

        self.write("")
        self.write("==================================================================================")
        self.write("")

    def print_trade(self, trade: Dict[str, Any]):
        """거래 로그 출력 (날짜별로 그룹화하여 저장)"""
        timestamp_ms = trade.get("timestamp", 0)
        trade_date = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")
        self.trades_by_day[trade_date].append(trade)

    def record_daily_balance(self, date: str, total_balance: float):
        """날짜별 총 자산 기록"""
        self.balance_by_day[date] = total_balance

    def print_trades_by_day(self):
        """날짜별로 그룹화된 거래 로그 출력"""
        # 날짜순으로 정렬
        dates = sorted(self.trades_by_day.keys())

        for date in dates:
            trades = self.trades_by_day[date]

            self.write("")
            self.write("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # 해당 날짜의 잔고와 수익률 표시
            if date in self.balance_by_day and self.initial_balance > 0:
                balance = self.balance_by_day[date]
                return_pct = ((balance - self.initial_balance) / self.initial_balance) * 100
                return_symbol = "+" if return_pct > 0 else ""
                self.write(f"📅 {date} | 총 자산: {format_currency(balance)} | 수익률: {return_symbol}{return_pct:.2f}%")
            else:
                self.write(f"📅 {date}")

            self.write("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # 해당 날짜의 모든 거래 출력
            for trade in trades:
                timestamp = datetime.fromtimestamp(trade["timestamp"] / 1000).strftime("%H:%M:%S")
                side = trade.get("side", "")
                market = trade.get("market", "")
                price = trade.get("price", 0)
                volume = trade.get("volume", 0)
                amount = trade.get("amount", 0)
                reason = trade.get("reason", "")
                realized_pnl = trade.get("realized_pnl")

                if side == "BUY":
                    self.write(f"  [{timestamp}] 🔵 BUY  {market} | Price: ₩{price:,.0f} | Volume: {volume:.8f} | Amount: ₩{amount:,.0f} | Reason: {reason}")
                else:
                    if realized_pnl is not None:
                        if realized_pnl >= 0:
                            pnl_str = f"+₩{realized_pnl:,.0f}"
                        else:
                            pnl_str = f"-₩{abs(realized_pnl):,.0f}"
                    else:
                        pnl_str = "N/A"
                    self.write(f"  [{timestamp}] 🔴 SELL {market} | Price: ₩{price:,.0f} | Volume: {volume:.8f} | Amount: ₩{amount:,.0f} | PnL: {pnl_str} | Reason: {reason}")

    def print_results(self, result: Dict[str, Any]):
        """백테스트 결과 출력"""
        # 먼저 날짜별로 그룹화된 거래 로그 출력
        self.print_trades_by_day()

        start_time = result.get("start_time", datetime.now())
        end_time = result.get("end_time", datetime.now())
        duration = end_time - start_time
        duration_days = duration.total_seconds() / 86400

        total_return = result.get("total_return", 0)
        profit_symbol = "🟢" if total_return >= 0 else "🔴"

        self.write("")
        self.write("==================================================================================")
        self.write("✅ Backtest Completed")
        self.write("==================================================================================")
        self.write("")

        self.write("==================================================================================")
        self.write("📈 BACKTEST RESULTS")
        self.write("==================================================================================")
        self.write("")

        self.write("📅 Period")
        self.write(f"   Start:    {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write(f"   End:      {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.write(f"   Duration: {duration_days:.1f} days")
        self.write("")

        initial_balance = result.get("initial_balance", 0)
        final_balance = result.get("final_balance", 0)
        total_return_percent = result.get("total_return_percent", 0)

        self.write("💰 Returns")
        self.write(f"   Initial Balance:  {format_currency(initial_balance):>15}")
        self.write(f"   Final Balance:    {format_currency(final_balance):>15}")
        self.write(f"   {profit_symbol} Total Return:    {format_currency(total_return):>15} ({total_return_percent:.2f}%)")
        self.write("")

        total_trades = result.get("total_trades", 0)
        buy_count = result.get("buy_count", 0)
        sell_count = result.get("sell_count", 0)

        self.write("📊 Trading Statistics")
        self.write(f"   Total Trades: {total_trades}")
        self.write(f"   Buy Orders:   {buy_count}")
        self.write(f"   Sell Orders:  {sell_count}")
        self.write("")

        # 매수 사유별 통계
        trades = result.get("trades", [])
        buy_reasons: Dict[str, int] = defaultdict(int)
        for trade in trades:
            if trade.get("side") == "BUY":
                buy_reasons[trade.get("reason", "unknown")] += 1

        if buy_reasons:
            self.write("   Buy Reasons:")
            for reason, count in buy_reasons.items():
                self.write(f"     {reason}: {count}")
            self.write("")

        # 매도 사유별 통계
        sell_reasons: Dict[str, int] = defaultdict(int)
        sell_profit_by_reason: Dict[str, float] = defaultdict(float)
        sell_loss_by_reason: Dict[str, float] = defaultdict(float)

        for trade in trades:
            if trade.get("side") == "SELL":
                reason = trade.get("reason", "unknown")
                pnl = trade.get("realized_pnl")
                sell_reasons[reason] += 1
                if pnl is not None:
                    if pnl > 0:
                        sell_profit_by_reason[reason] += pnl
                    elif pnl < 0:
                        sell_loss_by_reason[reason] += pnl

        if sell_reasons:
            self.write("   Sell Reasons:")
            reason_order = ["stop_loss", "early_exit_condition", "early_drop_condition", "trailing_stop_early", "anca_exit_condition", "anca_drop_condition", "trailing_stop_anca", "volume_rank_exit", "period_end"]

            printed_reasons = set()
            for reason in reason_order:
                if reason in sell_reasons:
                    self._print_sell_reason_stats(reason, sell_reasons[reason],
                                                   sell_profit_by_reason[reason],
                                                   sell_loss_by_reason[reason])
                    printed_reasons.add(reason)

            # 정의되지 않은 사유 출력
            for reason, count in sell_reasons.items():
                if reason not in printed_reasons:
                    self._print_sell_reason_stats(reason, count,
                                                   sell_profit_by_reason[reason],
                                                   sell_loss_by_reason[reason])
            self.write("")

        # 승률 계산 (개별 청산 기준)
        sell_trades = 0
        wins = 0
        for trade in trades:
            if trade.get("side") == "SELL" and trade.get("realized_pnl") is not None:
                sell_trades += 1
                if trade["realized_pnl"] > 0:
                    wins += 1

        win_rate = (wins / sell_trades * 100) if sell_trades > 0 else 0
        self.write(f"   Win Rate (개별): {win_rate:.2f}% ({wins} wins / {sell_trades} sells)")

        # 그룹 단위 승률 계산 (추가매수 그룹을 1건으로 계산)
        # 학습 때와 동일한 방식: 코인별로 모든 포지션 청산 시 1건으로 계산
        group_trades, group_wins = self._calculate_group_win_rate(trades)
        group_win_rate = (group_wins / group_trades * 100) if group_trades > 0 else 0
        self.write(f"   Win Rate (그룹): {group_win_rate:.2f}% ({group_wins} wins / {group_trades} trades)")
        self.write("")

        self.write("==================================================================================")
        self.write("")

    def _calculate_group_win_rate(self, trades: List[Dict]) -> tuple:
        """
        그룹 단위 승률 계산 (학습과 동일한 방식)
        - 코인별로 추가매수 그룹을 1건의 거래로 계산
        - 모든 포지션이 청산될 때 그룹 종료, 총 손익으로 승패 결정
        """
        from collections import defaultdict

        # 코인별 현재 포지션 수와 그룹 손익 추적
        coin_positions = defaultdict(int)  # 현재 보유 포지션 수
        coin_group_pnl = defaultdict(float)  # 현재 그룹의 누적 손익

        group_trades = 0
        group_wins = 0

        # 시간순 정렬
        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

        for trade in sorted_trades:
            market = trade.get("market", "")
            side = trade.get("side", "")

            if side == "BUY":
                coin_positions[market] += 1
            elif side == "SELL":
                pnl = trade.get("realized_pnl", 0) or 0
                coin_group_pnl[market] += pnl
                coin_positions[market] -= 1

                # 모든 포지션이 청산되면 그룹 종료
                if coin_positions[market] <= 0:
                    group_trades += 1
                    if coin_group_pnl[market] > 0:
                        group_wins += 1
                    # 그룹 리셋
                    coin_positions[market] = 0
                    coin_group_pnl[market] = 0.0

        return group_trades, group_wins

    def _print_sell_reason_stats(self, reason: str, count: int, profit: float, loss: float):
        """매도 사유별 통계 출력"""
        net_pnl = profit + loss  # loss는 이미 음수

        if profit > 0 and loss < 0:
            self.write(f"     {reason}: {count}건 | 수익: +{format_currency(profit)} | 손실: -{format_currency(abs(loss))} | 순손익: {format_currency(net_pnl)}")
        elif profit > 0:
            self.write(f"     {reason}: {count}건 | 수익: +{format_currency(profit)} | 손실: -₩0 | 순손익: +{format_currency(profit)}")
        elif loss < 0:
            self.write(f"     {reason}: {count}건 | 수익: +₩0 | 손실: -{format_currency(abs(loss))} | 순손익: {format_currency(loss)}")
        else:
            self.write(f"     {reason}: {count}건 | 수익: +₩0 | 손실: -₩0 | 순손익: ₩0")
