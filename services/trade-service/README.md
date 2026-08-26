# Trade Service

분석 서비스에서 발행한 거래 신호를 수신하여 업비트 API로 실제 매수/매도 주문을 실행하는 서비스입니다.

## 기능

- Kafka 거래 신호 구독
- 업비트 실시간 주문 실행 (매수/매도)
- 거래 완료 이벤트 발행
- Dry Run 모드 지원 (테스트용)
- 안전한 주문 실행 (API 키 검증)

## 환경 변수

`.env.example`을 `.env`로 복사하고 필요한 값을 설정하세요.

```bash
# Upbit API (실제 거래용 - 필수)
UPBIT_API_URL=https://api.upbit.com
UPBIT_ACCESS_KEY=your_access_key_here
UPBIT_SECRET_KEY=your_secret_key_here

# Kafka
KAFKA_BROKERS=localhost:9092
KAFKA_TOPIC_TRADE_SIGNALS=trade.signals
KAFKA_TOPIC_TRADES_COMPLETED=trades.completed

# Trading Mode (안전 설정)
REAL_MODE=false
DRY_RUN_MODE=true
```

## ⚠️  안전 설정

### 실제 거래 활성화 전 체크리스트

1. **API 키 권한**: 주문 권한만 부여 (출금 권한 제거)
2. **Dry Run 테스트**: 충분한 테스트 후 실거래 전환
3. **자금 관리**: 소액으로 시작하여 점진적 증액
4. **모니터링**: 로그 및 거래 내역 지속 확인

### Dry Run 모드

```bash
# 테스트용 (실제 거래 없음)
DRY_RUN_MODE=true
REAL_MODE=false
```

거래 신호를 수신하고 로직을 실행하지만 실제 주문은 하지 않습니다.

### 실거래 모드

```bash
# 실제 거래 (주의!)
DRY_RUN_MODE=false
REAL_MODE=true
```

## Kafka 메시지 포맷

### 입력 (Trade Signal)
```json
{
  "userId": "default_user",
  "signalType": "BUY",
  "market": "KRW-BTC",
  "price": 50000000,
  "reason": "momentum_surge",
  "timestamp": 1700000000000,
  "amount": 4750000
}
```

### 출력 (Trade Completed)
```json
{
  "userId": "default_user",
  "market": "KRW-BTC",
  "side": "BUY",
  "price": 50000000,
  "volume": 0.095,
  "timestamp": 1700000001000
}
```

## 실행 방법

### 로컬 개발

```bash
# 의존성 설치
npm install

# 개발 모드 실행 (Dry Run)
npm run dev

# 프로덕션 빌드
npm run build
npm start
```

### Docker

```bash
# 이미지 빌드
docker build -t trade-service .

# 컨테이너 실행 (Dry Run)
docker run -d \
  --name trade-service \
  --env-file .env \
  -p 3003:3003 \
  trade-service
```

## 아키텍처

```
Trade Service
├── Kafka Consumer (거래 신호 수신)
├── Upbit Trade Client (주문 실행)
├── Trade Executor (거래 로직)
└── Kafka Producer (거래완료 발행)
```

## 거래 흐름

```
1. Kafka → 거래 신호 수신
2. Signal Validation → 신호 검증
3. Upbit API → 시장가 주문 실행
   - BUY: 지정된 KRW로 매수
   - SELL: 보유 수량 전량 매도
4. Order Confirmation → 주문 체결 확인
5. Kafka → 거래완료 이벤트 발행
6. Asset Service → 자산 즉시 동기화
```

## 에러 처리

- **API 키 미설정**: 경고 로그 출력, 거래 스킵
- **주문 실패**: 에러 로그 출력, 재시도 없음
- **Kafka 연결 끊김**: 자동 재연결 시도
- **잔고 부족**: 업비트 API 에러 반환

## 모니터링

로그를 통해 다음 정보 확인:

- 수신한 거래 신호
- 실행한 주문 내역
- API 응답 및 에러
- 거래완료 이벤트 발행
