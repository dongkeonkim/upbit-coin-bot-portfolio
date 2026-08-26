# EKS 기반 트레이딩 시스템 운영 경험과 역량

## 개요

EKS 기반 암호화폐 자동매매 시스템을 운영하면서 얻을 수 있는 기술 경험과 역량 정리.

---

## 1. Kubernetes 핵심 역량

### 리소스 관리
- **Pod/Deployment/Service** 생명주기 관리
- **ConfigMap/Secret** 설정 및 민감정보 관리
- **리소스 제한** (requests/limits) 설정 및 튜닝

### 고가용성 구성
- **Leader Election** (Lease API) 기반 Active-Standby
- **롤링 업데이트**, 무중단 배포
- **롤백** 전략 및 실행

### 헬스 관리
- **livenessProbe** - 컨테이너 생존 확인
- **readinessProbe** - 트래픽 수신 준비 확인
- 장애 자동 복구 이해

### 실습 명령어
```bash
kubectl -n coinbot get pods
kubectl -n coinbot describe deployment trading-engine
kubectl -n coinbot rollout restart deployment/trading-engine
kubectl -n coinbot rollout undo deployment/trading-engine
kubectl -n coinbot get lease trading-engine-lock -o yaml
```

---

## 2. AWS 인프라 경험

### EKS (Elastic Kubernetes Service)
- 클러스터 생성, 노드 그룹 관리
- 워커 노드 스케일링
- 클러스터 업그레이드

### ECR (Elastic Container Registry)
- Docker 이미지 빌드 및 푸시
- 이미지 태깅 전략
- 취약점 스캔

### ElastiCache
- Redis 클러스터 구성
- 연결 및 인증 설정
- 모니터링 및 알림

### 네트워킹
- **VPC** 설계 (퍼블릭/프라이빗 서브넷)
- **보안 그룹** 인바운드/아웃바운드 규칙
- **IAM** 역할 및 정책 (IRSA)

### 모니터링
- **CloudWatch** 로그 그룹
- **CloudWatch** 메트릭 및 알람
- 비용 모니터링 (Cost Explorer)

---

## 3. DevOps/SRE 실무

### CI/CD 파이프라인

```yaml
# GitHub Actions 워크플로우 경험
- 변경 감지 (paths-filter)
- Docker 이미지 빌드/푸시
- EKS 배포 자동화
- Slack 알림 연동
```

### Infrastructure as Code
- **kubectl/kustomize** - 기본 K8s 관리
- **Terraform** (향후) - 인프라 코드화
- **GitOps** 패턴 이해

### 장애 대응
- 로그 분석 및 트러블슈팅
- 파드 재시작, 롤백 실행
- 근본 원인 분석 (RCA)

### 모니터링/알림 설계
- 핵심 메트릭 정의
- 알림 임계값 설정
- 에스컬레이션 정책

---

## 4. 분산 시스템 이해

### 고가용성 (HA)
- Active-Standby 아키텍처
- Failover 메커니즘
- 다중 가용영역 (Multi-AZ) 배포

### 상태 관리
- **Redis** 기반 공유 상태
- 캐싱 전략
- TTL 및 만료 정책

### 서비스 간 통신
- **Redis Pub/Sub** 메시징
- 이벤트 기반 아키텍처
- 느슨한 결합 (Loose Coupling)

### 동시성 제어
- 분산 락 (Distributed Lock)
- Leader Election
- 경쟁 조건 (Race Condition) 방지

---

## 5. 실전 트레이딩 시스템

### 실시간 데이터 처리
- **WebSocket** 연결 관리
- 가격 스트림 처리
- 지연 시간 최소화

### ML 모델 서빙
- 모델 로딩 및 추론
- 피처 엔지니어링
- 백테스트 vs 실거래 검증

### 주문 실행
- 거래소 API 연동
- 주문 상태 관리
- 에러 핸들링 및 재시도

### 리스크 관리
- 손절/익절 로직
- 트레일링 스탑
- 포지션 크기 관리

---

## 6. 이력서/포트폴리오 활용

### 프로젝트 요약

```
"Kubernetes(EKS) 기반 암호화폐 자동매매 시스템 설계/운영"

- 실시간 트레이딩 봇 아키텍처 설계 및 구현
- EKS 클러스터 구축 및 HA(고가용성) 구성
- Leader Election 기반 Active-Standby 무중단 운영
- GitHub Actions CI/CD 파이프라인 구축
- ML 모델 기반 매매 전략 구현 및 서빙
- WebSocket 실시간 가격 모니터링 시스템
- Redis Pub/Sub 기반 마이크로서비스 아키텍처
```

### 기술 스택 정리

```
Infrastructure: AWS EKS, ECR, ElastiCache, CloudWatch
Container: Docker, Kubernetes
CI/CD: GitHub Actions
Language: Python, TypeScript, Go
Database: Redis
Monitoring: CloudWatch, Slack Webhook
Trading: Upbit API, WebSocket, ML (XGBoost)
```

### 어필 포인트

1. **실제 돈이 오가는 시스템** - 장난감 프로젝트가 아님
2. **End-to-End 경험** - 설계부터 운영까지
3. **다양한 기술 스택** - 풀스택 + 인프라
4. **문제 해결 경험** - 장애 대응, 최적화

---

## 7. 향후 학습 로드맵

### 단기 (1-3개월)
- [ ] Prometheus + Grafana 모니터링 구축
- [ ] Terraform으로 인프라 코드화
- [ ] 로그 중앙화 (ELK 또는 Loki)

### 중기 (3-6개월)
- [ ] ArgoCD GitOps 도입
- [ ] Helm 차트 작성
- [ ] 카오스 엔지니어링 (장애 주입 테스트)

### 장기 (6-12개월)
- [ ] 멀티 클러스터/멀티 리전
- [ ] 서비스 메시 (Istio)
- [ ] 고급 ML Ops (MLflow, Kubeflow)

---

## 참고 자료

### Kubernetes
- [Kubernetes 공식 문서](https://kubernetes.io/docs/)
- [EKS 베스트 프랙티스](https://aws.github.io/aws-eks-best-practices/)

### DevOps
- [The DevOps Handbook](https://itrevolution.com/the-devops-handbook/)
- [SRE Book (Google)](https://sre.google/sre-book/table-of-contents/)

### 트레이딩 시스템
- [Upbit API 문서](https://docs.upbit.com/)
- [시스템 트레이딩 설계 패턴](https://www.amazon.com/Trading-Exchanges-Market-Microstructure-Practitioners/dp/0195144708)
