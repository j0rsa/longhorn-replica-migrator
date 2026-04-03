"""File-system and pod-manifest operations for the migration workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path


def build_pod_yaml(
    replica_path: Path,
    volume_name: str,
    volume_size_bytes: int,
    hostname: str,
    image: str,
) -> str:
    """Render the recovery-pod manifest as a YAML string.

    The pod mounts the replica directory at ``/volume`` and runs
    ``launch-simple-longhorn`` to expose the replica data as a block device
    at ``/dev/longhorn/<volume_name>`` on the host.

    Args:
        replica_path: Absolute path to the replica directory on the host.
        volume_name: Name passed to ``launch-simple-longhorn`` (becomes the
            block-device name under ``/dev/longhorn/``).
        volume_size_bytes: Volume size in bytes passed to
            ``launch-simple-longhorn``.
        hostname: Kubernetes node hostname used as a ``nodeSelector``.
        image: Container image for the recovery container.

    Returns:
        A fully-rendered Kubernetes Pod manifest as a YAML string.
    """
    return f"""\
apiVersion: v1
kind: Pod
metadata:
  name: longhorn-replica-recovery
  namespace: default
spec:
  hostPID: true
  hostNetwork: true
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: "{hostname}"
  containers:
  - name: recovery
    image: {image}
    securityContext:
      privileged: true
    command: ["launch-simple-longhorn"]
    args: ["{volume_name}", "{volume_size_bytes}"]
    volumeMounts:
    - name: dev
      mountPath: /host/dev
    - name: proc
      mountPath: /host/proc
    - name: data
      mountPath: /volume
  volumes:
  - name: dev
    hostPath:
      path: /dev
  - name: proc
    hostPath:
      path: /proc
  - name: data
    hostPath:
      path: {replica_path}
