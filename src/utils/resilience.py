"""
Resilience patterns: retry with exponential back-off and circuit breaker.

These wrap any callable so that transient failures (model loading,
embedding computation, I/O) are retried automatically, and persistent
failures trip the circuit breaker to fail fast.
"""

import time
from functools import wraps
from threading import Lock
from typing import Callable, TypeVar

import structlog

from src.settings import (
    CIRCUIT_BREAKER_RESET_TIMEOUT,
    CIRCUIT_BREAKER_THRESHOLD,
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
)

logger = structlog.get_logger(__name__)
T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        reset_timeout: float = CIRCUIT_BREAKER_RESET_TIMEOUT,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._state = "CLOSED"
        self._lock = Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN":
                if time.monotonic() - self._last_failure_time >= self.reset_timeout:
                    self._state = "HALF_OPEN"
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = "CLOSED"

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = "OPEN"
                logger.error("circuit_breaker.open", breaker=self.name,
                             failures=self._failure_count)

    def allow_request(self) -> bool:
        return self.state != "OPEN"


# ── Module-level breaker instances ─────────────────────────────────────────
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = Lock()


def get_breaker(name: str) -> CircuitBreaker:
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name)
        return _breakers[name]


def retry_with_backoff(
    func: Callable[..., T] | None = None,
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    breaker_name: str | None = None,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[..., T]:
    """Decorator: retry with exponential back-off and optional circuit breaker.

    Usage:
        @retry_with_backoff(breaker_name="knowledge_base")
        def query_knowledge_base(query, top_k=3):
            ...

        @retry_with_backoff(max_attempts=5, base_delay=2.0)
        def heavy_operation():
            ...
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        breaker = get_breaker(breaker_name or fn.__qualname__)

        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            if not breaker.allow_request():
                raise CircuitOpenError(
                    f"Circuit breaker '{breaker.name}' is OPEN — "
                    f"failing fast (resets after {breaker.reset_timeout}s)"
                )

            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    result = fn(*args, **kwargs)
                    breaker.record_success()
                    return result
                except retryable_exceptions as exc:
                    last_exc = exc
                    breaker.record_failure()

                    if attempt < max_attempts and breaker.allow_request():
                        delay = base_delay * (2 ** (attempt - 1))
                        logger.warning("retry.attempt",
                                       function=fn.__qualname__,
                                       attempt=attempt,
                                       max_attempts=max_attempts,
                                       delay=delay,
                                       error=str(exc))
                        time.sleep(delay)
                    else:
                        break

            logger.error("retry.exhausted",
                         function=fn.__qualname__,
                         attempts=max_attempts,
                         error=str(last_exc))
            raise last_exc  # type: ignore[misc]

        return wrapper

    if func is not None:
        return decorator(func)
    return decorator
