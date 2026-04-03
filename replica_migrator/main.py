"""Textual TUI entry point for the Longhorn replica migrator."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar, cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
    Switch,
)

from replica_migrator import kubectl as kube
from replica_migrator import ops
from replica_migrator.kubectl import DEFAULT_IMAGE
from replica_migrator.models import LonghornDisk, MigrationConfig, ReplicaRow
from replica_migrator.scan import format_size, list_longhorn_disks, resolve_replicas_root, scan_replicas

DEFAULT_LONGHORN_DEV = Path("/dev/longhorn")


# ---------------------------------------------------------------------------
# ReplicaPickScreen — unchanged from original
# ---------------------------------------------------------------------------


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Ask the user to confirm deletion of a replica directory."""

    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm_panel {
        width: 70;
        height: auto;
        border: heavy $error;
        padding: 1 2;
    }
    #confirm_panel Label {
        margin-bottom: 1;
    }
    #confirm_btn_row {
        height: auto;
        margin-top: 1;
    }
    #confirm_btn_row Button {
        width: 1fr;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_false", "Cancel", show=True),
        Binding("n", "dismiss_false", "No", show=True),
        Binding("y", "action_confirm_delete", "Yes, delete", show=True),
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        with Container(id="confirm_panel"):
            yield Label(f"[b red]Delete replica directory?[/b red]")
            yield Label(f"{self._path}")
            yield Label("[yellow]This action cannot be undone.[/yellow]")
            with Horizontal(id="confirm_btn_row"):
                yield Button("Cancel (Esc / N)", id="btn_no", variant="default")
                yield Button("Delete (Y)", id="btn_yes", variant="error")

    def action_dismiss_false(self) -> None:
        self.dismiss(False)

    def action_confirm_delete(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn_no")
    def on_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn_yes")
    def on_yes(self) -> None:
        self.dismiss(True)


class ReplicaPickScreen(ModalScreen[ReplicaRow | None]):
    """Pick one replica directory."""

    CSS = """
    ReplicaPickScreen {
        align: left top;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Back", show=True),
        Binding("enter", "confirm", "Select", show=True),
        Binding("d", "delete_replica", "Delete", show=True),
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    def __init__(self, replicas_root: Path) -> None:
        """Initialise the replica picker.

        Args:
            replicas_root: Root directory containing replica subdirectories.
        """
        super().__init__()
        self.replicas_root = replicas_root
        self._rows: list[ReplicaRow] = []

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield Label(f"Source replicas under [b]{self.replicas_root}[/b] — ↑/↓ navigate, Enter confirm, Esc cancel")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="replica_table")
        yield Static("", id="replica_detail")

    def on_mount(self) -> None:
        """Populate the table with scanned replica directories."""
        table = self.query_one("#replica_table", DataTable)
        table.add_columns(
            "Directory",
            "Size",
            "Volume name",
            "Head",
            "Parent",
            "Notes",
        )
        self._rows = scan_replicas(self.replicas_root)
        for r in self._rows:
            key = r.path.name
            table.add_row(
                r.dir_name,
                format_size(r.size_bytes),
                r.volume_name,
                _short(r.head, 36),
                _short(r.parent, 36),
                _short(r.meta_note, 42),
                key=key,
            )
        if self._rows:
            table.move_cursor(row=0)

    def action_dismiss_none(self) -> None:
        """Dismiss without selecting a replica."""
        self.dismiss(None)

    def action_confirm(self) -> None:
        """Confirm the currently highlighted replica row."""
        table = self.query_one("#replica_table", DataTable)
        cursor = table.cursor_row
        if cursor < 0 or cursor >= len(self._rows):
            return
        self.dismiss(self._rows[cursor])

    def action_delete_replica(self) -> None:
        """Ask for confirmation then delete the highlighted replica directory."""
        table = self.query_one("#replica_table", DataTable)
        cursor = table.cursor_row
        if cursor < 0 or cursor >= len(self._rows):
            return
        row = self._rows[cursor]
        self.app.push_screen(ConfirmDeleteScreen(row.path), self._after_delete(cursor))

    def _after_delete(self, cursor: int):
        def callback(confirmed: bool | None) -> None:
            if not confirmed:
                return
            row = self._rows[cursor]
            shutil.rmtree(row.path)
            self._rows.pop(cursor)
            table = self.query_one("#replica_table", DataTable)
            table.remove_row(row.path.name)
            new_cursor = min(cursor, len(self._rows) - 1)
            if new_cursor >= 0:
                table.move_cursor(row=new_cursor)
            self.query_one("#replica_detail", Static).update("")
        return callback

    @on(DataTable.RowSelected, "#replica_table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Dismiss with the selected row when Enter is pressed in the table."""
        idx = event.cursor_row
        if 0 <= idx < len(self._rows):
            self.dismiss(self._rows[idx])

    @on(DataTable.RowHighlighted, "#replica_table")
    def on_row_highlight(self, event: DataTable.RowHighlighted) -> None:
        """Update the detail panel when the cursor moves."""
        idx = event.cursor_row
        detail = self.query_one("#replica_detail", Static)
        if 0 <= idx < len(self._rows):
            r = self._rows[idx]
            detail.update(
                f"[b]Path:[/b] {r.path}\n[b]Volume name:[/b] {r.volume_name}  [b]Size:[/b] {format_size(r.size_bytes)}"
            )


# ---------------------------------------------------------------------------
# DiskPickScreen — unchanged from original
# ---------------------------------------------------------------------------


class DiskPickScreen(ModalScreen[LonghornDisk | None]):
    """Pick a device under /dev/longhorn/."""

    CSS = """
    DiskPickScreen {
        align: left top;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Back", show=True),
        Binding("enter", "confirm", "Select", show=True),
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    def __init__(self, dev_root: Path) -> None:
        """Initialise the disk picker.

        Args:
            dev_root: Directory to list block devices from (e.g. ``/dev/longhorn``).
        """
        super().__init__()
        self.dev_root = dev_root
        self._disks: list[LonghornDisk] = []

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield Label(f"Longhorn devices under [b]{self.dev_root}[/b] — ↑/↓ navigate, Enter confirm, Esc cancel")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="disk_table")
        yield Static("", id="disk_detail")

    def on_mount(self) -> None:
        """Populate the table with available Longhorn block devices."""
        table = self.query_one("#disk_table", DataTable)
        table.add_columns("Name", "Type / info", "Symlink target")
        self._disks = list_longhorn_disks(self.dev_root)
        for d in self._disks:
            tgt = d.target if d.target else "—"
            table.add_row(d.path.name, d.mode_bits, _short(tgt, 48), key=d.path.name)
        if self._disks:
            table.move_cursor(row=0)
        else:
            self.query_one("#disk_detail", Static).update(
                f"[yellow]No entries in {self.dev_root}[/yellow] "
                "(expected on a host without Longhorn block devs, e.g. macOS)."
            )

    def action_dismiss_none(self) -> None:
        """Dismiss without selecting a device."""
        self.dismiss(None)

    def action_confirm(self) -> None:
        """Confirm the currently highlighted disk row."""
        table = self.query_one("#disk_table", DataTable)
        cursor = table.cursor_row
        if cursor < 0 or cursor >= len(self._disks):
            return
        self.dismiss(self._disks[cursor])

    @on(DataTable.RowSelected, "#disk_table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Dismiss with the selected disk when Enter is pressed in the table."""
        idx = event.cursor_row
        if 0 <= idx < len(self._disks):
            self.dismiss(self._disks[idx])

    @on(DataTable.RowHighlighted, "#disk_table")
    def on_row_highlight(self, event: DataTable.RowHighlighted) -> None:
        """Update the detail panel when the cursor moves."""
        idx = event.cursor_row
        detail = self.query_one("#disk_detail", Static)
        if 0 <= idx < len(self._disks):
            d = self._disks[idx]
            detail.update(f"[b]Full path:[/b] {d.path}")


# ---------------------------------------------------------------------------
# ConfigScreen
# ---------------------------------------------------------------------------


class ConfigScreen(ModalScreen[MigrationConfig | None]):
    """Modal configuration screen for the migration run.

    Collects the engine image, transfer mode, and cleanup options before
    the user launches the migration.
    """

    CSS = """
    ConfigScreen {
        align: center middle;
    }
    #config_panel {
        width: 80;
        height: auto;
        border: heavy $primary;
        padding: 1 2;
    }
    #config_panel Label {
        margin-top: 1;
    }
    #config_btn_row {
        margin-top: 2;
        height: auto;
    }
    #config_btn_row Button {
        width: 1fr;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "action_dismiss_none", "Cancel", show=True),
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    def __init__(self, replica: ReplicaRow, disk: LonghornDisk) -> None:
        """Initialise the config screen with pre-selected source and target.

        Args:
            replica: Source replica directory chosen by the user.
            disk: Target Longhorn block device chosen by the user.
        """
        super().__init__()
        self._replica = replica
        self._disk = disk
        self._hostname = kube.get_hostname()

    def compose(self) -> ComposeResult:
        """Build the configuration form."""
        with Container(id="config_panel"):
            yield Label("[b]Configure migration[/b]")
            yield Label("Node hostname:")
            yield Static(self._hostname, id="hostname_static")
            yield Label("Longhorn engine image:")
            yield Input(DEFAULT_IMAGE, id="image_input")
            yield Label("Transfer mode:")
            with RadioSet(id="mode_radio"):
                yield RadioButton(
                    "Copy — safe, keeps source intact",
                    id="rb_copy",
                    value=True,
                )
                yield RadioButton(
                    "Move — destructive, frees source disk space",
                    id="rb_move",
                )
            yield Label("Delete source replica dir after transfer?")
            yield Switch(False, id="delete_switch")
            with Horizontal(id="config_btn_row"):
                yield Button("Cancel", id="btn_cancel")
                yield Button("Run Migration →", id="btn_run", variant="success")

    def action_dismiss_none(self) -> None:
        """Dismiss without starting a migration."""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self) -> None:
        """Cancel and return to the main screen."""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_run")
    def on_run(self) -> None:
        """Build a MigrationConfig from the form and dismiss with it."""
        image_input = self.query_one("#image_input", Input)
        image = image_input.value.strip() or DEFAULT_IMAGE

        mode_radio = self.query_one("#mode_radio", RadioSet)
        pressed = mode_radio.pressed_button
        move_mode = pressed is not None and pressed.id == "rb_move"

        delete_switch = self.query_one("#delete_switch", Switch)
        delete_replica = bool(delete_switch.value)

        cfg = MigrationConfig(
            replica=self._replica,
            disk=self._disk,
            hostname=self._hostname,
            image=image,
            move_mode=move_mode,
            delete_replica=delete_replica,
        )
        self.dismiss(cfg)


# ---------------------------------------------------------------------------
# MigrationScreen
# ---------------------------------------------------------------------------


class MigrationScreen(Screen[None]):
    """Full-screen migration progress display.

    Runs the 8-step migration workflow in a background thread and streams
    log lines to the RichLog widget.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    CSS = """
    MigrationScreen {
        layout: vertical;
    }
    #status_line {
        padding: 0 2;
        height: 3;
        background: $surface;
        border-bottom: solid $primary;
    }
    #log_box {
        height: 1fr;
        border: solid $primary;
        margin: 0 1;
    }
    #btn_migration_cancel {
        margin: 1 1;
        width: auto;
    }
    """

    class LogLine(Message):
        """A log line to append to the RichLog widget.

        Attributes:
            text: Rich-markup text to display.
        """

        def __init__(self, text: str) -> None:
            """Create a log line message.

            Args:
                text: Rich-markup text to display.
            """
            super().__init__()
            self.text = text

    class Done(Message):
        """Signals that the migration thread has finished.

        Attributes:
            success: Whether the migration completed successfully.
        """

        def __init__(self, success: bool) -> None:
            """Create a done message.

            Args:
                success: Whether migration completed without errors.
            """
            super().__init__()
            self.success = success

    def __init__(self, config: MigrationConfig) -> None:
        """Initialise the migration screen with a validated config.

        Args:
            config: All parameters for this migration run.
        """
        super().__init__()
        self._config = config
        self._done = False

    def compose(self) -> ComposeResult:
        """Build the migration progress UI."""
        yield Header()
        yield Static("Migration in progress…", id="status_line")
        yield RichLog(highlight=True, markup=True, id="log_box")
        yield Button("Cancel / Cleanup", id="btn_migration_cancel", variant="warning")
        yield Footer()

    def on_mount(self) -> None:
        """Start the migration worker when the screen mounts."""
        self._start_migration()

    def on_migration_screen_log_line(self, event: MigrationScreen.LogLine) -> None:
        """Append a log line to the RichLog widget.

        Args:
            event: The log line message.
        """
        log_box = self.query_one("#log_box", RichLog)
        log_box.write(event.text)
        status = self.query_one("#status_line", Static)
        # Strip markup for plain status
        plain = event.text.replace("[/bold]", "").replace("[bold]", "")
        status.update(plain)

    def on_migration_screen_done(self, event: MigrationScreen.Done) -> None:
        """Handle migration completion — update button and status.

        Args:
            event: The done message.
        """
        self._done = True
        btn = self.query_one("#btn_migration_cancel", Button)
        btn.label = "Back"  # type: ignore[assignment]
        btn.variant = "default"
        status = self.query_one("#status_line", Static)
        if event.success:
            status.update("[green bold]Migration complete[/green bold]")
        else:
            status.update("[red bold]Migration finished with errors — see log above[/red bold]")

    @on(Button.Pressed, "#btn_migration_cancel")
    def on_cancel_or_back(self) -> None:
        """Pop this screen when migration is done, or attempt cancellation."""
        if self._done:
            cast("MigratorApp", self.app).pop_screen()  # type: ignore[misc]
        else:
            log_box = self.query_one("#log_box", RichLog)
            log_box.write("[yellow][!] Cancellation not supported while migration is running.[/yellow]")

    @work(thread=True)
    def _start_migration(self) -> None:
        """Execute the full 8-step migration workflow in a background thread.

        Posts :class:`LogLine` messages throughout for UI updates and
        always posts :class:`Done` on exit regardless of success or failure.
        """
        cfg = self._config
        success = False
        src_mp: Path | None = None
        dst_mp: Path | None = None

        def log(text: str) -> None:
            self.post_message(MigrationScreen.LogLine(text))

        try:
            # -- Pre-flight -------------------------------------------------
            log("[bold][pre][/bold] Checking kubectl...")
            rc, out, err = kube.run_cmd("kubectl", "version", "--client")
            if out:
                log(f"    {out}")
            if err:
                log(f"    {err}")
            if rc != 0:
                log("[red]kubectl not available or misconfigured — aborting.[/red]")
                return

            # -- Step 1: hostname -------------------------------------------
            log(f"[bold][1/8][/bold] Node hostname: {cfg.hostname}")

            # -- Clean up any stale pod -------------------------------------
            phase = kube.pod_phase()
            if phase is not None:
                log(f"[yellow][!][/yellow] Existing pod found (phase={phase}), removing...")
                kube.delete_pod()
                time.sleep(3)

            # -- Step 2: derive source device path --------------------------
            vol = cfg.replica.volume_name if cfg.replica.volume_name != "—" else cfg.replica.dir_name
            src_device = Path("/dev/longhorn") / vol
            log(f"[bold][2/8][/bold] Source device will be: {src_device}")

            # -- Step 3: apply recovery pod ---------------------------------
            yaml_str = ops.build_pod_yaml(
                cfg.replica.path,
                vol,
                cfg.replica.size_bytes or 0,
                cfg.hostname,
                cfg.image,
            )
            log(f"[bold][3/8][/bold] Applying recovery pod ({cfg.image})...")
            rc, out = kube.apply_yaml(yaml_str)
            if out:
                log(f"    {out}")
            if rc != 0:
                log("[red]Failed to apply recovery pod — aborting.[/red]")
                return

            # -- Step 4: wait for pod Running -------------------------------
            log("[bold][4/8][/bold] Waiting for pod Running (up to 120 s)...")
            ok = kube.wait_pod_running()
            if not ok:
                log("[red]Pod did not reach Running state in time — aborting.[/red]")
                kube.delete_pod()
                return
            log("    Pod is Running")

            # -- Step 5: wait for source device -----------------------------
            log(f"[bold][5/8][/bold] Waiting for source device {src_device} (up to 60 s)...")
            ok = kube.wait_device(src_device, 60)
            if not ok:
                log(f"[red]Device {src_device} did not appear in time — aborting.[/red]")
                kube.delete_pod()
                return
            log(f"    Device {src_device} is ready")

            # -- Step 6: mount source and target ----------------------------
            src_mp = Path(tempfile.mkdtemp(prefix="lrm-src-"))
            dst_mp = Path(tempfile.mkdtemp(prefix="lrm-dst-"))

            log(f"[bold][6/8][/bold] Mounting source {src_device} → {src_mp}")
            rc, out = ops.mount_device(src_device, src_mp)
            if out:
                log(f"    {out}")
            if rc != 0:
                log("[red]Failed to mount source device — aborting.[/red]")
                src_mp.rmdir()
                dst_mp.rmdir()
                kube.delete_pod()
                return

            src_fs = ops.detect_fs_type(src_device)
            log(f"    Source filesystem: {src_fs or 'unknown'}")

            dst_fs = ops.detect_fs_type(cfg.disk.path)
            log(f"    Target filesystem: {dst_fs or 'unformatted'}")
            if dst_fs is None:
                if src_fs is None:
                    log("[red]Cannot determine filesystem type — aborting.[/red]")
                    ops.unmount(src_mp)
                    src_mp.rmdir()
                    dst_mp.rmdir()
                    kube.delete_pod()
                    return
                log(f"    Target is unformatted — formatting as {src_fs}...")
                rc, out = ops.format_device(cfg.disk.path, src_fs)
                if out:
                    log(f"    {out}")
                if rc != 0:
                    log("[red]Failed to format target device — aborting.[/red]")
                    ops.unmount(src_mp)
                    src_mp.rmdir()
                    dst_mp.rmdir()
                    kube.delete_pod()
                    return
                log(f"    Formatted {cfg.disk.path} as {src_fs}")

            log(f"              Mounting target {cfg.disk.path} → {dst_mp}")
            rc, out = ops.mount_device(cfg.disk.path, dst_mp)
            if out:
                log(f"    {out}")
            if rc != 0:
                log("[red]Failed to mount target device — aborting.[/red]")
                ops.unmount(src_mp)
                src_mp.rmdir()
                dst_mp.rmdir()
                kube.delete_pod()
                return

            # -- Step 7: transfer files ------------------------------------
            mode_label = "Moving" if cfg.move_mode else "Copying"
            log(f"[bold][7/8][/bold] {mode_label} files: {src_mp} → {dst_mp}")
            transfer_ok = True
            try:
                if cfg.move_mode:
                    ops.move_tree(src_mp, dst_mp, log)
                else:
                    ops.copy_tree(src_mp, dst_mp, log)
                log("    Transfer complete")
            except Exception as exc:
                log(f"[red][!] Transfer error: {exc}[/red]")
                transfer_ok = False

            # -- Step 8: unmount both ---------------------------------------
            log("[bold][8/8][/bold] Unmounting source and target...")
            _rc, _out = ops.unmount(src_mp)
            if _out:
                log(f"    {_out}")
            src_mp.rmdir()
            src_mp = None

            _rc, _out = ops.unmount(dst_mp)
            if _out:
                log(f"    {_out}")
            dst_mp.rmdir()
            dst_mp = None

            # -- Delete recovery pod ----------------------------------------
            log("Deleting recovery pod...")
            kube.delete_pod()

            # -- Optional: delete source replica dir ------------------------
            if cfg.delete_replica and transfer_ok:
                log(f"[opt] Deleting replica dir {cfg.replica.path}...")
                shutil.rmtree(cfg.replica.path)
                log("    Deleted")

            if transfer_ok:
                log("[green bold]✓ Migration complete[/green bold]")
                success = True
            else:
                log("[yellow]Migration finished but transfer had errors.[/yellow]")

        except Exception as exc:
            log(f"[red][!] Unexpected error: {exc}[/red]")
            # Best-effort cleanup
            with contextlib.suppress(Exception):
                if src_mp is not None and src_mp.is_mount():
                    ops.unmount(src_mp)
                if src_mp is not None and src_mp.exists():
                    src_mp.rmdir()
            with contextlib.suppress(Exception):
                if dst_mp is not None and dst_mp.is_mount():
                    ops.unmount(dst_mp)
                if dst_mp is not None and dst_mp.exists():
                    dst_mp.rmdir()
            with contextlib.suppress(Exception):
                kube.delete_pod()
        finally:
            self.post_message(MigrationScreen.Done(success))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short(s: str, n: int) -> str:
    """Truncate a string to at most *n* characters, adding an ellipsis.

    Args:
        s: Input string (newlines replaced with spaces).
        n: Maximum output length.

    Returns:
        Truncated string, with trailing ``…`` if it was shortened.
    """
    s = s.replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# MigratorApp
# ---------------------------------------------------------------------------


class MigratorApp(App[None]):
    """Root Textual application for the Longhorn replica migrator."""

    TITLE = "Longhorn replica migrator"
    CSS = """
    #backdrop {
        width: 100%;
        height: 1fr;
        align: center middle;
    }
    #main_panel {
        width: 90%;
        height: auto;
        max-width: 120;
        border: heavy $primary;
        padding: 1 2;
    }
    #summary {
        margin-bottom: 1;
        min-height: 5;
        padding: 1;
        background: $surface;
    }
    .menu_btn {
        width: 100%;
        margin-top: 1;
    }
    DataTable {
        height: 18;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "select_replica", "Source replica", show=True),
        Binding("2", "select_disk", "Destination disk", show=True),
        Binding("3", "run_migration", "Run migration", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("up", "focus_previous", "Up", show=False),
        Binding("down", "focus_next", "Next", show=False),
    ]

    def __init__(self, replicas_root: Path, dev_root: Path) -> None:
        """Initialise the app with root paths for replicas and devices.

        Args:
            replicas_root: Directory containing Longhorn replica subdirectories.
            dev_root: Directory exposing Longhorn block devices (``/dev/longhorn``).
        """
        super().__init__()
        self.replicas_root = replicas_root.resolve()
        self.dev_root = dev_root.resolve()
        self.selected_replica: ReplicaRow | None = None
        self.selected_disk: LonghornDisk | None = None

    def compose(self) -> ComposeResult:
        """Build the main menu layout."""
        yield Header()
        with Container(id="backdrop"), Container(id="main_panel"):
            yield Static(id="summary")
            with Vertical():
                yield Button(
                    "1 · Select source replica",
                    id="btn_replica",
                    classes="menu_btn",
                    variant="primary",
                )
                yield Button(
                    "2 · Select destination disk (/dev/longhorn/…)",
                    id="btn_disk",
                    classes="menu_btn",
                )
                yield Button(
                    "3 · Configure & Run Migration",
                    id="btn_run_migration",
                    classes="menu_btn",
                    variant="success",
                    disabled=True,
                )
                yield Button("Quit", id="btn_quit", classes="menu_btn", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        """Refresh the summary panel on first mount."""
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        """Rebuild the summary Static widget from current selections."""
        r = self.selected_replica
        d = self.selected_disk
        block = f"[b]Replicas directory[/b]\n{self.replicas_root}\n\n[b]Source replica[/b]\n"
        if r:
            block += (
                f"  {r.dir_name}\n  path: {r.path}\n  size: {format_size(r.size_bytes)}  volume name: {r.volume_name}\n"
            )
        else:
            block += "  [dim](none selected)[/dim]\n"
        block += "\n[b]Destination (Longhorn block dev)[/b]\n"
        if d:
            block += f"  {d.path}\n"
            if d.target:
                block += f"  → {d.target}\n"
        else:
            block += "  [dim](none selected)[/dim]\n"
        self.query_one("#summary", Static).update(block)
        self.query_one("#btn_run_migration", Button).disabled = not (self.selected_replica and self.selected_disk)

    def action_select_replica(self) -> None:
        """Open the replica picker via keyboard shortcut."""
        self.open_replica_picker()

    def action_select_disk(self) -> None:
        """Open the disk picker via keyboard shortcut."""
        self.open_disk_picker()

    def action_run_migration(self) -> None:
        """Open the config screen via keyboard shortcut."""
        self.open_config_screen()

    @on(Button.Pressed, "#btn_replica")
    def open_replica_picker(self) -> None:
        """Open the replica selection modal."""
        self.push_screen(ReplicaPickScreen(self.replicas_root), self._after_replica)

    def _after_replica(self, result: ReplicaRow | None) -> None:
        """Handle the result of the replica picker.

        Args:
            result: Selected replica row, or ``None`` if cancelled.
        """
        if result is not None:
            self.selected_replica = result
        self._refresh_summary()

    @on(Button.Pressed, "#btn_disk")
    def open_disk_picker(self) -> None:
        """Open the Longhorn device selection modal."""
        self.push_screen(DiskPickScreen(self.dev_root), self._after_disk)

    def _after_disk(self, result: LonghornDisk | None) -> None:
        """Handle the result of the disk picker.

        Args:
            result: Selected disk, or ``None`` if cancelled.
        """
        if result is not None:
            self.selected_disk = result
        self._refresh_summary()

    @on(Button.Pressed, "#btn_run_migration")
    def open_config_screen(self) -> None:
        """Open the migration configuration modal."""
        if self.selected_replica is None or self.selected_disk is None:
            return
        self.push_screen(
            ConfigScreen(self.selected_replica, self.selected_disk),
            self._after_config,
        )

    def _after_config(self, result: MigrationConfig | None) -> None:
        """Handle the result of the config screen.

        Args:
            result: Completed MigrationConfig, or ``None`` if cancelled.
        """
        if result is not None:
            self.push_screen(MigrationScreen(result))

    @on(Button.Pressed, "#btn_quit")
    def quit_btn(self) -> None:
        """Exit the application."""
        self.exit()

    async def action_quit(self) -> None:
        """Exit the application via keyboard binding."""
        self.exit()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> None:
    """Parse CLI arguments and launch the Textual TUI.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).
    """
    p = argparse.ArgumentParser(description="TUI for planning migration from Longhorn replica dirs to new volumes.")
    p.add_argument(
        "replicas_dir",
        type=Path,
        help="Directory containing Longhorn replica subdirs (e.g. /var/lib/longhorn/replicas)",
    )
    p.add_argument(
        "--dev-root",
        type=Path,
        default=DEFAULT_LONGHORN_DEV,
        help=f"Where Longhorn exposes block devices (default: {DEFAULT_LONGHORN_DEV})",
    )
    args = p.parse_args(argv)

    root = args.replicas_dir
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    root = resolve_replicas_root(root)

    app = MigratorApp(replicas_root=root, dev_root=args.dev_root)
    app.run()


if __name__ == "__main__":
    run()
