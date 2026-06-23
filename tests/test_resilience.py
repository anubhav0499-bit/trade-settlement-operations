"""Stress tests for circuit breaker and retry resilience patterns."""

import time
import pytest

from src.utils.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    get_breaker,
    retry_with_backoff,
)


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, reset_timeout=10)
        assert cb.state == "CLOSED"
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3, reset_timeout=10)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "CLOSED"
        cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.allow_request() is False

    def test_success_resets_count(self):
        cb = CircuitBreaker("test", failure_threshold=3, reset_timeout=10)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.state == "CLOSED"

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=0.1)
        cb.record_failure()
        assert cb.state == "OPEN"
        time.sleep(0.15)
        assert cb.state == "HALF_OPEN"
        assert cb.allow_request() is True

    def test_half_open_to_closed_on_success(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == "HALF_OPEN"
        cb.record_success()
        assert cb.state == "CLOSED"

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == "HALF_OPEN"
        cb.record_failure()
        assert cb.state == "OPEN"


class TestGetBreaker:
    def test_creates_new_breaker(self):
        breaker = get_breaker("unique_test_name_1")
        assert isinstance(breaker, CircuitBreaker)
        assert breaker.name == "unique_test_name_1"

    def test_returns_same_instance(self):
        b1 = get_breaker("unique_test_name_2")
        b2 = get_breaker("unique_test_name_2")
        assert b1 is b2


class TestRetryWithBackoff:
    def test_success_on_first_try(self):
        call_count = 0

        @retry_with_backoff(max_attempts=3, base_delay=0.01, breaker_name="test_ok")
        def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert good_func() == "ok"
        assert call_count == 1

    def test_retries_on_failure(self):
        call_count = 0

        @retry_with_backoff(max_attempts=3, base_delay=0.01, breaker_name="test_retry_1")
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        assert flaky_func() == "ok"
        assert call_count == 3

    def test_exhausted_retries_raises(self):
        @retry_with_backoff(max_attempts=2, base_delay=0.01, breaker_name="test_exhaust_1")
        def always_fail():
            raise ValueError("always")

        with pytest.raises(ValueError, match="always"):
            always_fail()

    def test_circuit_open_raises_immediately(self):
        breaker = get_breaker("test_open_circuit")
        for _ in range(breaker.failure_threshold + 1):
            breaker.record_failure()

        @retry_with_backoff(max_attempts=3, base_delay=0.01, breaker_name="test_open_circuit")
        def wont_run():
            return "unreachable"

        with pytest.raises(CircuitOpenError):
            wont_run()

    def test_retryable_exceptions_filter(self):
        call_count = 0

        @retry_with_backoff(
            max_attempts=3,
            base_delay=0.01,
            breaker_name="test_filter_1",
            retryable_exceptions=(ValueError,),
        )
        def type_error_func():
            nonlocal call_count
            call_count += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError):
            type_error_func()
        assert call_count == 1

    def test_decorator_without_parens(self):
        @retry_with_backoff
        def simple_func():
            return 42

        assert simple_func() == 42
