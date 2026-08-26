# Grafana 대시보드 관리

Grafana 대시보드를 ConfigMap으로 프로비저닝하여 파드 재시작 후에도 유지되도록 설정합니다.

## 대시보드 파일

대시보드 JSON 파일 위치:
```
k8s/monitoring/dashboards/coinbot-trading-dashboard.json
```

## 대시보드 설치

### 로컬 k3s

```bash
# 1. ConfigMap 생성
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n monitoring create configmap grafana-coinbot-dashboard \
  --from-file=coinbot-trading-dashboard.json=k8s/monitoring/dashboards/coinbot-trading-dashboard.json \
  --dry-run=client -o yaml | KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl apply -f -

# 2. Grafana sidecar가 인식하도록 라벨 추가
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n monitoring label configmap grafana-coinbot-dashboard grafana_dashboard=1 --overwrite

# 3. Grafana 재시작 (sidecar가 ConfigMap을 로드하도록)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n monitoring rollout restart deployment prometheus-grafana
```

### AWS EKS

```bash
# 1. ConfigMap 생성
kubectl -n monitoring create configmap grafana-coinbot-dashboard \
  --from-file=coinbot-trading-dashboard.json=k8s/monitoring/dashboards/coinbot-trading-dashboard.json \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. Grafana sidecar가 인식하도록 라벨 추가
kubectl -n monitoring label configmap grafana-coinbot-dashboard grafana_dashboard=1 --overwrite

# 3. Grafana 재시작
kubectl -n monitoring rollout restart deployment prometheus-grafana
```

## 대시보드 업데이트

1. Grafana에서 대시보드 수정
2. Settings > JSON Model 복사
3. `k8s/monitoring/dashboards/coinbot-trading-dashboard.json` 파일 업데이트
4. 위의 설치 명령어 다시 실행

## 대시보드 패널 구성

| 패널 | 설명 |
|------|------|
| Trading Analysis | 코인 상태, 매수/매도 신호, 트레일링 |
| Portfolio Updates | 포트폴리오 가치, 포지션 수, 수익률 |
| Errors & Warnings | 에러 및 경고 로그 |
| Market Data Service | 캔들 수집 상태 |
| Trade Service | 주문 실행 로그 |
| Asset Service | 자산 관리 로그 |
| All Services Status | 서비스 시작/연결 상태 |

## Loki 쿼리 예시

### 코인 상태 (Drop Bounce 전략)
```
{namespace="coinbot", pod=~"trading-engine.*"} | json | event =~ "coin_status|buy_|sell_|trailing|position_status" | line_format "{{.timestamp}} [{{.event}}] {{.data}}"
```

### 에러/경고만 조회
```
{namespace="coinbot"} | json | level =~ "error|warn" | line_format "[{{.level}}] {{.service}}: {{.event}} - {{.reason}}{{.msg}}"
```

## 트러블슈팅

### 대시보드가 나타나지 않음

```bash
# ConfigMap 확인
kubectl -n monitoring get configmap grafana-coinbot-dashboard -o yaml

# 라벨 확인
kubectl -n monitoring get configmap grafana-coinbot-dashboard --show-labels

# Grafana sidecar 로그 확인
kubectl -n monitoring logs -l app.kubernetes.io/name=grafana -c grafana-sc-dashboard
```

### Loki 데이터소스 UID 불일치

대시보드 JSON의 `uid` 값이 실제 Loki 데이터소스와 다를 수 있습니다:

```bash
# 실제 Loki 데이터소스 UID 확인 (Grafana UI에서)
# Configuration > Data sources > Loki > 상단 URL에서 uid 확인

# JSON 파일에서 uid 수정
sed -i 's/P8E80F9AEF21F6940/실제UID/g' k8s/monitoring/dashboards/coinbot-trading-dashboard.json
```
