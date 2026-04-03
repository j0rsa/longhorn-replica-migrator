"""Thin wrappers around kubectl for pod lifecycle management."""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

DEFAULT_IMAGE: str = "longhornio/longhorn-engine:v1.11.0"
POD_NAME: str = "longhorn-replica-recovery"
POD_NAMESPACE: str = "default"
_POLL_INTERVAL: int = 2


def get_hostname() -> str:
    """Return the current machine's hostname.

    Returns:
        The FQDN or short hostname as reported by the OS.
    """
    return socket.gethostname()


def run_cmd(*args: str) -> tuple[int, str, str]:
    """Execute a subprocess and capture its output.

    Args:
        *args: Command and arguments to run.

    Returns:
        A 3-tuple of (returncode, stdout, stderr).
    """
    result = subprocess.run(list(args), capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def apply_yaml(yaml_str: str) -> tuple[int, str]:
    """Apply a Kubernetes manifest via ``kubectl apply -f -``.

    Args:
        yaml_str: The YAML manifest as a string.

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr output).
    """
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str,
        capture_output=True,
        text=True,
    )
    combined = (result.stdout + result.stderr).strip()
    return result.returncode, combined


def delete_pod(
    name: str = POD_NAME,
    namespace: str = POD_NAMESPACE,
) -> tuple[int, str]:
    """Delete a Kubernetes pod, ignoring not-found errors.

    Args:
        name: Pod name to delete.
        namespace: Namespace containing the pod.

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr output).
    """
    rc, out, err = run_cmd(
        "kubectl",
        "delete",
        "pod",
        name,
        "-n",
        namespace,
        "--ignore-not-found",
    )
    return rc, (out + " " + err).strip()


def pod_phase(
    name: str = POD_NAME,
    namespace: str = POD_NAMESPACE,
) -> str | None:
    """Return the current phase of a pod, or None if the pod does not exist.

    Args:
        name: Pod name to query.
        namespace: Namespace containing the pod.

    Returns:
        Phase string (e.g. ``"Running"``, ``"Pending"``) or ``None`` if not found.
    """
    rc, out, _ = run_cmd(
        "kubectl",
        "get",
        "pod",
        name,
        "-n",
        namespace,
        "-o",
        "jsonpath={.status.phase}",
    )
    if rc != 0 or not out:
        return None
    return out


def wait_pod_running(
    name: str = POD_NAME,
    namespace: str = POD_NAMESPACE,
    timeout: int = 120,
) -> bool:
    """Poll until the pod reaches the ``Running`` phase or a timeout expires.

    Args:
        name: Pod name to wait for.
        namespace: Namespace containing the pod.
        timeout: Maximum seconds to wait before giving up.

    Returns:
        ``True`` if the pod reached ``Running`` within the timeout, else ``False``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phase = pod_phase(name, namespace)
        if phase == "Running":
            return True
        time.sleep(_POLL_INTERVAL)
    return False


def wait_device(device: Path, timeout: int = 60) -> bool:
    """Poll until a block device path exists on the filesystem.

    Args:
        device: Path to the expected device (e.g. ``/dev/longhorn/my-vol``).
        timeout: Maximum seconds to wait before giving up.

    Returns:
        ``True`` if the device appeared within the timeout, else ``False``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if device.exists():
            return True
        time.sleep(_POLL_INTERVAL)
    return False
