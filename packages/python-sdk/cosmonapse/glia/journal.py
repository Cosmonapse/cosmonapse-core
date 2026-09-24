"""
cosmonapse.glia.journal
~~~~~~~~~~~~~~~~~~~~~~~
The component's own append-only audit sink.

Two records answer two different questions. On the bus, thin:
``meta.glia`` on the signal the verdict shaped, carrying verdict,
policy id, policy version, direction, signal type, component identity
and a hash.
Local, full: this journal, carrying the matched content. It never
crosses the Synapse.

Format is newline-delimited JSON, one object per line, opened in append
mode. No collector ships in core: shipping the file to wherever
compliance lives is the deployment's job, and an ingest endpoint in core
would make every component depend on a reachable observer, which is a
centralised coupling in a decentralised design.

Writes are best effort. A journal that cannot be written records the
failure and gets out of the way; it must never take the component down.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Rotate (truncate to a ``.1`` sibling) past this size when the card
#: asked for a bound. Unset means grow forever, which is the honest
#: default for an audit log.
DEFAULT_MAX_BYTES: int | None = None


class Journal:
    """Append-only JSONL sink for one component's policy records."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = None if max_bytes is None else int(max_bytes)
        self.written = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        parent = self.path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._ready = True

    def record(self, entry: dict[str, Any]) -> None:
        """Append one record. Raises only what the caller already guards."""
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock:
            try:
                self._ensure()
                self._rotate_if_needed(len(line) + 1)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.write("\n")
            except OSError:
                self.failed += 1
                logger.warning(
                    "policy journal write failed for %s", self.path,
                    exc_info=True,
                )
                return
            self.written += 1

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.max_bytes is None:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        backup = self.path.with_suffix(f"{self.path.suffix}.1")
        try:
            self.path.replace(backup)
        except OSError:
            logger.warning(
                "policy journal rotation failed for %s", self.path,
                exc_info=True,
            )

    def read_all(self) -> list[dict[str, Any]]:
        """Every record written so far. For tests and for a collector the
        deployment writes; core does not ship one."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "jsonl",
            "path": str(self.path),
            "max_bytes": self.max_bytes,
            "written": self.written,
            "failed": self.failed,
        }

    def __repr__(self) -> str:
        return f"Journal(path={str(self.path)!r}, written={self.written})"
