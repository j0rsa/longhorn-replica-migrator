"""File-system and pod-manifest operations for the migration workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

_LARGE_FILE_BYTES = 256 * 1024 * 1024   # 256 MiB — log individually
_MOVE_CHUNK_BYTES = 512 * 1024 * 1024   # 512 MiB chunks for space-safe large-file moves


def _chunked_move_file(src: Path, dst: Path, size: int, log: Callable[[str], None]) -> None:
    """Move *src* to *dst* in reverse-order 512 MiB chunks.

    After each chunk is safely written and fsynced to the destination,
    the corresponding tail of the source file is truncated via ftruncate.
    This keeps peak extra disk usage at one chunk size rather than the
    full file size.
    """
    with open(src, "rb+") as sf, open(dst, "wb") as df:
        remaining = size
        while remaining > 0:
            chunk_size = min(_MOVE_CHUNK_BYTES, remaining)
            offset = remaining - chunk_size
            sf.seek(offset)
            data = sf.read(chunk_size)
            df.seek(offset)
            df.write(data)
            df.flush()
            os.fsync(df.fileno())
            os.ftruncate(sf.fileno(), offset)
            remaining = offset
            done_mib = (size - remaining) // 1_048_576
            total_mib = size // 1_048_576
            pct = done_mib * 100 // (total_mib or 1)
            log(f"    {done_mib}/{total_mib} MiB ({pct}%)...")
    shutil.copystat(src, dst)
    src.unlink()


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
            if st.st_size >= _LARGE_FILE_BYTES:
                log(f"    copying large file {item.name} ({st.st_size // 1024 // 1024} MiB)...")
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


def deflate_source_imgs(
    replica_path: Path,
    src_device: Path,
    fs_type: str | None,
    log: Callable[[str], None],
) -> None:
    """Free host disk space used by a Longhorn replica after files have been moved out.

    The source block device must be **unmounted** before calling this.

    Steps:
    1. ``zerofree`` (ext4) or a no-op warning (other FS) — writes actual zero
       bytes into the free blocks of the block device, which flows through the
       Longhorn engine into the backing ``.img`` sparse files.
    2. ``fallocate --dig-holes`` on every ``.img`` file — the kernel detects
       zero regions and replaces them with sparse holes, which the host OS
       then releases as free disk blocks.
    """
    if fs_type == "ext4":
        log("    [deflate] zerofree: zeroing free blocks on source device (may take a while)...")
        result = subprocess.run(["zerofree", str(src_device)], capture_output=True, text=True)
        out = (result.stdout + result.stderr).strip()
        if out:
            log(f"    {out}")
        if result.returncode != 0:
            log("    [yellow][deflate] zerofree failed — skipping hole punching[/yellow]")
            return
    else:
        log(f"    [yellow][deflate] No zerofree for {fs_type} — hole punching may recover fewer blocks[/yellow]")

    imgs = sorted(replica_path.glob("*.img"))
    log(f"    [deflate] fallocate --dig-holes on {len(imgs)} .img file(s)...")
    for img in imgs:
        before = img.stat().st_blocks * 512
        result = subprocess.run(["fallocate", "--dig-holes", str(img)], capture_output=True, text=True)
        out = (result.stdout + result.stderr).strip()
        after = img.stat().st_blocks * 512
        freed = (before - after) // 1_048_576
        status = f"freed {freed} MiB" if freed > 0 else "no change"
        if out:
            log(f"    {img.name}: {out} ({status})")
        else:
            log(f"    {img.name}: {status}")


def move_tree(
    src: Path,
    dst: Path,
    log: Callable[[str], None],
    total: int = 0,
    deflate_every_bytes: int = 0,
    deflate_cb: Callable[[], None] | None = None,
) -> None:
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
    bytes_since_deflate = 0
    # Pre-collect so we can resume the same list after unmount/remount cycles.
    all_items = sorted(src.rglob("*"), key=lambda p: (len(p.parts), p))
    for item in all_items:
        # Skip items already moved in a previous pass (e.g. after remount).
        if not item.is_symlink() and not item.exists():
            continue
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
            elif st.st_size >= _LARGE_FILE_BYTES:
                log(f"    moving large file {item.name} ({st.st_size // 1024 // 1024} MiB)...")
                _chunked_move_file(item, dest_file, st.st_size, log)
                if st.st_nlink > 1:
                    inode_map[st.st_ino] = dest_file
            else:
                shutil.move(str(item), str(dest_file))
                if st.st_nlink > 1:
                    inode_map[st.st_ino] = dest_file
            count += 1
            bytes_since_deflate += st.st_size
        except OSError as exc:
            msg = f"{item}: {exc}"
            log(f"    [red][!] failed {msg}[/red]")
            errors.append(msg)
        if count % 50 == 0 and count > 0:
            pct = f" ({count * 100 // total}%)" if total else ""
            log(f"    moved {count}/{total if total else '?'}{pct} files...")
        if deflate_cb and deflate_every_bytes > 0 and bytes_since_deflate >= deflate_every_bytes:
            log(f"    [deflate] {bytes_since_deflate // 1_073_741_824} GiB moved — triggering source deflation...")
            deflate_cb()
            bytes_since_deflate = 0
    log(f"    move_tree: {count} files moved, {len(errors)} errors")
    if errors:
        raise RuntimeError(f"{len(errors)} file(s) failed to move")
