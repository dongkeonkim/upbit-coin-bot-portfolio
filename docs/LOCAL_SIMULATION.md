# 로컬 모의투자 실행 가이드

로컬 k3s 환경에서 모의투자를 실행하고 모니터링하는 방법을 설명합니다.

## 사전 요구사항

- k3s 설치 및 실행 중
- Docker 및 로컬 레지스트리 (localhost:5000)
- kubectl 접근 가능

## 빠른 시작

관리 스크립트를 사용하면 간편하게 서비스를 관리할 수 있습니다:

```bash
# 스크립트 위치
./scripts/k3s-simulation.sh

# 사용 가능한 명령어
./scripts/k3s-simulation.sh status    # 파드 상태 및 포트폴리오 확인
./scripts/k3s-simulation.sh start     # 모든 서비스 시작
./scripts/k3s-simulation.sh stop      # 모든 서비스 중지
./scripts/k3s-simulation.sh restart   # 모든 서비스 재시작
./scripts/k3s-simulation.sh portfolio # 포트폴리오 상세 조회
./scripts/k3s-simulation.sh reset     # 포트폴리오 초기화 (1,000만원)
./scripts/k3s-simulation.sh logs <app> # 로그 확인
```

## 환경 설정

### 1. KUBECONFIG 설정

```bash
# 환경변수로 설정 (권장)
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 또는 매번 명령어에 추가
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl ...
```

### 2. 모의투자 모드 확인/설정

```bash
# 현재 모드 확인 (false = 모의투자)
kubectl -n coinbot get secret coinbot-secrets -o jsonpath='{.data.REAL_MODE}' | base64 -d; echo

# 모의투자 모드로 변경 (필요시)
kubectl -n coinbot patch secret coinbot-secrets --type='json' \
  -p='[{"op": "replace", "path": "/data/REAL_MODE", "value": "ZmFsc2U="}]'
```

### 3. 포트폴리오 초기화 (선택사항)

기존 포트폴리오를 삭제하고 1,000만원으로 새로 시작:

```bash
kubectl -n coinbot exec -it $(kubectl -n coinbot get pod -l app=redis -o jsonpath='{.items[0].metadata.name}') \
  -- redis-cli del "portfolio:default_user"
```

## 서비스 구성

모의투자에 필요한 4개 서비스:

| 서비스 | 역할 |
|--------|------|
| market-data-service | Upbit API에서 1분봉 캔들 수집 → Redis 저장 |
| trading-engine | 캔들 분석 → 매수/매도 신호 발행 |
| trade-service | 신호 수신 → 수수료 적용 → trade:completed 발행 |
| asset-service | trade:completed 수신 → 포트폴리오 업데이트 |

> **Note**: trade-service는 모의투자에서도 필요합니다.
> - 실거래: Upbit API로 실제 주문 실행
> - 모의투자: API 호출 없이 수수료(0.05%)만 적용하여 체결 처리

## 서비스 실행

### 서비스 시작

```bash
kubectl -n coinbot scale deployment/asset-service --replicas=1
kubectl -n coinbot scale deployment/market-data-service --replicas=1
kubectl -n coinbot scale deployment/trade-service --replicas=1
kubectl -n coinbot scale deployment/trading-engine --replicas=1
```

### 서비스 재시작

설정 변경 후:

```bash
kubectl -n coinbot rollout restart deployment/asset-service \
  deployment/market-data-service deployment/trade-service deployment/trading-engine
```

### 상태 확인

```bash
kubectl -n coinbot get pods
```

정상 상태:
```
NAME                                  READY   STATUS
asset-service-xxx                     1/1     Running
market-data-service-xxx               1/1     Running
trade-service-xxx                     1/1     Running
trading-engine-xxx                    1/1     Running
redis-xxx                             1/1     Running
```

### 서비스 중지

```bash
kubectl -n coinbot scale deployment/asset-service --replicas=0
kubectl -n coinbot scale deployment/market-data-service --replicas=0
kubectl -n coinbot scale deployment/trade-service --replicas=0
kubectl -n coinbot scale deployment/trading-engine --replicas=0
```

## 모니터링

### 1. 로그 확인

```bash
# 개별 서비스 로그
kubectl -n coinbot logs -f -l app=asset-service        # 포트폴리오 관리
kubectl -n coinbot logs -f -l app=market-data-service  # 시장 데이터 수집
kubectl -n coinbot logs -f -l app=trade-service        # 매매 체결 처리
kubectl -n coinbot logs -f -l app=trading-engine       # 전략 실행

# 전체 로그 (stern 사용 시)
stern -n coinbot ".*" --tail=50
```

### 2. 포트폴리오 상태 확인

```bash
kubectl -n coinbot exec -it $(kubectl -n coinbot get pod -l app=redis -o jsonpath='{.items[0].metadata.name}') \
  -- redis-cli get "portfolio:default_user" | python3 -m json.tool
```

출력 예시:
```json
{
  "userId": "default_user",
  "timestamp": 1766286220380,
  "krw_balance": 10000000,
  "total_krw_value": 10000000,
  "holdings": {}
}
```

### 3. RedisInsight (Redis 데이터 조회)

브라우저에서 http://localhost:30081 접속
- 포트폴리오, 캔들, 매매 신호 등 Redis 데이터 실시간 조회

### 4. Grafana + Loki (로그 검색)

