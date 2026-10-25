"""The audit log: one JSON object per guard decision, append-only.

Every payment the guard allows, warns on, or blocks leaves a record
the buyer can read: what was about to be paid, which stages ran, what
each one found, what the checks themselves cost, and the final
decision with its reasons. If the guard ever stops a payment you
needed, this file is where it explains itself.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        line = json.dumps(entry, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
