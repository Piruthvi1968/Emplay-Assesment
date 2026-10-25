"""What every agent shares: the context it works with and the node wrapper that traces it and contains errors."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import FieldSpec, Settings
from app.llm_client import LLM
from app.logging_utils import TraceLogger
from app.models import IngestReport
from app.search.engine import SearchEngine

log = logging.getLogger("agents")

_INPUT_KEYS = ("mode", "bid_id", "question", "group", "fields", "fields_todo")


@dataclass
class AgentContext:
    settings: Settings
    engine: SearchEngine  # the search engine is the agents' tool
    llm: LLM
    trace: TraceLogger
    index_folder: Callable[[Path], IngestReport]  # incremental ingest that also refreshes the engine

    @property
    def specs(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.settings.fields}


def _summary(data: dict[str, Any], keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Short, JSON-friendly view of a state/update dict for the trace."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if keys is not None and key not in keys:
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            out[key] = value if not isinstance(value, str) else value[:200]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[key] = value[:25]
        elif isinstance(value, dict | list):
            out[key] = f"{len(value)} items"
        else:
            out[key] = type(value).__name__
    return out


def agent_node(name: str, on_error: Callable[[Any, dict, Exception], dict] | None = None) -> Callable:
    """Wrap an agent method used as a graph node.

    Logs one trace line per step (input, output, status, duration). An exception becomes an entry in
    `errors` plus the agent's fallback update, so one failing agent never crashes the whole run.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def node(self: Any, state: dict) -> dict:
            start = time.perf_counter()
            status, error = "ok", None
            try:
                update = fn(self, state) or {}
            except Exception as exc:
                log.exception("%s failed", name)
                status, error = "error", f"{type(exc).__name__}: {exc}"
                update = {"errors": [f"{name}: {error}"], **(on_error(self, state, exc) if on_error else {})}
            ms = round((time.perf_counter() - start) * 1000)
            self.ctx.trace.log(
                name,
                "node",
                status=status,
                ms=ms,
                input=_summary(state, _INPUT_KEYS),
                output=_summary(update),
                error=error,
            )
            log.info("%-20s %s in %.1fs", name, status, ms / 1000)
            return update

        return node

    return decorator
