"""K8s Lease 기반 Leader Election"""

import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from ..utils import get_logger

logger = get_logger(__name__)

# Leader Election 상수
DEFAULT_LEASE_DURATION = 15  # 초
DEFAULT_RENEW_DEADLINE = 10  # 초
DEFAULT_RETRY_PERIOD = 2  # 초


class LeaderElector:
    """
    K8s Lease 기반 Leader Election 관리자

    - Leader: TradingLoop 실행
    - Standby: Lease 획득 대기 (리소스 최소화)
    """

    def __init__(
        self,
        lease_name: str = "trading-engine-lock",
        namespace: Optional[str] = None,
        lease_duration: int = DEFAULT_LEASE_DURATION,
        renew_deadline: int = DEFAULT_RENEW_DEADLINE,
        retry_period: int = DEFAULT_RETRY_PERIOD,
        on_started_leading: Optional[Callable[[], None]] = None,
        on_stopped_leading: Optional[Callable[[], None]] = None,
        on_new_leader: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            lease_name: Lease 리소스 이름
            namespace: K8s 네임스페이스 (None이면 Pod 네임스페이스 사용)
            lease_duration: Lease 유효 기간 (초)
            renew_deadline: Leader의 Lease 갱신 주기 (초)
            retry_period: Standby의 재시도 주기 (초)
            on_started_leading: Leader가 되었을 때 콜백
            on_stopped_leading: Leader에서 해제되었을 때 콜백
            on_new_leader: 새 Leader 감지 시 콜백 (leader_id 전달)
        """
        self.lease_name = lease_name
        self.namespace = namespace or self._get_namespace()
        self.lease_duration = lease_duration
        self.renew_deadline = renew_deadline
        self.retry_period = retry_period

        # 콜백
        self.on_started_leading = on_started_leading
        self.on_stopped_leading = on_stopped_leading
        self.on_new_leader = on_new_leader

        # Pod Identity (hostname = pod name in K8s)
        self.identity = os.getenv("HOSTNAME", f"local-{os.getpid()}")

        # 상태
        self._is_leader = False
        self._running = False
        self._leader_id: Optional[str] = None

        # K8s API 클라이언트
        self._api: Optional[client.CoordinationV1Api] = None

        # 스레드
        self._election_thread: Optional[threading.Thread] = None

        # 종료 이벤트
        self._stop_event = threading.Event()

        logger.info(
            "leader_elector_initialized",
            lease_name=self.lease_name,
            namespace=self.namespace,
            identity=self.identity,
            lease_duration=self.lease_duration,
        )

    def _get_namespace(self) -> str:
        """Pod 네임스페이스 조회 (K8s 내부) 또는 기본값"""
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "coinbot"  # 기본 네임스페이스

    def _init_k8s_client(self) -> None:
        """K8s API 클라이언트 초기화"""
        try:
            # 클러스터 내부에서 실행 (Pod 내부)
            config.load_incluster_config()
            logger.info("k8s_config_loaded", mode="incluster")
        except config.ConfigException:
            # 로컬 개발 환경
            config.load_kube_config()
            logger.info("k8s_config_loaded", mode="kubeconfig")

        self._api = client.CoordinationV1Api()

    def _get_lease(self) -> Optional[client.V1Lease]:
        """현재 Lease 조회"""
        try:
            return self._api.read_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _create_lease(self) -> client.V1Lease:
        """새 Lease 생성 (첫 Leader)"""
        now = datetime.now(timezone.utc)
        lease = client.V1Lease(
            metadata=client.V1ObjectMeta(
                name=self.lease_name,
                namespace=self.namespace,
            ),
            spec=client.V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration,
                acquire_time=now,
                renew_time=now,
            ),
        )

        return self._api.create_namespaced_lease(
            namespace=self.namespace,
            body=lease,
        )

    def _update_lease(self, lease: client.V1Lease) -> client.V1Lease:
        """Lease 갱신 (Leader 유지 또는 인수)"""
        now = datetime.now(timezone.utc)

        # 기존 holder와 다르면 acquire_time 업데이트
        if lease.spec.holder_identity != self.identity:
            lease.spec.acquire_time = now

        lease.spec.holder_identity = self.identity
        lease.spec.renew_time = now
        lease.spec.lease_duration_seconds = self.lease_duration

        return self._api.replace_namespaced_lease(
            name=self.lease_name,
            namespace=self.namespace,
            body=lease,
        )

    def _is_lease_expired(self, lease: client.V1Lease) -> bool:
        """Lease 만료 여부 확인"""
        if not lease.spec.renew_time:
            return True

        renew_time = lease.spec.renew_time
        if isinstance(renew_time, str):
            renew_time = datetime.fromisoformat(renew_time.replace("Z", "+00:00"))

        duration = lease.spec.lease_duration_seconds or self.lease_duration
        now = datetime.now(timezone.utc)

        return (now - renew_time).total_seconds() > duration

    def _try_acquire_or_renew(self) -> bool:
        """Lease 획득 또는 갱신 시도"""
        try:
            lease = self._get_lease()

            if lease is None:
                # Lease가 없으면 생성
                self._create_lease()
                logger.info("lease_created", identity=self.identity)
                return True

            # 내가 이미 Leader인 경우 - 갱신
            if lease.spec.holder_identity == self.identity:
                self._update_lease(lease)
                return True

            # 다른 Leader가 있는 경우
            if not self._is_lease_expired(lease):
                # Lease가 유효하면 대기
                if self._leader_id != lease.spec.holder_identity:
                    self._leader_id = lease.spec.holder_identity
                    logger.info(
                        "current_leader_detected",
                        leader=self._leader_id,
                        identity=self.identity,
                    )
                    if self.on_new_leader:
                        self.on_new_leader(self._leader_id)
                return False

            # Lease가 만료되었으면 인수
            self._update_lease(lease)
            logger.info(
                "lease_acquired",
                identity=self.identity,
                previous_holder=lease.spec.holder_identity,
            )
            return True

        except ApiException as e:
            if e.status == 409:
                # Conflict - 다른 Pod가 먼저 획득
                logger.debug("lease_conflict", identity=self.identity)
                return False
            logger.error("lease_error", error=str(e), status=e.status)
            return False
        except Exception as e:
            logger.error("lease_unexpected_error", error=str(e))
            return False

    def _election_loop(self) -> None:
        """Leader Election 메인 루프"""
        while self._running and not self._stop_event.is_set():
            try:
                acquired = self._try_acquire_or_renew()

                if acquired and not self._is_leader:
                    # Standby → Leader 전환
                    self._is_leader = True
                    logger.info("became_leader", identity=self.identity)
                    if self.on_started_leading:
                        self.on_started_leading()

                elif not acquired and self._is_leader:
                    # Leader → Standby 전환 (Leader 상실)
                    self._is_leader = False
                    logger.warning("lost_leadership", identity=self.identity)
                    if self.on_stopped_leading:
                        self.on_stopped_leading()

                # 대기 시간 결정
                if self._is_leader:
                    # Leader: 갱신 주기
                    self._stop_event.wait(timeout=self.renew_deadline)
                else:
                    # Standby: 재시도 주기
                    self._stop_event.wait(timeout=self.retry_period)

            except Exception as e:
                logger.error("election_loop_error", error=str(e))
                self._stop_event.wait(timeout=self.retry_period)

    def start(self) -> None:
        """Leader Election 시작"""
        self._init_k8s_client()
        self._running = True
        self._stop_event.clear()

        # Election 루프 시작
        self._election_thread = threading.Thread(
            target=self._election_loop,
            daemon=True,
        )
        self._election_thread.start()

        logger.info("leader_election_started", identity=self.identity)

    def stop(self) -> None:
        """Leader Election 중지"""
        self._running = False
        self._stop_event.set()

        if self._is_leader:
            self._is_leader = False
            if self.on_stopped_leading:
                self.on_stopped_leading()

        # 스레드 정리
        if self._election_thread and self._election_thread.is_alive():
            self._election_thread.join(timeout=5)

        logger.info("leader_election_stopped", identity=self.identity)

    @property
    def is_leader(self) -> bool:
        """현재 Leader 여부"""
        return self._is_leader

    def run_until_stopped(self) -> None:
        """Leader Election 실행 (블로킹, SIGTERM/SIGINT 대기)"""
        stop_event = threading.Event()

        def signal_handler(signum, frame):
            signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
            logger.info("signal_received", signal=signal_name)
            stop_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        self.start()

        try:
            stop_event.wait()
        finally:
            self.stop()
