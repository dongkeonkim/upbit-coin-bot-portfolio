# 모니터링 가이드

업비트 코인봇 시스템 상태 확인 명령어 모음

지금의 진행사항을 확인하기
1. 4개의 서비스가 잘 작동하는지 로그들을 확인.
2. PostgreSQL에 저장 잘 되고 있는지 확인.
3. 현재 보유중인 것들이 레디스에 잘 저장되어있는지, 수익률은 얼마인지, Peak 는 얼마였는지 확인.

## 1. 서비스 상태 확인

### 전체 Pod 상태
```bash
kubectl -n coinbot get pods
```

### 개별 서비스 로그
```bash
# trading-engine (매매 신호 생성)
kubectl -n coinbot logs deployment/trading-engine --tail=20

# market-data-service (캔들 수집)
kubectl -n coinbot logs deployment/market-data-service --tail=20

# trade-service (주문 실행)
kubectl -n coinbot logs deployment/trade-service --tail=20

# asset-service (포트폴리오 관리)
kubectl -n coinbot logs deployment/asset-service --tail=20
```

### 실시간 로그 스트리밍
```bash
kubectl -n coinbot logs -f deployment/trading-engine
```

### 에러만 필터링
```bash
kubectl -n coinbot logs deployment/trading-engine --tail=100 | grep -E "error|Error|ERROR"
```

---

## 2. PostgreSQL (TimescaleDB) 확인

### 캔들 데이터 현황
```bash
kubectl -n coinbot exec -it deployment/postgresql -- psql -U coinbot -d coinbot -c \
  "SELECT market, COUNT(*) as cnt, MIN(timestamp) as oldest, MAX(timestamp) as newest
   FROM candles GROUP BY market ORDER BY market;"
```

### 최근 캔들 조회
```bash
kubectl -n coinbot exec -it deployment/postgresql -- psql -U coinbot -d coinbot -c \
  "SELECT * FROM candles WHERE market='KRW-BTC' ORDER BY timestamp DESC LIMIT 5;"
```

### 압축 상태 확인
```bash
kubectl -n coinbot exec -it deployment/postgresql -- psql -U coinbot -d coinbot -c \
  "SELECT * FROM timescaledb_information.compression_settings;"
```

### 테이블 크기 확인
```bash
kubectl -n coinbot exec -it deployment/postgresql -- psql -U coinbot -d coinbot -c \
  "SELECT hypertable_size('candles');"
```

---

## 3. Redis 상태 확인

### 포트폴리오 조회
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli GET "portfolio:default_user"
```

### 포트폴리오 (보기 좋게 포맷팅)
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli GET "portfolio:default_user" | python3 -m json.tool
```

### 트레일링 상태 조회 (entry_count, entry_time, peak_profit, highest_price)
```bash
# 전체 조회
kubectl -n coinbot exec deployment/redis -- redis-cli MGET \
  "entry_count:default_user:KRW-SOL" \
  "entry_count:default_user:KRW-ETH" \
  "entry_count:default_user:KRW-BTC" \
  "entry_time:default_user:KRW-SOL" \
  "entry_time:default_user:KRW-ETH" \
  "entry_time:default_user:KRW-BTC" \
  "peak_profit:default_user:KRW-SOL" \
  "peak_profit:default_user:KRW-ETH" \
  "peak_profit:default_user:KRW-BTC" \
  "highest_price:default_user:KRW-SOL" \
  "highest_price:default_user:KRW-ETH" \
  "highest_price:default_user:KRW-BTC"
```

### 특정 코인 상태 조회
```bash
MARKET="KRW-BTC"
kubectl -n coinbot exec deployment/redis -- redis-cli MGET \
  "entry_count:default_user:$MARKET" \
  "entry_time:default_user:$MARKET" \
  "peak_profit:default_user:$MARKET" \
  "highest_price:default_user:$MARKET"
```

### 사용자 관련 모든 Redis 키 조회
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli KEYS "*:default_user:*"
```

### 캔들 캐시 확인
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli KEYS "candles:*" | head -10
```

---

## 4. 수익률 계산

### 현재 수익률 조회 (Python 스크립트)
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli GET "portfolio:default_user" | python3 -c "
import json, sys
import urllib.request

portfolio = json.loads(sys.stdin.read())
with urllib.request.urlopen('https://api.upbit.com/v1/ticker?markets=KRW-BTC,KRW-ETH,KRW-SOL,KRW-XRP') as r:
    prices_raw = json.loads(r.read())
prices = {p['market'].split('-')[1]: p['trade_price'] for p in prices_raw}

print('| 코인 | 평균가 | 현재가 | 수익률 | 손익 |')
print('|------|--------|--------|--------|------|')

total_pnl = 0
total_value = portfolio['krw_balance']

for coin, h in portfolio['holdings'].items():
    avg, bal = h['avg_buy_price'], h['balance']
    cur = prices.get(coin, 0)
    if cur == 0: continue
    pnl = bal * (cur - avg)
    total_pnl += pnl
    total_value += bal * cur
    print(f'| {coin} | {avg:,.0f}원 | {cur:,.0f}원 | {(cur-avg)/avg*100:+.2f}% | {pnl:+,.0f}원 |')

