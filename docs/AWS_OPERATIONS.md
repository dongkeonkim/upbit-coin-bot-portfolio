# AWS 운영 가이드

AWS EKS 클러스터에서 운영 중인 서비스 관리 및 모니터링 가이드입니다.

## 클러스터 정보

- **클러스터**: `<cluster-name>.<region>.eksctl.io`
- **리전**: ap-northeast-2 (서울)
- **네임스페이스**: coinbot, monitoring

## 서비스 구성

| 서비스 | Replicas | 역할 |
|--------|----------|------|
| market-data-service | 1 | Upbit API에서 1분봉 캔들 수집 |
| trading-engine | 2 (HA) | 매매 신호 생성, 트레일링 익절/손절 |
| trade-service | 1 | 주문 실행 (Upbit API) |
| asset-service | 1 | 포트폴리오 관리 |
| redis | 1 | 상태 저장, 서비스 간 통신 |
| redisinsight | 1 | Redis 데이터 조회 UI |

## 모니터링

### Grafana + Loki (로그)

```bash
# 포트포워딩
kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80

# 브라우저: http://localhost:3000
# 관리자 계정은 배포 시 생성한 Secret에서 확인
```

**Loki 쿼리 (Drop Bounce 전략):**
```
{namespace="coinbot", pod=~"trading-engine.*"} | json | event =~ "coin_status|buy_|sell_|trailing|position_status"
```

### RedisInsight (Redis 데이터)

```bash
# 포트포워딩
kubectl -n coinbot port-forward svc/redisinsight 8001:5540

# 브라우저: http://localhost:8001
```

## 운영 명령어

### 서비스 상태 확인

```bash
kubectl -n coinbot get pods
kubectl -n coinbot logs -f -l app=trading-engine --tail=50
```

### 포트폴리오 확인

```bash
kubectl -n coinbot exec -it $(kubectl -n coinbot get pod -l app=redis -o jsonpath='{.items[0].metadata.name}') \
  -- redis-cli get "portfolio:default_user" | python3 -m json.tool
```

### 서비스 재시작

```bash
kubectl -n coinbot rollout restart deployment/trading-engine
kubectl -n coinbot rollout restart deployment/market-data-service
```

## 트러블슈팅

### Grafana CrashLoopBackOff

**원인**: 여러 데이터소스가 `isDefault: true`로 설정됨

**해결**:
```bash
# Loki 데이터소스의 isDefault를 false로 변경
kubectl -n monitoring get configmap loki-loki-stack -o yaml | \
  sed 's/isDefault: true/isDefault: false/' | kubectl apply -f -

# Grafana 재시작
kubectl -n monitoring rollout restart deployment prometheus-grafana
```

### 파드 재시작 이력 확인

```bash
kubectl -n coinbot describe pod <pod-name> | grep -A 10 "Last State"
```

## 변경 이력

### 2025-12-21

- Grafana 데이터소스 충돌 해결 (Loki isDefault: false)
- RedisInsight 설치 (coinbot 네임스페이스)
