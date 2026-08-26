# Asset Service

업비트 API를 통해 사용자의 자산(원화 + 보유 코인)을 조회하고 Redis에 저장하는 서비스입니다.

## 기능

- 업비트 계좌 정보 조회 (인증 API 사용)
- 1시간 주기 자동 자산 동기화
- 거래 완료 시 실시간 자산 업데이트
- Redis에 포트폴리오 저장
- Kafka 거래완료 이벤트 구독

## 환경 변수

`.env.example`을 `.env`로 복사하고 필요한 값을 설정하세요.

```bash
# Upbit API (실제 API 키 필요)
UPBIT_API_URL=https://api.upbit.com
UPBIT_ACCESS_KEY=your_access_key_here
UPBIT_SECRET_KEY=your_secret_key_here

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Kafka
KAFKA_BROKERS=localhost:9092
KAFKA_TOPIC_TRADES_COMPLETED=trades.completed

# Asset Monitoring
USER_ID=default_user
SYNC_INTERVAL_HOURS=1
```

## Redis 데이터 구조

```json
{
  "userId": "default_user",
  "timestamp": 1700000000000,
  "krw_balance": 5000000,
  "total_krw_value": 5500000,
  "holdings": {
    "BTC": {
      "currency": "BTC",
      "balance": 0.5,
      "locked": 0,
      "avg_buy_price": 50000000
    }
  }
}
```

Redis Key: `portfolio:{userId}`

## Kafka 메시지 수신 포맷

```json
{
  "userId": "default_user",
  "market": "KRW-BTC",
  "side": "BUY",
  "price": 50000000,
  "volume": 0.1,
  "timestamp": 1700000000000
}
```

Topic: `trades.completed`

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
docker build -t asset-service .

# 컨테이너 실행
docker run -d \
  --name asset-service \
  --env-file .env \
  -p 3002:3002 \
  asset-service
```

## 아키텍처

```
Asset Service
├── Upbit Auth Client (계좌 조회)
├── Redis Client (포트폴리오 저장)
├── Kafka Consumer (거래완료 이벤트 수신)
└── Asset Sync Service (자산 동기화)
```

## 동작 흐름

1. **주기적 동기화**: 1시간마다 자동으로 포트폴리오 동기화
2. **이벤트 기반 동기화**: 거래 완료 이벤트 수신 시 즉시 동기화
3. **Redis 업데이트**: 조회한 자산 정보를 Redis에 저장
4. **다른 서비스 활용**: 분석 서비스에서 Redis 데이터 활용
