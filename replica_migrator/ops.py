"""File-system and pod-manifest operations for the migration workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _stream_cmd(cmd: list[str], log: Callable[[str], None]) -> int:
    """Run *cmd*, streaming its stdout+stderr to *log* in real time.

    Treats both ``\\n`` and ``\\r`` as line separators so that tools like
    ``zerofree -v`` that redraw a progress line via carriage-return emit a
    new log entry for each update rather than going silent.

    Returns the process exit code.
    """
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    ) as proc:
        buf = b""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            while True:
                cr = buf.find(b"\r")
                nl = buf.find(b"\n")
                if cr == -1 and nl == -1:
                    break
                pos = min(x for x in (cr, nl) if x != -1)
                line = buf[:pos].decode(errors="replace").strip()
                buf = buf[pos + 1 :]
                if line:
                    log(f"    {line}")
        if buf:
            line = buf.decode(errors="replace").strip()
            if line:
                log(f"    {line}")
    return proc.returncode or 0

_LARGE_FILE_BYTES = 256 * 1024 * 1024   # 256 MiB — log individually
_STATE_VERSION = 1


def _load_inode_state(state_file: Path, dst: Path) -> dict[int, Path]:
    """Load a persisted source-inode → dest-path map from *state_file*.

    Entries whose destination file no longer exists are silently dropped.
    Returns an empty dict if the file is missing, unreadable, or stale.
    """
    if not state_file.exists():
        return {}
    try:
        data: object = json.loads(state_file.read_text())
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return {}
        result: dict[int, Path] = {}
        for ino_str, rel in data.get("inode_map", {}).items():
            dest = dst / rel
            if dest.exists():
                result[int(ino_str)] = dest
        return result
    except Exception:
        return {}


def _save_inode_state(state_file: Path, inode_map: dict[int, Path], dst: Path) -> None:
    """Atomically persist *inode_map* to *state_file*."""
    payload = {
        "version": _STATE_VERSION,
        "inode_map": {str(ino): str(p.relative_to(dst)) for ino, p in inode_map.items()},
    }
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(state_file)



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


def sync_fs(mountpoint: Path) -> None:
    """Flush and commit the journal for the filesystem at *mountpoint*.

    Runs ``sync -f <mountpoint>`` which flushes only that filesystem's dirty
    pages and commits all pending journal transactions.  Call this before
    unmounting to guarantee a clean on-disk state so that a subsequent
    remount for fstrim finds no journal replay needed.
    """
    subprocess.run(["sync", "-f", str(mountpoint)], check=False)


def mount_device(device: Path, mountpoint: Path, extra_opts: str = "") -> tuple[int, str]:
    """Mount a block device at the given mountpoint.

    Args:
        device: Path to the block device to mount.
        mountpoint: Directory at which to mount the device.
        extra_opts: Optional comma-separated mount options (e.g. ``"discard"``).

    Returns:
        A 2-tuple of (returncode, combined stdout+stderr).
    """
    cmd = ["mount"]
    if extra_opts:
        cmd += ["-o", extra_opts]
    cmd += [str(device), str(mountpoint)]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def copy_tree(src: Path, dst: Path, log: Callable[[str], None], total: int = 0,
              progress_cb: Callable[[int, int], None] | None = None) -> None:
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
            if progress_cb:
                progress_cb(count, total)
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
    fs_type: str | None,  # reserved for future per-FS logic
    log: Callable[[str], None],
) -> None:
    """Free host disk space used by a Longhorn replica after files have been moved out.

    The source block device must be **unmounted** before calling this.

    Strategy
    --------
    **Do NOT use zerofree.**  The Longhorn ``.img`` files are sparse — filesystem
    free blocks are already stored as sparse holes (zero host-disk cost).  zerofree
    writes zeros to those holes, materialising them into real bytes and *increasing*
    disk usage before fallocate can punch them back.

    Instead:

    1. Mount the device briefly with ``-o discard`` and run ``fstrim``.  This sends
       DISCARD/UNMAP commands to the Longhorn block device for every free block.
       The engine punches holes in the ``.img`` files *directly*, with no temporary
       disk-space spike.

    2. ``fallocate --dig-holes`` on every ``.img`` file to catch any zero regions
       that fstrim left behind (e.g. blocks the engine zeroed rather than holed).
    """
    import tempfile

    # Show device size for context.
    size_result = subprocess.run(
        ["blockdev", "--getsize64", str(src_device)], capture_output=True, text=True
    )
    if size_result.returncode == 0:
        dev_gib = int(size_result.stdout.strip()) / 1024 ** 3
        log(f"    [deflate] device size: {dev_gib:.1f} GiB")

    # Snapshot .img disk usage before fstrim so we can decide whether
    # fallocate is worth running afterwards.
    def _img_blocks() -> int:
        return sum(img.stat().st_blocks for img in replica_path.glob("*.img"))

    before_blocks = _img_blocks()

    # Step 1: fstrim via a temporary mount.
    # Note: fstrim does NOT require the filesystem to be mounted with -o discard.
    # The discard mount option enables real-time per-deletion TRIM; fstrim is a
    # batch operation that sends FITRIM ioctl independently of mount options.
    # Mounting with -o discard on a filesystem that has prior journal state causes
    # FITRIM to return EBADMSG ("Bad message"), so we use a plain mount here.
    fstrim_ok = False
    tmp_mp = Path(tempfile.mkdtemp(prefix="lrm-trim-"))
    try:
        log("    [deflate] mounting source for fstrim...")
        rc_mnt, out_mnt = mount_device(src_device, tmp_mp)
        if out_mnt:
            log(f"    {out_mnt}")
        if rc_mnt != 0:
            log("    [yellow][deflate] mount failed — skipping fstrim[/yellow]")
        else:
            log("    [deflate] fstrim: sending DISCARD for all free blocks...")
            rc_trim = _stream_cmd(["fstrim", "-v", str(tmp_mp)], log)
            fstrim_ok = rc_trim == 0
            if not fstrim_ok:
                log("    [yellow][deflate] fstrim failed (DISCARD may not be supported by this Longhorn engine version)[/yellow]")
            unmount(tmp_mp)
    finally:
        tmp_mp.rmdir()

    after_fstrim_blocks = _img_blocks()
    freed_by_fstrim = (before_blocks - after_fstrim_blocks) * 512 // 1_048_576
    log(f"    [deflate] fstrim freed {freed_by_fstrim} MiB from .img files")


def move_tree(
    src: Path,
    dst: Path,
    log: Callable[[str], None],
    total: int = 0,
    deflate_every_bytes: int = 0,
    deflate_cb: Callable[[], None] | None = None,
    state_file: Path | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    workers: int = 4,
) -> None:
    """Recursively move all files from *src* to *dst*, logging progress.

    Regular files are moved in parallel using *workers* threads.  Hard-linked
    files (nlink > 1) are processed sequentially to preserve inode_map
    correctness without locking around the inode check + move pair.

    When *deflate_every_bytes* is set, regular files are grouped into batches
    no larger than that threshold.  Deflation is triggered between batches —
    never mid-batch — so the source filesystem is always fully unmounted while
    workers are idle.

    Raises:
        RuntimeError: If one or more files could not be moved.
    """
    count = 0
    errors: list[str] = []
    inode_map: dict[int, Path] = _load_inode_state(state_file, dst) if state_file else {}
    if inode_map:
        log(f"    Resumed with {len(inode_map)} hard-link inode(s) from previous run")
    bytes_since_deflate = 0

    all_items = sorted(src.rglob("*"), key=lambda p: (len(p.parts), p))

    # ------------------------------------------------------------------
    # Phase 1: directories + symlinks — sequential (fast, order-sensitive)
    # ------------------------------------------------------------------
    file_items: list[Path] = []
    for item in all_items:
        if not item.is_symlink() and not item.exists():
            continue
        if item.is_dir():
            (dst / item.relative_to(src)).mkdir(parents=True, exist_ok=True)
            continue
        if item.is_symlink():
            dest_link = dst / item.relative_to(src)
            dest_link.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest_link.symlink_to(os.readlink(item))
                item.unlink()
                count += 1
                if progress_cb:
                    progress_cb(count, total)
            except OSError as exc:
                msg = f"symlink {item} → {os.readlink(item)}: {exc}"
                log(f"    [yellow][!] skipped {msg}[/yellow]")
                errors.append(msg)
            continue
        file_items.append(item)

    # ------------------------------------------------------------------
    # Phase 2: separate hard-linked from regular files
    # Hard-linked files (nlink > 1) need sequential processing so that the
    # first occurrence is moved and later occurrences become hard links
    # without a TOCTOU race on inode_map.
    # ------------------------------------------------------------------
    regular: list[Path] = []
    hardlinked: list[Path] = []
    for item in file_items:
        try:
            (hardlinked if item.stat().st_nlink > 1 else regular).append(item)
        except OSError:
            regular.append(item)  # will fail gracefully in worker

    # ------------------------------------------------------------------
    # Phase 3: regular files — parallel
    # ------------------------------------------------------------------
    def _move_one(item: Path) -> int:
        """Move a single regular file; returns bytes moved."""
        if not item.exists():
            return 0
        st = item.stat()
        dest_file = dst / item.relative_to(src)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if st.st_size >= _LARGE_FILE_BYTES:
            log(f"    moving large file {item.name} ({st.st_size // 1024 // 1024} MiB)...")
        shutil.move(str(item), str(dest_file))
        return st.st_size

    def _run_batch(batch: list[Path]) -> int:
        """Move *batch* in parallel; returns total bytes moved."""
        nonlocal count
        total_bytes = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_move_one, item): item for item in batch}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    size = future.result()
                    count += 1
                    total_bytes += size
                    if progress_cb:
                        progress_cb(count, total)
                    if count % 50 == 0:
                        pct = f" ({count * 100 // total}%)" if total else ""
                        log(f"    moved {count}/{total if total else '?'}{pct} files...")
                except OSError as exc:
                    msg = f"{item}: {exc}"
                    log(f"    [red][!] failed {msg}[/red]")
                    errors.append(msg)
        return total_bytes

    if deflate_every_bytes > 0 and deflate_cb:
        # Split regular files into batches ≤ deflate_every_bytes so deflation
        # can fire between batches (never while workers hold the source mount).
        batch: list[Path] = []
        batch_bytes = 0
        for item in regular:
            try:
                sz = item.stat().st_size
            except OSError:
                sz = 0
            batch.append(item)
            batch_bytes += sz
            if batch_bytes >= deflate_every_bytes:
                moved = _run_batch(batch)
                bytes_since_deflate += moved
                log(f"    [deflate] {bytes_since_deflate // 1_073_741_824} GiB moved — triggering source deflation...")
                deflate_cb()
                bytes_since_deflate = 0
                batch, batch_bytes = [], 0
        if batch:
            bytes_since_deflate += _run_batch(batch)
    else:
        bytes_since_deflate += _run_batch(regular)

    # ------------------------------------------------------------------
    # Phase 4: hard-linked files — sequential
    # ------------------------------------------------------------------
    for item in hardlinked:
        if not item.is_symlink() and not item.exists():
            continue
        dest_file = dst / item.relative_to(src)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            st = item.stat()
            if st.st_ino in inode_map:
                os.link(inode_map[st.st_ino], dest_file)
                item.unlink()
            else:
                if st.st_size >= _LARGE_FILE_BYTES:
                    log(f"    moving large file {item.name} ({st.st_size // 1024 // 1024} MiB)...")
                shutil.move(str(item), str(dest_file))
                inode_map[st.st_ino] = dest_file
                if state_file:
                    _save_inode_state(state_file, inode_map, dst)
            count += 1
            bytes_since_deflate += st.st_size
            if progress_cb:
                progress_cb(count, total)
            if count % 50 == 0:
                pct = f" ({count * 100 // total}%)" if total else ""
                log(f"    moved {count}/{total if total else '?'}{pct} files...")
            if deflate_cb and deflate_every_bytes > 0 and bytes_since_deflate >= deflate_every_bytes:
                log(f"    [deflate] {bytes_since_deflate // 1_073_741_824} GiB moved — triggering source deflation...")
                deflate_cb()
                bytes_since_deflate = 0
        except OSError as exc:
            msg = f"{item}: {exc}"
            log(f"    [red][!] failed {msg}[/red]")
            errors.append(msg)

    log(f"    move_tree: {count} files moved, {len(errors)} errors")
    if state_file and state_file.exists():
        state_file.unlink()
    if errors:
        raise RuntimeError(f"{len(errors)} file(s) failed to move")
