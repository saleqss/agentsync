"""Unified thread-safe progress event bus shared by Textual TUI workers and headless Rich CLI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

EventLevel = Literal["info", "success", "warning", "error"]


class ProgressEvent(BaseModel):
    """Structured step-by-step progress notification emitted by core operations."""

    phase: str
    step_index: int = 0
    total_steps: int = 1
    message_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    percent: float = 0.0
    item_name: str = ""
    level: EventLevel = "info"


class CancelledError(RuntimeError):
    """Raised when a user cancels an active background scan/patch/sync/handoff worker."""


class ProgressBus:
    """Thread-safe pub/sub event bus with cooperative cancellation support."""

    def __init__(self, listener: Callable[[ProgressEvent], None] | None = None) -> None:
        self._listeners: list[Callable[[ProgressEvent], None]] = []
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        if listener is not None:
            self._listeners.append(listener)

    def subscribe(self, listener: Callable[[ProgressEvent], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def cancel(self) -> None:
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        self._cancel_event.clear()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise CancelledError("Operation cancelled by user.")

    def emit(
        self,
        phase: str,
        message_key: str,
        *,
        step_index: int = 0,
        total_steps: int = 1,
        percent: float | None = None,
        item_name: str = "",
        level: EventLevel = "info",
        **params: Any,
    ) -> ProgressEvent:
        pct = (
            percent
            if percent is not None
            else (min(100.0, max(0.0, (step_index / max(1, total_steps)) * 100.0)))
        )
        ev = ProgressEvent(
            phase=phase,
            step_index=step_index,
            total_steps=total_steps,
            message_key=message_key,
            params=params,
            percent=round(pct, 1),
            item_name=item_name,
            level=level,
        )
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(ev)
            except Exception:
                continue
        return ev
