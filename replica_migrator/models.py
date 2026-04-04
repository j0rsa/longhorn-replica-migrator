from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TransferMode(Enum):
    COPY = "copy"
    MOVE = "move"
    MOVE_DEFLATE = "move_deflate"


@dataclass(frozen=True)
class ReplicaRow:
    """One Longhorn replica directory under the replicas root."""

    path: Path
    dir_name: str
    size_bytes: int | None
    volume_name: str
    head: str
    parent: str
    meta_note: str


@dataclass(frozen=True)
class LonghornDisk:
    """Entry under /dev/longhorn/."""

    path: Path
    target: str | None
    mode_bits: str


@dataclass(frozen=True)
class MigrationConfig:
    """All parameters needed to execute a single migration run.

    Captures both user selections (replica source, target device) and
    operational settings (image, transfer mode, post-migration cleanup).
    Immutable after construction to prevent accidental mutation during
    the migration workflow.
    """

    replica: ReplicaRow
    """Orphaned replica directory that provides the source data."""

    disk: LonghornDisk
    """Pre-provisioned empty Longhorn volume attached to this node (TARGET)."""

    hostname: str
    """Kubernetes node hostname used in the recovery-pod nodeSelector."""

    image: str
    """Container image for the longhorn-engine recovery container."""

    transfer_mode: TransferMode
    """Copy, Move, or Move+Deflate."""

    delete_replica: bool
    """If ``True``, delete the source replica directory after transfer."""
