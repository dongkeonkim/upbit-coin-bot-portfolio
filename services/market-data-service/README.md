# Market Data Service

업비트 API를 통해 거래대금 상위 10개 코인과 보유 코인의 1분봉 데이터를 수집하여 Kafka에 발행하는 서비스입니다.

## 기능

- 거래대금 기준 TOP 10 코인 자동 선정
- 사용자 보유 코인 자동 추가
- 1분 주기로 캔들 데이터 수집
- Kafka로 실시간 캔들 데이터 발행
- Redis에서 포트폴리오 정보 조회

## 환경 변수

`.env.example`을 `.env`로 복사하고 필요한 값을 설정하세요.

```bash
# Upbit API
UPBIT_API_URL=https://api.upbit.com

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Kafka
KAFKA_BROKERS=localhost:9092
KAFKA_TOPIC_CANDLES=market.candles

# Market Selection
TOP_MARKETS_COUNT=10
CANDLE_INTERVAL_MINUTES=1
```

## 실행 방법

### 로컬 개발

```bash
# 의존성 설치
npm install

# 개발 모드 실행
npm run dev

# 프로덕션 빌드
npm run build
npm start
```

### Docker

```bash
# 이미지 빌드
docker build -t market-data-service .

# 컨테이너 실행
docker run -d \
  --name market-data-service \
  --env-file .env \
  -p 3001:3001 \
  market-data-service
```

## Kafka 메시지 포맷

```json
{
  "market": "KRW-BTC",
  "timestamp": 1700000000000,
  "open": 50000000,
  "high": 51000000,
  "low": 49500000,
  "close": 50500000,
  "volume": 10.5,
  "quote_volume": 525000000,
  "interval": "1m"
}
```

## 아키텍처

```
Market Data Service
├── Upbit API Client (시세 조회)
├── Redis Client (포트폴리오 조회)
├── Market Selector (TOP 10 + 보유 코인 선정)
├── Candle Collector (1분봉 수집)
└── Kafka Producer (캔들 발행)
```
