from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from replica_migrator.models import LonghornDisk, ReplicaRow


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _parse_volume_meta(meta_path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        raw = meta_path.read_text().strip()
    except OSError as e:
        return None, str(e)
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            typed: dict[str, object] = {str(k): v for k, v in data.items()}  # type: ignore[misc]
            return typed, None
        if isinstance(data, int):
            return {"Size": data}, None
    except json.JSONDecodeError:
        pass
    try:
        return {"Size": int(raw)}, None
    except ValueError:
        return {"_unparsed": raw[:500]}, None


def _human(n: int) -> str:
    if n < 0:
        return str(n)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.2f} {units[i]}"


def resolve_replicas_root(path: Path) -> Path:
    """Return the replicas directory from *path*.

    Accepts either the replicas directory itself or a parent that contains a
    ``replicas/`` subdirectory (e.g. a Longhorn data root).
    """
    candidate = path / "replicas"
    if candidate.is_dir():
        return candidate
    return path


def scan_replicas(replicas_root: Path) -> list[ReplicaRow]:
    """List subdirectories that look like Longhorn replica data dirs."""
    rows: list[ReplicaRow] = []
    if not replicas_root.is_dir():
        return rows
    for child in sorted(replicas_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        meta_path = child / "volume.meta"
        meta: dict[str, object] | None = None
        err: str | None = None
        try:
            has_meta = meta_path.is_file()
        except PermissionError as e:
            has_meta = False
            err = f"permission denied: {e}"
        if has_meta:
            meta, err = _parse_volume_meta(meta_path)
        elif err is None:
            meta, err = None, "no volume.meta"

        size_bytes: int | None = None
        volume_name = ""
        head = ""
        parent = ""
        notes: list[str] = []
        if err:
            notes.append(err)

        if meta:
            sz = meta.get("Size")
            if isinstance(sz, int):
                size_bytes = sz
            raw_name = meta.get("Name")
            volume_name = str(raw_name) if raw_name else ""
            raw_head = meta.get("Head")
            head = str(raw_head) if raw_head else ""
            raw_parent = meta.get("Parent")
            parent = str(raw_parent) if raw_parent else ""
            if "_unparsed" in meta:
                notes.append(f"unparsed meta: {str(meta['_unparsed'])[:80]}")

        if size_bytes is None:
            size_bytes = _dir_size(child)
            notes.append("size: sum of files (no Size in meta)")

        meta_note = "; ".join(notes) if notes else "—"

        if not volume_name:
            volume_name = "—"

        rows.append(
            ReplicaRow(
                path=child.resolve(),
                dir_name=child.name,
                size_bytes=size_bytes,
                volume_name=volume_name,
                head=head or "—",
                parent=parent or "—",
                meta_note=meta_note.strip() or "—",
            )
        )
    return rows


def format_size(n: int | None) -> str:
    if n is None:
        return "—"
    return _human(n)


def list_longhorn_disks(dev_root: Path) -> list[LonghornDisk]:
    """List block devices / symlinks under /dev/longhorn/."""
    out: list[LonghornDisk] = []
    if not dev_root.is_dir():
        return out
    for child in sorted(dev_root.iterdir(), key=lambda p: p.name.lower()):
        try:
            st = child.lstat()
        except OSError:
            continue
        mode = st.st_mode
        target: str | None = None
        if stat.S_ISLNK(mode):
            try:
                target = os.readlink(child)
            except OSError:
                target = "?"
            kind = "symlink"
        elif stat.S_ISBLK(mode):
            kind = "block"
        elif stat.S_ISCHR(mode):
            kind = "char"
        else:
            kind = "other"
        bits = f"{kind} {st.st_rdev}" if stat.S_ISBLK(mode) or stat.S_ISCHR(mode) else kind
        out.append(LonghornDisk(path=child.resolve(), target=target, mode_bits=bits))
    return out