브라우저에서 http://localhost:30080 접속 (관리자 계정은 로컬 Secret에서 확인)

로그 검색:
1. 좌측 메뉴 → Explore
2. 데이터소스: Loki 선택
3. 쿼리: `{namespace="coinbot"}`
4. 특정 서비스: `{namespace="coinbot", app="trading-engine"}`

### 5. 슬랙 알림

서비스 시작/종료 및 매매 체결이 슬랙으로 전송됩니다:

- **서비스 시작**: Mode(SIMULATION/LIVE), Host, Strategy
- **매수 체결**: 마켓, 가격, 금액, 포트폴리오 잔고
- **매도 체결**: 마켓, 가격, 수량, 손익, 포트폴리오 잔고

## 전략 설정

### Drop Bounce 전략 (기본값)

| 항목 | 설정 |
|------|------|
| 매수 조건 | 10분 내 -2.5% 하락 후 +0.5% 반등, 양봉, 아래꼬리 >= 몸통 |
| 거래대금 필터 | 60분 평균의 5배 미만 |
| 트레일링 스탑 | +1.8% 활성화, 고점 대비 -0.3% 하락 시 익절 |
| 손절 | -1.0% |
| 투자 비율 | 잔고의 30% |

### 거래 대상 코인 제한 (ALLOWED_MARKETS)

특정 코인만 거래하도록 제한할 수 있습니다:

```bash
# 현재 설정 확인
kubectl -n coinbot get deployment trading-engine -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ALLOWED_MARKETS")].value}'

# BTC, ETH, XRP만 거래하도록 설정
kubectl -n coinbot set env deployment/trading-engine ALLOWED_MARKETS=KRW-BTC,KRW-ETH,KRW-XRP

# 모든 TOP 10 코인 거래 (제한 해제)
kubectl -n coinbot set env deployment/trading-engine ALLOWED_MARKETS-
```

> **Note**: market-data-service는 TOP 10 캔들을 계속 수집하지만, trading-engine은 ALLOWED_MARKETS에 지정된 코인만 매매합니다.

## 트러블슈팅

### 서비스가 CrashLoopBackOff 상태

```bash
# 로그 확인
kubectl -n coinbot logs -l app=trading-engine --tail=50

# 이벤트 확인
kubectl -n coinbot get events --sort-by='.lastTimestamp' | tail -20
```

### 포트폴리오가 업데이트되지 않음

1. **REAL_MODE 확인**: `false`여야 함
2. **trade-service 실행 확인**: replicas=1 인지 확인
3. **asset-service 로그**: `simulationMode: true` 확인
4. **Redis 연결 상태 확인**

```bash
# trade-service가 신호를 받고 있는지 확인
kubectl -n coinbot logs -l app=trade-service --tail=20
```

### 캔들 데이터가 없음

```bash
# market-data-service 로그 확인
kubectl -n coinbot logs -l app=market-data-service --tail=50

# Redis에서 캔들 데이터 확인
kubectl -n coinbot exec -it $(kubectl -n coinbot get pod -l app=redis -o jsonpath='{.items[0].metadata.name}') \
  -- redis-cli keys "candles:*" | head -10
```

### 매수 신호가 발생하지 않음

```bash
# trading-engine 로그에서 전략 실행 확인
kubectl -n coinbot logs -l app=trading-engine --tail=100 | grep -E "signal|buy|sell"
```

### 429 Rate Limit 에러

Upbit API 호출 제한 초과 시 발생합니다.

**원인**: 로컬에서 서비스가 중복 실행 중일 수 있습니다.

```bash
# 로컬에서 실행 중인 프로세스 확인
ps aux | grep -E "python.*trading|ts-node|node.*service" | grep -v grep

# 중복 프로세스가 있으면 종료
pkill -f "ts-node-dev.*market-data-service"
pkill -f "python.*trading"
```

> **Note**: k3s 파드와 로컬 프로세스가 동시에 Upbit API를 호출하면 rate limit에 걸립니다.

## 참고

### 아키텍처

```
┌────────────────────────────────────────────────────────────────┐
│                      k3s (coinbot namespace)                    │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  market-data-service ──→ Upbit API (1분봉)                     │
│         │                                                       │
│         ↓ candles:{market}:1m                                  │
│       Redis ←─────────────────────────────────────────┐        │
│         │                                             │        │
│         ↓                                             │        │
│  trading-engine (Drop Bounce Strategy)                │        │
│         │                                             │        │
│         ↓ trade:signal:*                              │        │
│  trade-service ──→ 슬랙 알림 (매수/매도 체결)          │        │
│         │ (모의: 수수료 적용, 실거래: Upbit API)       │        │
│         │                                             │        │
│         ↓ trade:completed                             │        │
│  asset-service ←──────────────────────────────────────┘        │
│  (모의 포트폴리오 관리)                                         │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

### 주요 Redis 키

| 키 패턴 | 설명 |
|---------|------|
| `portfolio:{userId}` | 포트폴리오 상태 (잔고, 보유코인) |
| `candles:{market}:1m` | 1분봉 캔들 데이터 |
| `market:top10` | TOP 10 거래대금 마켓 목록 |
| `market:target:{userId}` | 모니터링 대상 마켓 |
| `trade:signal:{market}` | 매매 신호 (BUY/SELL) |
| `trade:completed` | 체결 완료 이벤트 (Pub/Sub 채널) |
