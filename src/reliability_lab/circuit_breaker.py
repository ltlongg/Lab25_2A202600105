from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """3-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED.

    CLOSED:    calls pass through; failure_count accumulates.
    OPEN:      fail fast; after reset_timeout_seconds, probe once via HALF_OPEN.
    HALF_OPEN: allow one probe; success → CLOSED, failure → OPEN immediately.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        OPEN: fail fast unless reset timeout has elapsed, then probe via HALF_OPEN.
        CLOSED / HALF_OPEN: allow the request.
        """
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                return True
            return False
        return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit {self.name} is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Reset failure count; close circuit from HALF_OPEN when probe threshold met."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0

    def record_failure(self) -> None:
        """Increment failure count; open immediately from HALF_OPEN or on threshold breach."""
        self.success_count = 0
        self.failure_count += 1
        if self.state == CircuitState.HALF_OPEN:
            # Probe failed — re-open immediately, don't require threshold
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
            self.failure_count = 0
        elif self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold")
            self.opened_at = time.monotonic()
            self.failure_count = 0

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
