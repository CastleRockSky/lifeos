"""Tests for agent_auth.py — agent API key hashing."""

import hashlib

from agent_auth import hash_key


def test_hash_key_is_deterministic():
    assert hash_key("my-secret-key") == hash_key("my-secret-key")


def test_hash_key_matches_sha256():
    # Keys are stored as plain SHA-256 hex digests.
    assert hash_key("test") == hashlib.sha256(b"test").hexdigest()


def test_hash_key_differs_per_input():
    assert hash_key("key-a") != hash_key("key-b")


def test_hash_key_output_shape():
    digest = hash_key("anything")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
