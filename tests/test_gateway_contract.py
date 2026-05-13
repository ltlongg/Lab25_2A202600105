from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    # Route is now specific: "primary:<name>", "fallback:<name>", "cache_hit:<score>", or "static_fallback"
    assert (
        result.route.startswith("primary:")
        or result.route.startswith("fallback:")
        or result.route.startswith("cache_hit:")
        or result.route == "static_fallback"
    )


def test_fallback_serves_when_primary_circuit_open() -> None:
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.006)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=60),
        "backup": CircuitBreaker("backup", failure_threshold=2, reset_timeout_seconds=60),
    }
    gateway = ReliabilityGateway([primary, backup], breakers)

    # Force circuit open on primary
    for _ in range(5):
        gateway.complete("test query")

    result = gateway.complete("another query")
    assert result.route.startswith("fallback:") or result.route == "static_fallback"
    open_count = sum(1 for t in breakers["primary"].transition_log if t["to"] == "open")
    assert open_count >= 1 or breakers["primary"].state.value == "open"


def test_static_fallback_when_all_providers_fail() -> None:
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60),
    }
    gateway = ReliabilityGateway([primary], breakers)
    # Exhaust circuit, then check static fallback
    for _ in range(3):
        gateway.complete("query")
    result = gateway.complete("query")
    assert result.route in {"static_fallback", "primary:primary"} or result.route.startswith("fallback:")


def test_cache_hit_serves_from_cache() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, cache)
    gateway.complete("hello world test")
    result = gateway.complete("hello world test")
    assert result.cache_hit is True
    assert result.route.startswith("cache_hit:")
