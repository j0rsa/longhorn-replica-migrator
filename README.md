# Longhorn Replica Migrator

A terminal UI (TUI) for recovering data from orphaned Longhorn replica directories
and migrating that data into a new, pre-provisioned Longhorn volume.

Must be run **directly on the Longhorn storage node** with `kubectl` available and
configured to reach the cluster API server.  **The tool must be run as `root`** —
`mount`/`umount` require root privileges, and the Longhorn block devices under
`/dev/longhorn/` are typically only accessible to root.

![Main screen](docs/main-screen.png)

---

## How it works

Longhorn stores replica data as raw files on the node's filesystem.  When a volume
is lost or corrupted but the replica directory survives, the data is still
recoverable.  This tool automates the two-mount recovery approach:

1. A **recovery pod** (`longhornio/longhorn-engine`) is scheduled on the same node
   with the orphaned replica directory mounted at `/volume`.  The pod runs
   `launch-simple-longhorn <volume-name> <size>`, which reconstructs the block
   device and exposes it at `/dev/longhorn/<volume-name>` on the host.

2. The tool then mounts both the **source** device (created by the recovery pod)
   and the **target** device (a new, empty Longhorn volume you pre-provision and
   attach to the node) to temporary directories.

3. All files are copied (or moved) from the source mountpoint to the target
   mountpoint.

4. Both mounts are released, the recovery pod is deleted, and — optionally — the
   original replica directory is removed.

The target Longhorn volume is a regular, healthy Longhorn PVC that Kubernetes
manages going forward.  After migration you can detach the volume and use it from
any workload.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Longhorn storage node | The node where the orphaned replica directory lives |
| **Run as `root`** | Required — `mount`/`umount` and `/dev/longhorn/` access need root |
| `kubectl` configured | Must be able to schedule pods in the cluster |
| Python 3.11+ | `uv` is the recommended runtime manager |
| Target Longhorn volume | A new, **empty** Longhorn volume already attached to this node (see below) |

### Preparing the target volume

Before launching the migrator, create a new Longhorn volume via the Longhorn UI or
a PVC manifest and attach it to this specific node.  The volume must be:

- Empty (no existing data)
- Large enough to hold the source replica's data
- Attached and visible under `/dev/longhorn/<name>` on the node

---

## Installation

Modern Debian/Ubuntu systems block `pip install` into the system Python (PEP 668).
Use **`pipx`** — it creates an isolated virtualenv automatically and puts the
command on your `PATH`:

```bash
# Install pipx if not already present
apt install pipx
pipx ensurepath   # adds ~/.local/bin to PATH; re-login or source ~/.bashrc

# Build the wheel, then install
uv build
pipx install dist/longhorn_replica_migrator-*.whl

# Upgrade after a rebuild
pipx upgrade longhorn-replica-migrator
# or: pipx install --force dist/longhorn_replica_migrator-*.whl
```

### Alternative: manual virtualenv

```bash
python3 -m venv /opt/replica-migrator
/opt/replica-migrator/bin/pip install dist/longhorn_replica_migrator-*.whl
# run via full path:
/opt/replica-migrator/bin/longhorn-replica-migrator /var/lib/longhorn/replicas
```

### Run without installing (development)

```bash
uv run longhorn-replica-migrator /var/lib/longhorn/replicas
```

---

## Usage

