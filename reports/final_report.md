# Day 10 Reliability Report

## 1. Architecture summary

The `ReliabilityGateway` is the central entry point for every LLM request. It layers four
reliability mechanisms — semantic cache, circuit breakers, fallback chain, and a static
last-resort response — so that **any single point of failure cannot bring the system down**.

```
User Request
    |
    v
[ReliabilityGateway.complete(prompt)]
    |
    +---> [Cache check: ResponseCache / SharedRedisCache]
    |          TF-IDF + Jaccard similarity >= threshold?
    |          Privacy guard (_is_uncacheable)?
    |          False-hit guard (_looks_like_false_hit)?
    |              YES → return cached response  (route: "cache_hit:<score>")
    |              NO  ↓
    |
    +---> [CircuitBreaker: primary]
    |          state == OPEN?  skip provider
    |          state == CLOSED / HALF_OPEN → call FakeLLMProvider("primary")
    |              success → record_success(), cache result
    |                          (route: "primary:primary")
    |              failure → record_failure()
    |                  if threshold reached → CB transitions CLOSED→OPEN
    |                  if HALF_OPEN probe fails → CB transitions HALF_OPEN→OPEN
    |
    +---> [CircuitBreaker: backup]  (same logic, lower fail_rate)
    |          (route: "fallback:backup")
    |
    +---> [Static fallback message]
               "I'm temporarily unavailable…"
               (route: "static_fallback")
```

**State machine** for each CircuitBreaker:

```
CLOSED ──(failures >= threshold)──► OPEN
  ▲                                    |
  │                           (reset_timeout elapsed)
  │                                    ▼
  └──(success_count >= success_threshold)── HALF_OPEN
```

Privacy guardrail: any query matching PII patterns (balance, password, SSN, credit card,
account/user IDs) is **never** cached in either in-memory or Redis backends.

False-hit guardrail: a cache hit is suppressed when the query and cached key contain
different 4-digit numbers (years, IDs), preventing stale date-sensitive answers.

---

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Tolerates 2 transient errors before opening; small enough for fast reaction |
| reset_timeout_seconds | 2 | Short probe window — FakeLLMProvider recovers quickly in tests |
| success_threshold | 1 | Single successful probe closes the circuit immediately |
| cache TTL | 300 s | 5 minutes covers repeated bursts without serving stale LLM facts |
| similarity_threshold | 0.92 | High threshold prevents false hits; TF-IDF + Jaccard handles near-identical queries |
| load_test requests | 100 per scenario | 400 total across 4 scenarios — statistically significant while keeping runtime short |

---

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.5% | ✅ |
| Latency P95 | < 2500 ms | 484.88 ms | ✅ |
| Fallback success rate | >= 95% | 95.56% | ✅ |
| Cache hit rate | >= 10% | 77.25% | ✅ |
| Recovery time | < 5000 ms | 2312.95 ms | ✅ |

All five SLOs are met. The system is particularly strong on cache hit rate (77.25%) and
latency P95 (484.88 ms), both well within targets.

---

## 4. Metrics

Full run across 4 chaos scenarios (400 total requests):

| Metric | Value |
|---|---:|
| availability | 0.9950 (99.5%) |
| error_rate | 0.0050 (0.5%) |
| latency_p50_ms | 0.22 |
| latency_p95_ms | 484.88 |
| latency_p99_ms | 528.90 |
| fallback_success_rate | 0.9556 (95.56%) |
| cache_hit_rate | 0.7725 (77.25%) |
| estimated_cost | $0.04271 |
| estimated_cost_saved | $0.309 |
| circuit_open_count | 4 |
| recovery_time_ms | 2312.95 |

The very low P50 (0.22 ms) reflects ~77% of requests being served from the in-memory cache
with sub-millisecond lookups. P95/P99 represent real provider calls (180–260 ms base
latency + occasional slow paths).

---

## 5. Cache comparison

Measured on the `all_healthy` scenario (100 requests, both providers at 0% fail rate):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 230.59 | 0.57 | -99.8% |
| latency_p95_ms | 515.48 | 250.86 | -51.4% |
| estimated_cost | $0.0489 | $0.0121 | -75.3% |
| cache_hit_rate | 0.0 | 0.78 | +78 pp |

The cache delivers a 75% cost reduction and nearly eliminates median latency by serving
repeated or semantically-similar queries locally. High similarity threshold (0.92) keeps
precision high — no false hits observed in the healthy baseline.

---

## 6. Redis shared cache

### Why in-memory cache is insufficient for multi-instance deployments

`ResponseCache` stores entries in a Python list (`self._entries`) that lives inside a single
process. When multiple gateway instances run (e.g., behind a load balancer), each process
builds its own private cache. A query answered by instance A is unknown to instance B — so
**every instance cold-starts on every unique query**, negating cost and latency savings.
Worse, after a deployment restart the entire warm cache is lost.

### How `SharedRedisCache` solves this

`SharedRedisCache` stores query→response pairs in a Redis hash with a consistent MD5-based
key (`rl:cache:<hash12>`). Because all instances connect to the same Redis server:

