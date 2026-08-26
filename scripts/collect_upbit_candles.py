#!/usr/bin/env python3
"""
업비트 1분봉 캔들 수집 스크립트
- 2024-01-01 ~ 현재까지 수집
- 진행상황 저장 (중단 시 재개 가능)
- Rate limit 준수 (초당 10회)
"""

import os
import sys
import json
import time
import requests
import psycopg2
from datetime import datetime, timedelta
from pathlib import Path

# 설정
UPBIT_API_URL = "https://api.upbit.com/v1"
START_DATE = datetime(2025, 12, 31, 0, 0, 0)  # 기존 데이터 이후부터
END_DATE = datetime(2026, 1, 10, 23, 59, 0)  # 2026년 1월 10일 23:59까지
CANDLES_PER_REQUEST = 200
REQUEST_DELAY = 0.11  # 초당 약 9회 (안전 마진)

# DB 설정
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "coinbot",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "coinbot"
}

# 진행상황 저장 파일
PROGRESS_FILE = Path(__file__).parent / "upbit_collect_progress.json"


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def get_krw_markets():
    """KRW 마켓 목록 조회"""
    resp = requests.get(f"{UPBIT_API_URL}/market/all")
    resp.raise_for_status()
    markets = resp.json()
    return [m['market'] for m in markets if m['market'].startswith('KRW-')]


def get_candles(market: str, to: datetime, count: int = 200):
    """1분봉 캔들 조회"""
    params = {
        "market": market,
        "to": to.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": count
    }
    resp = requests.get(f"{UPBIT_API_URL}/candles/minutes/1", params=params)

    if resp.status_code == 429:
        # Rate limit - 잠시 대기 후 재시도
        print(f"  ⚠️ Rate limit, waiting 1s...")
        time.sleep(1)
        return get_candles(market, to, count)

    resp.raise_for_status()
    return resp.json()


def save_candles_to_db(conn, candles: list, market: str):
    """캔들 데이터 DB 저장"""
    if not candles:
        return 0

    cur = conn.cursor()

    # UPSERT 쿼리
    query = """
        INSERT INTO candles_upbit (market, interval, timestamp, open, high, low, close, volume, quote_volume)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market, interval, timestamp) DO NOTHING
    """

    rows = []
    for c in candles:
        # UTC -> KST 변환 없이 UTC 그대로 저장
        ts = datetime.strptime(c['candle_date_time_utc'], "%Y-%m-%dT%H:%M:%S")
        rows.append((
            market,
            '1m',
            ts,
            c['opening_price'],
            c['high_price'],
            c['low_price'],
            c['trade_price'],
            c['candle_acc_trade_volume'],
            c['candle_acc_trade_price']
        ))

    cur.executemany(query, rows)
    conn.commit()
    cur.close()

    return len(rows)


def fill_missing_candles(conn, market: str):
    """빠진 1분봉을 직전 close 가격으로 채우기 (수집 기간만)"""
    cur = conn.cursor()

    # 수집 기간 내에서만 빈 구간 채우기
    first_time = START_DATE
    last_time = END_DATE

    # 해당 기간 내 기존 데이터 수 확인
    cur.execute("""
        SELECT COUNT(*)
        FROM candles_upbit
        WHERE market = %s AND timestamp >= %s AND timestamp <= %s
    """, (market, first_time, last_time))

    existing_count = cur.fetchone()[0]

    # 이론상 개수 계산
    time_diff = (last_time - first_time).total_seconds() / 60
    expected_count = int(time_diff) + 1
    missing_count = expected_count - existing_count

    if missing_count == 0:
        cur.close()
        return 0

    # 빠진 시간 찾아서 채우기 (generate_series 사용 - 더 효율적)
    cur.execute("""
        WITH time_series AS (
            SELECT generate_series(%s::timestamp, %s::timestamp, '1 minute'::interval) AS ts
        ),
        existing_times AS (
            SELECT timestamp
            FROM candles_upbit
            WHERE market = %s AND timestamp >= %s AND timestamp <= %s
        ),
        missing_times AS (
            SELECT ts.ts
            FROM time_series ts
            LEFT JOIN existing_times et ON ts.ts = et.timestamp
            WHERE et.timestamp IS NULL
        ),
        previous_candles AS (
            SELECT
                mt.ts,
                (SELECT close
                 FROM candles_upbit
                 WHERE market = %s
                   AND timestamp < mt.ts
                 ORDER BY timestamp DESC
                 LIMIT 1) as prev_close
            FROM missing_times mt
        )
        INSERT INTO candles_upbit (market, interval, timestamp, open, high, low, close, volume, quote_volume)
        SELECT
            %s,
            '1m',
            ts,
            COALESCE(prev_close, 0),
            COALESCE(prev_close, 0),
            COALESCE(prev_close, 0),
            COALESCE(prev_close, 0),
            0,
            0
        FROM previous_candles
        WHERE prev_close IS NOT NULL
        ON CONFLICT (market, interval, timestamp) DO NOTHING
    """, (first_time, last_time, market, first_time, last_time, market, market))

    inserted = cur.rowcount
    conn.commit()
    cur.close()

    return inserted


