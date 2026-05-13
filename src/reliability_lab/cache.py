from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from math import log, sqrt
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


def _jaccard_similarity(a: str, b: str) -> float:
    """Token-level Jaccard similarity — shared tokens / union of tokens."""
    left = set(re.findall(r"\b\w+\b", a.lower()))
    right = set(re.findall(r"\b\w+\b", b.lower()))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _tfidf_similarity(a: str, b: str) -> float:
    """TF-IDF cosine similarity with Jaccard fallback for near-identical strings.

    TF-IDF alone scores 0 when all tokens are shared (IDF=0). In that case
    we fall back to Jaccard, which correctly scores near-identical strings high
    and lets _looks_like_false_hit() catch year/ID mismatches downstream.
    """
    if a.lower().strip() == b.lower().strip():
        return 1.0

    def tokenize(text: str) -> list[str]:
        return re.findall(r"\b\w+\b", text.lower())

    tokens_a = tokenize(a)
    tokens_b = tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0

    tf_a = Counter(tokens_a)
    tf_b = Counter(tokens_b)
    vocab = set(tf_a) | set(tf_b)

    # IDF: log(2 / df) — small 2-doc corpus
    def idf(term: str) -> float:
        df = (1 if term in tf_a else 0) + (1 if term in tf_b else 0)
        return log(2.0 / df)

    vec_a = {t: tf_a[t] * idf(t) for t in vocab}
    vec_b = {t: tf_b[t] * idf(t) for t in vocab}

    dot = sum(vec_a[t] * vec_b[t] for t in vocab)
    mag_a = sqrt(sum(v * v for v in vec_a.values()))
    mag_b = sqrt(sum(v * v for v in vec_b.values()))

    if mag_a == 0 or mag_b == 0:
        # All tokens shared → TF-IDF collapses; fall back to Jaccard
        return _jaccard_similarity(a, b)

    tfidf_score = dot / (mag_a * mag_b)
    # If TF-IDF gives near-zero but strings are obviously similar, use Jaccard
    if tfidf_score < 0.01:
        return _jaccard_similarity(a, b)
    return tfidf_score


class ResponseCache:
    """In-memory cache with TF-IDF similarity, privacy guardrails, and false-hit detection."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        best_value: str | None = None
        best_score = 0.0
        best_key: str | None = None
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key

        if best_score >= self.similarity_threshold and best_key is not None:
            if _looks_like_false_hit(query, best_key):
                self.false_hit_log.append({"query": query, "cached_key": best_key, "score": best_score})
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """TF-IDF cosine similarity with exact-match fast path."""
        return _tfidf_similarity(a, b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        try:
            # Step 1: exact-match lookup
            key = f"{self.prefix}{self._query_hash(query)}"
            response = self._redis.hget(key, "response")
            if response is not None:
                return response, 1.0

            # Step 2: similarity scan
            best_value: str | None = None
            best_score = 0.0
            best_cached_query: str | None = None

            for scan_key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(scan_key, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_cached_query = cached_query
                    best_value = self._redis.hget(scan_key, "response")

            if best_score >= self.similarity_threshold and best_cached_query is not None:
                if _looks_like_false_hit(query, best_cached_query):
                    self.false_hit_log.append({"query": query, "cached_key": best_cached_query, "score": best_score})
                    return None, best_score
                return best_value, best_score

            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
