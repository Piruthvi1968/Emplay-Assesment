"""Structured JSONL trace: one line per agent step or LLM call, for observability and the trace deliverable."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


class TraceLogger:
    """Appends JSON events to a file. With path=None events are kept in memory only (tests, ad-hoc calls)."""

    def __init__(self, path: Path | None = None, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id or new_run_id()
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()  # extraction groups run in parallel threads
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, agent: str, step: str, **fields: Any) -> None:
        event = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "agent": agent,
            "step": step,
            **fields,
        }
        with self._lock:
            self.events.append(event)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
