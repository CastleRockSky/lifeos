"""Tests for rate_limit.py — the in-memory per-key token bucket."""

import uuid

from rate_limit import take


def _fresh_key():
    """A unique key so buckets never collide across tests."""
    return f"test-{uuid.uuid4()}"


def test_zero_limit_never_blocks():
    key = _fresh_key()
    for _ in range(100):
        assert take(key, 0) is True


def test_negative_limit_never_blocks():
    assert take(_fresh_key(), -5) is True


def test_limit_is_enforced():
    key = _fresh_key()
    assert take(key, 3) is True
    assert take(key, 3) is True
    assert take(key, 3) is True
    assert take(key, 3) is False  # 4th call in the same minute is rejected


def test_keys_are_independent():
    key_a, key_b = _fresh_key(), _fresh_key()
    assert take(key_a, 1) is True
    assert take(key_a, 1) is False
    # A different key has its own fresh bucket.
    assert take(key_b, 1) is True