def load_progress():
    """진행상황 로드"""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return {"completed_markets": [], "current_market": None, "current_to": None}


def save_progress(progress: dict):
    """진행상황 저장"""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2, default=str)


def collect_market(conn, market: str, start_from: datetime = None):
    """특정 마켓의 캔들 수집"""

    # 시작 지점 결정
    if start_from:
        current_to = start_from
    else:
        current_to = END_DATE

    total_saved = 0
    empty_count = 0

    while current_to > START_DATE:
        try:
            candles = get_candles(market, current_to, CANDLES_PER_REQUEST)
            time.sleep(REQUEST_DELAY)

            if not candles:
                empty_count += 1
                if empty_count >= 3:
                    # 3번 연속 빈 응답 = 상장 전
                    print(f"  📍 상장 시점 도달: {current_to}")
                    break
                current_to -= timedelta(minutes=CANDLES_PER_REQUEST)
                continue

            empty_count = 0

            # 가장 오래된 캔들 시간
            oldest = datetime.strptime(candles[-1]['candle_date_time_utc'], "%Y-%m-%dT%H:%M:%S")

            # START_DATE 이전 데이터 필터링
            candles = [c for c in candles
                      if datetime.strptime(c['candle_date_time_utc'], "%Y-%m-%dT%H:%M:%S") >= START_DATE]

            saved = save_candles_to_db(conn, candles, market)
            total_saved += saved

            # 진행상황 출력
            print(f"  {oldest.strftime('%Y-%m-%d %H:%M')} | +{saved} | 누적: {total_saved:,}")

            # 다음 요청 시점
            current_to = oldest - timedelta(minutes=1)

        except Exception as e:
            print(f"  ❌ Error: {e}")
            time.sleep(1)
            continue

    return total_saved


def main():
    print("=" * 60)
    print("🚀 업비트 1분봉 캔들 수집 시작")
    print("=" * 60)
    print(f"기간: {START_DATE} ~ {END_DATE}")
    print()

    # 마켓 목록 조회
    print("📋 마켓 목록 조회 중...")
    markets = get_krw_markets()
    print(f"   총 {len(markets)}개 마켓")
    print()

    # 진행상황 로드
    progress = load_progress()
    completed = set(progress.get('completed_markets', []))

    # 재개 지점
    start_market = progress.get('current_market')
    start_to = progress.get('current_to')
    if start_to:
        start_to = datetime.fromisoformat(start_to)

    print(f"✅ 완료된 마켓: {len(completed)}개")
    if start_market:
        print(f"🔄 재개 마켓: {start_market}")
    print()

    # DB 연결
    conn = get_db_connection()

    total_all = 0

    try:
        for i, market in enumerate(markets):
            # 이미 완료된 마켓 건너뛰기
            if market in completed:
                continue

            # 재개 지점까지 건너뛰기
            if start_market and market != start_market:
                continue
            start_market = None  # 재개 후 초기화

            print(f"[{i+1}/{len(markets)}] {market}")

            # 진행상황 저장
            progress['current_market'] = market
            save_progress(progress)

            # 수집
            saved = collect_market(conn, market, start_from=start_to)
            start_to = None  # 재개 후 초기화

            # 빈 구간 채우기
            filled = fill_missing_candles(conn, market)

            total_all += saved
            print(f"  ✅ 완료: {saved:,}개 저장, {filled:,}개 빈 구간 채움")
            print()

            # 완료 기록
            progress['completed_markets'].append(market)
            progress['current_market'] = None
            progress['current_to'] = None
            save_progress(progress)

    except KeyboardInterrupt:
        print("\n⚠️ 중단됨. 진행상황 저장됨.")
        save_progress(progress)

    finally:
        conn.close()

    print("=" * 60)
    print(f"🎉 수집 완료! 총 {total_all:,}개 캔들 저장")
    print("=" * 60)


if __name__ == "__main__":
    main()
