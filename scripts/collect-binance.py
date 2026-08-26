"""
바이낸스 과거 데이터 수집 스크립트
Binance Historical Data Collection Script

바이낸스 API에서 과거 캔들 데이터를 가져와 PostgreSQL에 저장
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional
import httpx
import asyncpg
from tqdm import tqdm

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# === 설정 ===

BINANCE_BASE_URL = "https://api.binance.com"
RATE_LIMIT_DELAY = 0.1  # 100ms delay between requests

# 환율 설정 (USD → KRW)
USD_TO_KRW_RATE = 1400

# PostgreSQL 연결 정보
DB_CONFIG = {
    "host": "localhost",  # Docker 외부에서 실행 시
    "port": 5432,
    "database": "coinbot",
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
}

# 수집 대상 심볼 (BTC, ETH, XRP만)
TARGET_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'XRPUSDT']

# 스테이블코인 제외 목록
STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD",
    "USDD", "GUSD", "FRAX", "LUSD", "SUSD", "USDJ", "UST",
}

INTERVALS = ["1m"]

# 2025년 12월 12일 ~ 12월 18일 (추가 수집)
START_TIME = int(datetime(2025, 12, 12).timestamp() * 1000)
END_TIME = int(datetime(2025, 12, 18, 23, 59, 59).timestamp() * 1000)


def convert_symbol(binance_symbol: str) -> str:
    """바이낸스 심볼을 업비트 형식으로 변환 (BTCUSDT → KRW-BTC)"""
    if binance_symbol.endswith("USDT"):
        base = binance_symbol[:-4]  # USDT 제거
        return f"KRW-{base}"
    return binance_symbol


def convert_interval(binance_interval: str) -> str:
    """바이낸스 인터벌 변환"""
    return binance_interval


# === 바이낸스 API 클라이언트 ===

class BinanceClient:
    """바이낸스 API 클라이언트"""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.request_count = 0

    async def close(self):
        await self.client.aclose()

    async def get_top_symbols_by_volume(self, top_n: int = 30) -> List[str]:
        """거래대금 기준 상위 N개 USDT 페어 조회 (스테이블코인 제외)"""
        response = await self.client.get(f"{BINANCE_BASE_URL}/api/v3/ticker/24hr")
        response.raise_for_status()
        tickers = response.json()

        # USDT 페어만 필터링 + 스테이블코인 제외
        usdt_pairs = []
        for t in tickers:
            symbol = t["symbol"]
            if not symbol.endswith("USDT"):
                continue
            # 베이스 코인 추출 (BTCUSDT → BTC)
            base = symbol[:-4]
            if base in STABLECOINS:
                continue
            usdt_pairs.append(t)

        # 거래대금 순 정렬
        usdt_pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)

        top_symbols = [t["symbol"] for t in usdt_pairs[:top_n]]

        logger.info(f"Top {top_n} symbols by 24h volume:")
        for i, t in enumerate(usdt_pairs[:top_n], 1):
            volume_b = float(t["quoteVolume"]) / 1_000_000_000
            logger.info(f"  {i:2d}. {t['symbol']:12s} - ${volume_b:.2f}B")

        return top_symbols

    async def get_all_usdt_symbols(self) -> set:
        """현재 바이낸스에 상장된 모든 USDT 페어 조회 (상장폐지 체크용)"""
        response = await self.client.get(f"{BINANCE_BASE_URL}/api/v3/ticker/24hr")
        response.raise_for_status()
        tickers = response.json()
        return {t["symbol"] for t in tickers if t["symbol"].endswith("USDT")}

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit: int = 1000,
    ) -> List[list]:
        """Klines 데이터 조회"""
        await asyncio.sleep(RATE_LIMIT_DELAY)

        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
        }

        response = await self.client.get(f"{BINANCE_BASE_URL}/api/v3/klines", params=params)
        response.raise_for_status()

        self.request_count += 1
        return response.json()

    async def get_klines_range(
        self, symbol: str, interval: str, start_time: int, end_time: int
    ) -> List[list]:
        """기간별 전체 Klines 조회 (페이지네이션 자동 처리)"""
        all_klines = []
        current_start = start_time
        max_limit = 1000

        logger.info(
            f"Fetching {symbol} {interval}: {datetime.fromtimestamp(start_time/1000)} ~ "
            f"{datetime.fromtimestamp(end_time/1000)}"
        )

        with tqdm(desc=f"{symbol} {interval}", unit=" klines") as pbar:
            while current_start < end_time:
                klines = await self.get_klines(
                    symbol=symbol,
                    interval=interval,
                    start_time=current_start,
                    end_time=end_time,
                    limit=max_limit,
                )

                if not klines:
                    break

                all_klines.extend(klines)
                pbar.update(len(klines))

                # 마지막 캔들의 종료 시간 + 1ms를 다음 시작점으로
                last_kline = klines[-1]
                current_start = last_kline[6] + 1

        logger.info(f"Fetched {len(all_klines)} klines for {symbol} {interval}")
        return all_klines


# === PostgreSQL 저장 ===

async def ensure_table_exists(pool: asyncpg.Pool):
    """candles 테이블 생성"""
    create_table_query = """
        CREATE TABLE IF NOT EXISTS candles (
            id BIGSERIAL,
            market VARCHAR(20) NOT NULL,
            interval VARCHAR(10) NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            open_time TIMESTAMPTZ NOT NULL,
            close_time TIMESTAMPTZ NOT NULL,
            open DECIMAL(20, 8) NOT NULL,
            high DECIMAL(20, 8) NOT NULL,
            low DECIMAL(20, 8) NOT NULL,
            close DECIMAL(20, 8) NOT NULL,
            volume DECIMAL(20, 8) NOT NULL,
            trade_count INTEGER NOT NULL,
            quote_asset_volume DECIMAL(20, 8) NOT NULL,
            taker_buy_base_asset_volume DECIMAL(20, 8),
            taker_buy_quote_asset_volume DECIMAL(20, 8),
            completed BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (market, interval, timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_candles_market_interval_timestamp
            ON candles (market, interval, timestamp DESC);

        CREATE INDEX IF NOT EXISTS idx_candles_timestamp
            ON candles (timestamp DESC);
    """

    async with pool.acquire() as conn:
        await conn.execute(create_table_query)

    logger.info("Candles table and indexes ensured")


async def insert_klines_batch(pool: asyncpg.Pool, klines: List[list], symbol: str, interval: str):
    """Klines 배치 저장"""
    if not klines:
        return 0

    market = convert_symbol(symbol)
    interval_str = convert_interval(interval)

    # 데이터 변환 (USD → KRW 환율 적용)
    records = []
    for kline in klines:
        record = (
            market,
            interval_str,
            datetime.fromtimestamp(kline[0] / 1000),  # timestamp
            datetime.fromtimestamp(kline[0] / 1000),  # open_time
            datetime.fromtimestamp(kline[6] / 1000),  # close_time
            float(kline[1]) * USD_TO_KRW_RATE,  # open (KRW)
            float(kline[2]) * USD_TO_KRW_RATE,  # high (KRW)
            float(kline[3]) * USD_TO_KRW_RATE,  # low (KRW)
            float(kline[4]) * USD_TO_KRW_RATE,  # close (KRW)
            float(kline[5]),  # volume (코인 수량, 환율 적용 안함)
            int(kline[8]),  # trade_count
            float(kline[7]) * USD_TO_KRW_RATE,  # quote_asset_volume (KRW)
            float(kline[9]),  # taker_buy_base_asset_volume (코인 수량)
            float(kline[10]) * USD_TO_KRW_RATE,  # taker_buy_quote_asset_volume (KRW)
            True,  # completed
            datetime.fromtimestamp(kline[0] / 1000),  # created_at
            datetime.fromtimestamp(kline[6] / 1000),  # updated_at
        )
        records.append(record)

    # 배치 INSERT
    insert_query = """
        INSERT INTO candles (
            market, interval, timestamp, open_time, close_time,
            open, high, low, close, volume,
            trade_count, quote_asset_volume,
            taker_buy_base_asset_volume, taker_buy_quote_asset_volume,
            completed, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
        ON CONFLICT (market, interval, timestamp)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            trade_count = EXCLUDED.trade_count,
            quote_asset_volume = EXCLUDED.quote_asset_volume,
            taker_buy_base_asset_volume = EXCLUDED.taker_buy_base_asset_volume,
            taker_buy_quote_asset_volume = EXCLUDED.taker_buy_quote_asset_volume,
            completed = EXCLUDED.completed,
            updated_at = EXCLUDED.updated_at
    """

    async with pool.acquire() as conn:
        await conn.executemany(insert_query, records)

    return len(records)


# === 메인 함수 ===

async def get_symbols_to_collect(binance: BinanceClient, pool: asyncpg.Pool, top_n: int = 30) -> List[str]:
    """수집할 심볼 목록 조회 (상위 N개 + 기존 DB 마켓)"""
    # 1. 현재 거래대금 상위 N개
    top_symbols = set(await binance.get_top_symbols_by_volume(top_n))

    # 2. 기존 DB에 있는 마켓 조회
    existing_rows = await pool.fetch("SELECT DISTINCT market FROM candles")
    existing_symbols = set()
    for row in existing_rows:
        market = row["market"]
        if market.startswith("KRW-"):
            # KRW-BTC → BTCUSDT
            base = market.replace("KRW-", "")
            existing_symbols.add(f"{base}USDT")

    # 3. 현재 바이낸스에 상장된 심볼 조회 (상장폐지 필터링)
    active_symbols = await binance.get_all_usdt_symbols()

    # 4. 합집합 & 상장폐지 제외
    all_symbols = (top_symbols | existing_symbols) & active_symbols

    # 로깅
    new_symbols = top_symbols - existing_symbols
    continued_symbols = existing_symbols & active_symbols
    delisted_symbols = existing_symbols - active_symbols

    if new_symbols:
        logger.info(f"New symbols to collect: {sorted(new_symbols)}")
    if delisted_symbols:
        logger.warning(f"Delisted symbols (skipped): {sorted(delisted_symbols)}")
    logger.info(f"Total symbols: {len(all_symbols)} (top {top_n}: {len(top_symbols)}, existing: {len(continued_symbols)})")

    return sorted(all_symbols)


async def main():
    """메인 수집 함수"""
    logger.info("Starting Binance data collection")

    # 바이낸스 클라이언트
    binance = BinanceClient()

    try:
        # PostgreSQL 연결
        pool = await asyncpg.create_pool(**DB_CONFIG, min_size=5, max_size=20)
        logger.info("PostgreSQL pool created")

        try:
            # 테이블 생성
            await ensure_table_exists(pool)

            # 수집할 심볼 목록 (BTC, ETH, XRP만)
            symbols = TARGET_SYMBOLS
            logger.info(f"Collecting {len(symbols)} symbols: {symbols}")
            logger.info(f"Intervals: {INTERVALS}")

            total_collected = 0
            total_tasks = len(symbols) * len(INTERVALS)
            completed_tasks = 0

            # 각 심볼과 인터벌 조합별로 데이터 수집
            for symbol in symbols:
                for interval in INTERVALS:
                    completed_tasks += 1
                    logger.info(f"Processing {symbol} - {interval} ({completed_tasks}/{total_tasks})")

                    try:
                        # 데이터 가져오기
                        klines = await binance.get_klines_range(
                            symbol=symbol,
                            interval=interval,
                            start_time=START_TIME,
                            end_time=END_TIME,
                        )

                        # 데이터베이스 저장
                        inserted = await insert_klines_batch(pool, klines, symbol, interval)
                        total_collected += inserted

                        logger.info(
                            f"Completed {symbol} {interval}: {inserted} candles saved "
                            f"(Total: {total_collected})"
                        )

                    except Exception as e:
                        logger.error(f"Failed to process {symbol} {interval}: {e}", exc_info=True)

            logger.info(f"Data collection completed: {total_collected} total candles")
            logger.info(f"Binance API requests: {binance.request_count}")

        finally:
            await pool.close()
            logger.info("PostgreSQL pool closed")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await binance.close()


if __name__ == "__main__":
    asyncio.run(main())
