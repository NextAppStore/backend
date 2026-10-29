"""Read-only Kubernetes client layer for the lab (Kubernetes-App) endpoints.

Mirrors ``openstack_client.py``'s shape, for the one synchronous use left in
the API process: listing student pod status and downloading a team's
kubeconfig. Mutating actions (reassign/reset/add/remove-student) run as
worker jobs instead (``app.services.jobs.JobPayload``'s ``kubeconfig`` field)
and build their own client the same way, in the worker repository.

Two responsibilities:

1. Open one ``kubernetes`` ``ApiClient`` per request from a team's
   kubeconfig as provisioned, exposed as a context manager so endpoints can
   close it cleanly. The ``server:`` URL from Terraform
   (``Kubernetes-App/terraform/main.tf`` ``api_endpoint``) is the bracketed
   IPv6 literal and is used unpatched — IPv4 on this OpenStack network is
   unreliable/unreachable from the backend and worker, while IPv6 is not.
2. List student pods/services/deployments in namespace ``lab``, matching
   ``labctl cmd_list`` exactly.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from kubernetes import client as k8s
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

# Matches labctl's own timeout for a single API call during ``list``; lab
# actions that roll out a Deployment use a much longer worker-side timeout.
_REQUEST_TIMEOUT_SECONDS = 10

LAB_NAMESPACE = "lab"
_STUDENT_LABEL_SELECTOR = "role=student"


@contextmanager
def team_api_client(kubeconfig_yaml: str) -> Iterator[k8s.ApiClient]:
    """Yield a ``kubernetes`` ``ApiClient`` for one team's cluster.

    - 502 for a transient/unreachable cluster error (500s are reserved for
      backend bugs); the message is never the raw SDK exception text, which
      can echo request details.
    """
    api_client: k8s.ApiClient | None = None
    # The ``kubernetes`` client only loads a kubeconfig from a file path or
    # the default locations, not from a string — write it to a private
    # tempfile for the duration of this connection.
    with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig", delete=False) as f:
        f.write(kubeconfig_yaml)
        kubeconfig_path = f.name
    try:
        configuration = k8s.Configuration()
        k8s_config.load_kube_config(config_file=kubeconfig_path, client_configuration=configuration)
        configuration.retries = 0
        api_client = k8s.ApiClient(configuration)
        yield api_client
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK raises many types
        logger.warning("Kubernetes cluster unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "lab_cluster_unavailable"},
        )
    finally:
        if api_client is not None:
            with suppress(Exception):
                api_client.close()
        with suppress(OSError):
            import os

            os.unlink(kubeconfig_path)


@dataclass
class StudentPodStatus:
    username: str
    port: int | None
    generation: int | None
    pod_name: str | None
    pod_phase: str | None
    pod_age: datetime | None
    ready: bool


def list_students(api_client: k8s.ApiClient) -> list[StudentPodStatus]:
    """All students with status, port and generation — mirrors ``labctl cmd_list``."""
    apps_api = k8s.AppsV1Api(api_client)
    core_api = k8s.CoreV1Api(api_client)

    deployments = apps_api.list_namespaced_deployment(
        LAB_NAMESPACE,
        label_selector=_STUDENT_LABEL_SELECTOR,
        _request_timeout=_REQUEST_TIMEOUT_SECONDS,
    ).items
    services = core_api.list_namespaced_service(
        LAB_NAMESPACE,
        label_selector=_STUDENT_LABEL_SELECTOR,
        _request_timeout=_REQUEST_TIMEOUT_SECONDS,
    ).items
    pods = core_api.list_namespaced_pod(
        LAB_NAMESPACE,
        label_selector=_STUDENT_LABEL_SELECTOR,
        _request_timeout=_REQUEST_TIMEOUT_SECONDS,
    ).items

    ports_by_student = {
        svc.metadata.labels.get("student"): svc.spec.ports[0].node_port
        for svc in services
        if svc.spec.ports and svc.metadata.labels
    }
    pods_by_student: dict[str, k8s.V1Pod] = {}
    for pod in pods:
        username = (pod.metadata.labels or {}).get("student")
        if username:
            pods_by_student[username] = pod

    results: list[StudentPodStatus] = []
    for dep in deployments:
        username = (dep.metadata.labels or {}).get("student")
        if not username:
            continue
        generation_raw = (dep.spec.template.metadata.annotations or {}).get("lab-generation")
        pod = pods_by_student.get(username)
        results.append(
            StudentPodStatus(
                username=username,
                port=ports_by_student.get(username),
                generation=int(generation_raw)
                if generation_raw and generation_raw.isdigit()
                else None,
                pod_name=pod.metadata.name if pod else None,
                pod_phase=pod.status.phase if pod else None,
                pod_age=pod.metadata.creation_timestamp if pod else None,
                ready=bool(dep.status.ready_replicas),
            )
        )
    results.sort(key=lambda s: s.username)
    return results