print(f'\n총 평가금액: {total_value:,.0f}원 | 총 손익: {total_pnl:+,.0f}원')
"
```

---

## 5. 수동 상태 수정

### entry_count 수정
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli SET "entry_count:default_user:KRW-BTC" "3"
```

### entry_time 수정 (현재 시간으로)
```bash
ENTRY_TIME=$(date -Iseconds)
kubectl -n coinbot exec deployment/redis -- redis-cli SET "entry_time:default_user:KRW-BTC" "$ENTRY_TIME"
```

### 여러 값 한번에 수정
```bash
kubectl -n coinbot exec deployment/redis -- redis-cli MSET \
  "entry_count:default_user:KRW-SOL" "3" \
  "entry_count:default_user:KRW-ETH" "3" \
  "entry_count:default_user:KRW-BTC" "3"
```

### 트레일링 상태 초기화 (매도 후 수동 정리)
```bash
MARKET="KRW-BTC"
kubectl -n coinbot exec deployment/redis -- redis-cli DEL \
  "entry_count:default_user:$MARKET" \
  "entry_time:default_user:$MARKET" \
  "peak_profit:default_user:$MARKET" \
  "highest_price:default_user:$MARKET" \
  "trailing:default_user:$MARKET:early" \
  "trailing:default_user:$MARKET:anca"
```

---

## 6. 서비스 재시작

### 개별 서비스 재시작
```bash
kubectl -n coinbot rollout restart deployment/trading-engine
kubectl -n coinbot rollout restart deployment/market-data-service
kubectl -n coinbot rollout restart deployment/trade-service
kubectl -n coinbot rollout restart deployment/asset-service
```

### 재시작 상태 확인
```bash
kubectl -n coinbot rollout status deployment/trading-engine
```

---

## 7. 전체 상태 확인 스크립트

```bash
#!/bin/bash
echo "========== 서비스 상태 =========="
kubectl -n coinbot get pods

echo -e "\n========== PostgreSQL 캔들 현황 =========="
kubectl -n coinbot exec -it deployment/postgresql -- psql -U coinbot -d coinbot -c \
  "SELECT market, COUNT(*) as cnt, MAX(timestamp) as newest FROM candles GROUP BY market ORDER BY market;"

echo -e "\n========== Redis 트레일링 상태 =========="
kubectl -n coinbot exec deployment/redis -- redis-cli KEYS "*:default_user:*" | sort

echo -e "\n========== 현재 수익률 =========="
kubectl -n coinbot exec deployment/redis -- redis-cli GET "portfolio:default_user" | python3 -c "
import json, sys, urllib.request
portfolio = json.loads(sys.stdin.read())
with urllib.request.urlopen('https://api.upbit.com/v1/ticker?markets=KRW-BTC,KRW-ETH,KRW-SOL,KRW-XRP') as r:
    prices = {p['market'].split('-')[1]: p['trade_price'] for p in json.loads(r.read())}
total_pnl = 0
for coin, h in portfolio['holdings'].items():
    cur = prices.get(coin, 0)
    if cur == 0: continue
    pnl = h['balance'] * (cur - h['avg_buy_price'])
    total_pnl += pnl
    print(f\"{coin}: {(cur-h['avg_buy_price'])/h['avg_buy_price']*100:+.2f}% ({pnl:+,.0f}원)\")
print(f'총 손익: {total_pnl:+,.0f}원')
"
```

---

## 8. Redis 키 패턴 정리

| 키 패턴 | 설명 | 예시 |
|---------|------|------|
| `portfolio:{userId}` | 포트폴리오 (잔고, 보유량) | `portfolio:default_user` |
| `entry_count:{userId}:{market}` | 매수 횟수 (최대 3) | `entry_count:default_user:KRW-BTC` |
| `entry_time:{userId}:{market}` | 첫 진입 시간 (6시간 체크) | `entry_time:default_user:KRW-BTC` |
| `peak_profit:{userId}:{market}` | 피크 수익률 (%) | `peak_profit:default_user:KRW-BTC` |
| `highest_price:{userId}:{market}` | 최고가 (원) | `highest_price:default_user:KRW-BTC` |
| `trailing:{userId}:{market}:early` | Early 트레일링 활성화 | `trailing:default_user:KRW-BTC:early` |
| `trailing:{userId}:{market}:anca` | ANCA 트레일링 활성화 | `trailing:default_user:KRW-BTC:anca` |
| `candles:{market}:1m` | 1분봉 캔들 캐시 | `candles:KRW-BTC:1m` |
| `market:top10` | TOP 10 마켓 목록 | - |
| `market:target:{userId}` | 타겟 마켓 목록 | `market:target:default_user` |

---

## 9. 주요 설정값 (ml_strategy.py)

| 설정 | 값 | 설명 |
|------|-----|------|
| `HOLD_MINUTES` | 360 | 6시간 후 자동 매도 |
| `THRESHOLD` | 0.40 | 신규 매수 임계값 |
| `THRESHOLD_INCREMENT` | 0.10 | 추가 매수 시 임계값 증가분 |
| `COIN_MAX_POSITIONS` | 3 | 코인당 최대 매수 횟수 |
| `TRAILING_TRIGGER_PCT` | 1.8 | 트레일링 시작 수익률 (%) |
| `TRAILING_DROP_PCT` | 0.3 | 트레일링 하락폭 (%) |
| `SL_PCT` | 1.0 | 손절 기준 (%) |