- **Warm-up is shared** — one instance's cache hit benefits all others immediately.
- **TTL is enforced server-side** — `EXPIRE` ensures stale responses are evicted uniformly
  across the fleet without each process running its own TTL logic.
- **Privacy and false-hit guards** remain in the client layer — no sensitive data ever
  reaches Redis.
- **Graceful degradation** — all Redis calls are wrapped in `try/except`; a Redis outage
  transparently falls through to the provider chain.

### Evidence of shared state

Two separate `SharedRedisCache` instances (different Python objects, same Redis URL)
demonstrate shared state:

```python
# Instance 1 writes three entries
c1 = SharedRedisCache("redis://localhost:6379/0", ttl=300, threshold=0.5)
c1.set("What is the capital of France?",   "Paris is the capital of France.")
c1.set("How does a circuit breaker work?", "A circuit breaker monitors failures...")
c1.set("What is machine learning?",        "Machine learning is a subset of AI...")

# Instance 2 (separate object, same Redis) reads without any prior writes
c2 = SharedRedisCache("redis://localhost:6379/0", ttl=300, threshold=0.5)

val, score = c2.get("What is the capital of France?")
# → score=1.00, value='Paris is the capital of France.'   ✅ exact hit

val, score = c2.get("How does machine learning work?")
# → score=0.38 (below threshold=0.5), value=None          ✅ correctly below threshold
```

Keys written by instance 1 are immediately visible to instance 2: **3 keys shared**.

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:0c9fdf1c9bd7
rl:cache:cfc5e751cb29
rl:cache:7bd4e893e89a
```

Three entries stored using MD5-prefixed keys under the `rl:cache:` namespace, each a Redis
hash with `query` and `response` fields plus server-side TTL.

### In-memory vs Redis latency comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms (cache hits) | ~0.20 ms | ~0.57 ms | Network RTT to localhost Redis |
| latency_p95_ms (mixed) | ~250 ms | ~251 ms | Provider calls dominate P95 |

Redis adds ~0.4 ms per cache hit (local loopback RTT). For multi-instance production
deployments the warm-cache benefit across the fleet far outweighs this overhead.

---

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic fallback to backup; circuit opens ≥ 1× | 100% fallback to backup; circuit opened 2× | ✅ pass |
| primary_flaky_50 | Circuit oscillates; mix of primary + fallback routes | Circuit opened 1×; ~50% primary, ~50% fallback | ✅ pass |
| cache_stale_candidate | Cache serves hits; false-hit guard blocks year mismatches | Cache served; false-hit guard fired on date-diff queries | ✅ pass |
| all_healthy | Near-perfect availability; no static fallbacks | 99%+ availability; zero static fallbacks | ✅ pass |
| cache_vs_nocache | Cached run has higher hit rate than no-cache baseline | 77.25% vs 0% — cost saved $0.309 | ✅ pass |

All 5 scenarios pass. The `cache_stale_candidate` scenario validated the combined
TF-IDF + Jaccard similarity approach: without the Jaccard fallback, shared-token queries
(e.g., "refund policy for 2024" vs "refund policy for 2026") scored 0.0 TF-IDF and
bypassed the false-hit guard entirely. The hybrid scorer surfaces the true semantic
similarity, then the false-hit guard correctly suppresses the stale response.

---

## 8. Failure analysis

**Remaining weakness: in-memory circuit breaker state is not shared across instances.**

Each gateway instance maintains its own `CircuitBreaker` objects. In a horizontally scaled
deployment, instance A may have tripped its primary circuit breaker after observing three
failures, while instances B and C are still sending traffic to the failing provider — they
have no visibility into A's state. This means:

- The failing provider continues to receive traffic from "healthy" instances even while A's
  circuit is open.
- During a partial outage, the fleet-wide error rate stays elevated longer than necessary.
- Recovery probing is uncoordinated — all instances probe independently, multiplying probe
  traffic and potentially overwhelming a recovering provider.

**Fix:** Store circuit breaker state in Redis using a hash per circuit
(`cb:<name>:state`, `cb:<name>:failure_count`, `cb:<name>:last_open_ts`). Protect counter
increments with Redis atomic operations (`INCR`, Lua scripts for compare-and-swap). A
single OPEN event in any instance propagates to the entire fleet within milliseconds, and
recovery probing can be gated to one instance at a time using a Redis lock (`SET NX EX`).

---

## 9. Next steps

1. **Distributed circuit breaker state via Redis** — move `failure_count`, `state`, and
   `last_open_ts` into Redis so all gateway instances share the same circuit state. Use a
   Lua script for atomic CAS on state transitions to avoid split-brain during concurrent
   failures.

2. **Per-query quality SLO** — add a post-call validator that scores LLM responses
   (length, JSON validity, keyword presence). Treat a low-quality response as a soft
   failure so the circuit breaker can also open on degraded — not just errored — providers.

3. **Adaptive similarity threshold** — dynamically lower `similarity_threshold` during
   high-load periods (circuit open, cache cold) and raise it during steady state. This
   increases cache reuse under stress while maintaining precision when the system is healthy.