"""


def detect_fs_type(device: Path) -> str | None:
    """Return the filesystem type on *device*, or ``None`` if unformatted/unknown.

    Args:
        device: Block device to probe.

    Returns:
        Filesystem type string (e.g. ``"ext4"``, ``"xfs"``) or ``None``.
    """
    result = subprocess.run(
        ["blkid", "-o", "value", "-s", "TYPE", str(device)],
        capture_output=True,
        text=True,
    )
    fs_type = result.stdout.strip()
    return fs_type if fs_type else None


def format_device(device: Path, fs_type: str) -> tuple[int, str]:
    """Format *device* with the given filesystem type.

    Args:
        device: Block device to format.
        fs_type: Filesystem type, e.g. ``"ext4"`` or ``"xfs"``.

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr).
    """
    mkfs_cmd = f"mkfs.{fs_type}"
    result = subprocess.run(
        [mkfs_cmd, "-f", str(device)] if fs_type == "xfs" else [mkfs_cmd, str(device)],
        capture_output=True,
        text=True,
    )
    combined = (result.stdout + result.stderr).strip()
    return result.returncode, combined


def mount_device(device: Path, mountpoint: Path) -> tuple[int, str]:
    """Mount a block device at the given mountpoint.

    Args:
        device: Path to the block device to mount.
        mountpoint: Directory at which to mount the device.

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr).
    """
    result = subprocess.run(
        ["mount", str(device), str(mountpoint)],
        capture_output=True,
        text=True,
    )
    combined = (result.stdout + result.stderr).strip()
    return result.returncode, combined


def unmount(mountpoint: Path) -> tuple[int, str]:
    """Unmount a previously mounted filesystem.

    Args:
        mountpoint: Directory that was used as the mount target.

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr).
    """
    result = subprocess.run(
        ["umount", str(mountpoint)],
        capture_output=True,
        text=True,
    )
    combined = (result.stdout + result.stderr).strip()
    return result.returncode, combined


def count_files(path: Path) -> int:
    """Count transferable entries (files + symlinks) under *path*."""
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_dir():
            total += 1
    return total


def copy_tree(src: Path, dst: Path, log: Callable[[str], None], total: int = 0) -> None:
    """Recursively copy all files from *src* to *dst*, logging progress.

    Symlinks are recreated as symlinks (not followed).  Per-file errors are
    logged and collected; a RuntimeError is raised at the end if any occurred.

    Args:
        src: Source directory to copy from.
        dst: Destination directory to copy into (must exist).
        log: Callable that receives progress strings.

    Raises:
        RuntimeError: If one or more files could not be copied.
    """
    count = 0
    errors: list[str] = []
    inode_map: dict[int, Path] = {}  # source inode → first dest copy
    for item in src.rglob("*"):
        if item.is_symlink():
            relative = item.relative_to(src)
            dest_link = dst / relative
            dest_link.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest_link.symlink_to(os.readlink(item))
                count += 1
            except OSError as exc:
                msg = f"symlink {item} → {os.readlink(item)}: {exc}"
                log(f"    [yellow][!] skipped {msg}[/yellow]")
                errors.append(msg)
            continue
        if item.is_dir():
            relative = item.relative_to(src)
            (dst / relative).mkdir(parents=True, exist_ok=True)
            continue
        relative = item.relative_to(src)
        dest_file = dst / relative
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            st = item.stat()
            if st.st_nlink > 1 and st.st_ino in inode_map:
                os.link(inode_map[st.st_ino], dest_file)
            else:
                shutil.copy2(item, dest_file)
                if st.st_nlink > 1:
                    inode_map[st.st_ino] = dest_file
            count += 1
        except OSError as exc:
            msg = f"{item}: {exc}"
            log(f"    [red][!] failed {msg}[/red]")
            errors.append(msg)
        if count % 50 == 0 and count > 0:
            pct = f" ({count * 100 // total}%)" if total else ""
            log(f"    copied {count}/{total if total else '?'}{pct} files...")
    log(f"    copy_tree: {count} files copied, {len(errors)} errors")
    if errors:
        raise RuntimeError(f"{len(errors)} file(s) failed to copy")


def move_tree(src: Path, dst: Path, log: Callable[[str], None], total: int = 0) -> None:
    """Recursively move all files from *src* to *dst*, logging progress.

    Symlinks are recreated as symlinks on the destination then removed from
    the source.  Per-file errors are logged and collected; a RuntimeError is
    raised at the end if any occurred.

    Args:
        src: Source directory to move from.
        dst: Destination directory to move into (must exist).
        log: Callable that receives progress strings.

    Raises:
        RuntimeError: If one or more files could not be moved.
    """
    count = 0
    errors: list[str] = []
    inode_map: dict[int, Path] = {}  # source inode → first dest copy
    for item in sorted(src.rglob("*"), key=lambda p: (len(p.parts), p)):
        if item.is_symlink():
            relative = item.relative_to(src)
            dest_link = dst / relative
            dest_link.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest_link.symlink_to(os.readlink(item))
                item.unlink()
                count += 1
            except OSError as exc:
                msg = f"symlink {item} → {os.readlink(item)}: {exc}"
                log(f"    [yellow][!] skipped {msg}[/yellow]")
                errors.append(msg)
            continue
        if item.is_dir():
            relative = item.relative_to(src)
            (dst / relative).mkdir(parents=True, exist_ok=True)
            continue
        relative = item.relative_to(src)
        dest_file = dst / relative
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            st = item.stat()
            if st.st_nlink > 1 and st.st_ino in inode_map:
                os.link(inode_map[st.st_ino], dest_file)
                item.unlink()
            else:
                shutil.move(str(item), str(dest_file))
                if st.st_nlink > 1:
                    inode_map[st.st_ino] = dest_file
            count += 1
        except OSError as exc:
            msg = f"{item}: {exc}"
            log(f"    [red][!] failed {msg}[/red]")
            errors.append(msg)
        if count % 50 == 0 and count > 0:
            pct = f" ({count * 100 // total}%)" if total else ""
            log(f"    moved {count}/{total if total else '?'}{pct} files...")
    log(f"    move_tree: {count} files moved, {len(errors)} errors")
    if errors:
        raise RuntimeError(f"{len(errors)} file(s) failed to move")
