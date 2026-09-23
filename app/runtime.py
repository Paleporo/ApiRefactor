"""Budget di tempo della run: controllato tra le fasi e usato per limitare il timeout di ogni chiamata LLM."""

from __future__ import annotations

import time

from app.errors import RunTimeoutError


class Deadline:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.started = time.monotonic()

    @property
    def remaining(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self, phase: str) -> None:
        if self.remaining <= 0:
            raise RunTimeoutError(f"Budget complessivo della run ({self.seconds:.0f}s) esaurito durante la fase {phase}")
