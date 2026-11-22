"""Contrôle coopératif, utilisable depuis un worker Qt ou une CLI."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .errors import Cancelled


@dataclass(frozen=True)
class Progress:
    phase: str
    path: str
    file_bytes: int
    file_size: int
    completed_bytes: int
    total_bytes: int
    bytes_per_second: float
    remaining_seconds: float | None


class Control:
    def __init__(self, callback: Callable[[Progress], None] | None = None):
        self.callback = callback
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self.started = time.monotonic()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def checkpoint(self) -> None:
        while not self._running.wait(0.1):
            if self._cancel.is_set():
                raise Cancelled("Opération interrompue")
        if self._cancel.is_set():
            raise Cancelled("Opération interrompue")

    def emit(self, phase: str, path: str, count: int, size: int, done: int, total: int) -> None:
        self.checkpoint()
        speed = done / max(time.monotonic() - self.started, 0.001)
        if self.callback:
            self.callback(
                Progress(
                    phase,
                    path,
                    count,
                    size,
                    done,
                    total,
                    speed,
                    (total - done) / speed if speed else None,
                )
            )
