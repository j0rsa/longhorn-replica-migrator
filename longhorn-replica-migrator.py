#!/usr/bin/env python3
"""Launch the Longhorn replica migrator TUI.

Usage:
  ./longhorn-replica-migrator.py /var/lib/longhorn/replicas
  python longhorn-replica-migrator.py /path/to/replicas --dev-root /dev/longhorn

See also: recovery-pod-example.yaml (launch-simple-longhorn recovery pod).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from replica_migrator.main import run  # noqa: E402

if __name__ == "__main__":
    run()
