# UI Mockup & Layout Notes

## Main Screen

```
┌──────────────────────────────────────────────────────────────────┐
│  Longhorn replica migrator                       [Header 1 row]  │
├──────────────────────────────────────────────────────────────────┤
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Replicas directory                                        │  │
│  │  /data/longhorn-archive/replicas                          │  │  #summary
│  │                                                            │  │  background: $surface
│  │  Source replica                                            │  │  min-height: 5
│  │    bitwarden-data-83b4db0c  1.00 GiB                      │  │  padding: 1
│  │                                                            │  │
│  │  Destination (Longhorn block dev)                          │  │
│  │    /dev/longhorn/bitwarden                                 │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │  #backdrop
│  ┌──────────────────────────────────────────────────────────────┐ │  align: center middle
│  │          1 · Select source replica                           │ │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │          2 · Select destination disk (/dev/longhorn/…)       │ │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │          3 · Configure & Run Migration          [success]    │ │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │          4 · Deflate source replica             [warning]    │ │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │          Quit                                   [error]      │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  1 Source replica  2 Destination disk  3 Run  4 Deflate  q Quit  │  Footer
└──────────────────────────────────────────────────────────────────┘
```

**Buttons 3 and 4** are disabled (`disabled=True`) until the relevant
selections are made:
- Button 3 requires both replica + disk selected
- Button 4 requires replica selected only

---

## Migration Screen

```
┌──────────────────────────────────────────────────────────────────┐
│  Longhorn replica migrator                       [Header 1 row]  │
├──────────────────────────────────────────────────────────────────┤
│                                                  [#status_line]  │
│  Moved 449/4231 (10%)                            height: 4       │
│                                                  padding: 1 2 0 2│
├╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┤  border-bottom: solid
│ ┌────────────────────────────────────────────────────────────┐   │
│ │  [pre]  Checking kubectl...                                │   │
│ │  [1/8]  Node hostname: cvps                               │   │  #log_box
│ │  [2/8]  Source device: /dev/longhorn/media                │   │  height: 1fr
│ │  [3/8]  Applying recovery pod...                          │   │  border: solid
│ │  [4/8]  Waiting for pod Running...                        │   │  margin: 0 1
│ │      Pod is Running                                       │   │
│ │  [5/8]  Waiting for device /dev/longhorn/media...         │   │
│ │      Device is ready                                      │   │
│ │  [6/8]  Mounting source → /tmp/lrm-src-xxx               │   │
│ │  [7/8]  Moving files...                                   │   │
│ │      moving large file media.img (48231 MiB)...           │   │
│ │      512/48231 MiB (1%)...                                │   │
│ │      1024/48231 MiB (2%)...                               │   │
│ └────────────────────────────────────────────────────────────┘   │
│  [ Cancel / Cleanup ]          [btn margin: 1 1, width: auto]    │
├──────────────────────────────────────────────────────────────────┤
│  q Quit  ^C Quit                                  [Footer]       │
└──────────────────────────────────────────────────────────────────┘
```

**Status bar vs log box** — intentionally decoupled:
- `#status_line` always shows **overall** file count progress
  (`Moved 449/4231 (10%)`), updated via `StatusUpdate` message after
  each completed file, never overwritten by per-chunk log noise.
- `#log_box` receives all `LogLine` messages including per-chunk MiB
  progress for large files, deflation steps, errors, etc.

**Known issues / discussion points:**

1. **Status line height** — `border-bottom` consumes 1 row from `height`,
   so `height: 4` gives the intended 3 visible rows
   (1 empty padding + 1 text + 1 border line).

2. **Cancel button** — floats bottom-left, unanchored. Could be moved
   into the status bar row (right-aligned) to save a vertical row.

3. **Log box double-frame** — `margin: 0 1` + `border: solid` creates two
   visual frames around the same widget. Consider removing the margin
   and keeping only the border.

4. **No elapsed time** — no indication of how long the current step has
   been running. Could be added to the status bar.

5. **No data-volume progress** — status bar shows file count but not
   bytes transferred. For large-file migrations bytes matter more than
   file count.

---

## Deflate Screen

```
┌──────────────────────────────────────────────────────────────────┐
│  Longhorn replica migrator                       [Header 1 row]  │
├──────────────────────────────────────────────────────────────────┤
│                                                  [#deflate_status]│
│  [deflate] fstrim: sending DISCARD for all free blocks...        │  height: 3
│                                                  padding: 1 2 0 2│
├╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┤  border-bottom: warning
│ ┌────────────────────────────────────────────────────────────┐   │
│ │  [pre]  Checking kubectl...                                │   │  #deflate_log
│ │  [1/4]  Applying recovery pod...                          │   │  height: 1fr
│ │  [2/4]  Waiting for pod Running...                        │   │  border: warning
│ │  [3/4]  Waiting for device...                             │   │  margin: 0 1
│ │  [4/4]  Deflating...                                      │   │
│ │      device size: 4096.0 GiB                              │   │
│ │      mounting source for fstrim...                        │   │
│ │      fstrim: /tmp/lrm-trim-xxx: 3891.2 GiB trimmed        │   │
│ │      fstrim freed 1240321 MiB from .img files             │   │
│ │  Deleting recovery pod...                                 │   │
│ │  ✓ Deflation complete                                     │   │
│ └────────────────────────────────────────────────────────────┘   │
│  [ Back ]                      [btn margin: 1 1, width: auto]    │
├──────────────────────────────────────────────────────────────────┤
│  ^C Quit                                          [Footer]       │
└──────────────────────────────────────────────────────────────────┘
```

**Note:** Same structural issue as MigrationScreen — `border-bottom` on
the status area consumes a row from `height: 3`, effectively showing only
2 visible rows. Should be `height: 4` for consistency.

---

## Config Modal

```
┌──────────────────────────────────────────────────────────────────┐
│  (main screen dimmed behind)                                     │
│           ┌──────────────────────────────────────────┐          │
│           │  Configure migration          [#config_panel]       │
│           │  border: heavy $primary       width: 80   │         │
│           │                               height: auto│         │
│           │  Node hostname:                            │         │
│           │  cvps                                      │         │
│           │                                            │         │
│           │  Longhorn engine image:                    │         │
│           │  ┌──────────────────────────────────────┐ │         │
│           │  │ longhornio/longhorn-engine:v1.11.0   │ │         │
│           │  └──────────────────────────────────────┘ │         │
│           │                                            │         │
│           │  Transfer mode:                            │         │
│           │  ○ Copy — safe, keeps source intact        │         │
│           │  ● Move — destructive                      │         │
│           │  ○ Move + Deflate — move & shrink .img     │         │
│           │                                            │         │
│           │  Delete source replica dir after transfer? │         │
│           │  [  OFF  ]                                 │         │
│           │                                            │         │
│           │  ┌──────────────┐  ┌───────────────────┐  │         │
│           │  │    Cancel    │  │  Run Migration →   │  │         │
│           │  └──────────────┘  └───────────────────┘  │         │
│           └──────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────────────────┘
```