```
longhorn-replica-migrator <replicas_dir> [--dev-root /dev/longhorn]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `replicas_dir` | (required) | Directory containing Longhorn replica subdirectories, e.g. `/var/lib/longhorn/replicas` |
| `--dev-root` | `/dev/longhorn` | Directory where Longhorn exposes block devices |

Example:

```bash
longhorn-replica-migrator /var/lib/longhorn/replicas
```

---

## TUI walkthrough

### Step 1 — Select source replica

Press **1 · Select source replica**.  A table lists every subdirectory found under
`replicas_dir` along with its size, volume name, and metadata notes.  Navigate with
arrow keys, press **Enter** to confirm, or **Esc** to cancel.

### Step 2 — Select destination disk

Press **2 · Select destination disk (/dev/longhorn/…)**.  A table lists every entry
found under `--dev-root`.  Select the **target** volume — the new, empty Longhorn
volume you attached to this node.  Press **Enter** to confirm.

Once both selections are made, button **3** becomes active.

### Step 3 — Configure and run

Press **3 · Configure & Run Migration**.  A modal form appears:

| Field | Default | Description |
|-------|---------|-------------|
| Node hostname | auto-detected | Used as `kubernetes.io/hostname` node selector |
| Longhorn engine image | `longhornio/longhorn-engine:v1.10.0` | Container image for the recovery pod |
| Transfer mode | Copy | Copy (safe) or Move (destructive) |
| Delete source replica dir | Off | Remove the replica directory after transfer |

Press **Run Migration →** to start.

The migration log streams in real time.  If the target device is unformatted, it is
automatically formatted with the same filesystem as the source before mounting.

![Migration in progress](docs/migration-1.png)

![Migration complete](docs/migration-2.png)

---

## The 8 automated migration steps

| Step | Action |
|------|--------|
| pre | Verify `kubectl` is available |
| 1/8 | Log the node hostname |
| 2/8 | Derive the source device path (`/dev/longhorn/<volume-name>`) |
| 3/8 | Build and apply the recovery pod manifest |
| 4/8 | Wait up to 120 s for the pod to reach `Running` state |
| 5/8 | Wait up to 60 s for the source block device to appear |
| 6/8 | Mount source and target devices to temporary directories |
| 7/8 | Copy or move all files from source mountpoint to target mountpoint |
| 8/8 | Unmount both devices; delete the recovery pod |
| opt | (optional) Delete the original replica directory |

All progress is streamed to the log panel in real time.

---

## Transfer modes

### Copy (safe, default)

Files are copied with `shutil.copy2` (preserving metadata).  The original replica
directory is left intact.  Use this when you want a fallback.

### Move (destructive)

Files are moved with `shutil.move`.  This frees disk space on the source
immediately.  **There is no undo.**  Only use move mode when you are confident the
migration will succeed and you have verified the target volume beforehand.

---

## Terminal mouse support

The TUI supports mouse navigation.  **Keyboard navigation always works** and is the
recommended way to operate the tool over SSH:

| Key | Action |
|-----|--------|
| **Tab** / **Shift+Tab** | Move focus between buttons |
| **Enter** / **Space** | Activate the focused button |
| **↑ / ↓** | Navigate rows in the replica/disk table |
| **Enter** | Confirm selection in a table |
| **Esc** | Cancel / go back |
| **q** or **Ctrl+C** | Quit |

### Mouse clicks not working over SSH

If hover highlights work but button clicks do nothing, the most common cause is a
**terminal multiplexer** (tmux or screen) swallowing mouse button events.

**tmux fix** — add to `~/.tmux.conf` and reload:
```bash
set -g mouse on
```
```bash
tmux source ~/.tmux.conf
```

**screen** — start screen with `screen -m` or add `mousetrack on` to `~/.screenrc`.

If you are not using a multiplexer, the SSH client itself may not be forwarding SGR
mouse sequences (`?1006h`).  In that case use keyboard navigation — it is equivalent.

---

## Notes and warnings

- **The tool must be run as `root`.**  Both `mount`/`umount` and direct access to
  Longhorn block devices under `/dev/longhorn/` require root privileges.  Running
  as a non-root user will cause the migration to fail at the mount step.
- The target Longhorn volume **must** be attached to the **same node** before
  running the migrator.  The tool does not provision or attach volumes automatically.
- If the migration is interrupted mid-transfer, the target volume may contain
  partial data.  Inspect the log output to determine how many files were transferred.
- The recovery pod name is fixed as `longhorn-replica-recovery` in the `default`
  namespace.  If a pod with that name already exists from a previous run, the tool
  will delete it before proceeding.
