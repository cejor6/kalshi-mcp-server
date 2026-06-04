"""Pytest fixtures shared across the suite.

The RSA key here is generated ON DEMAND per test session. We do NOT
commit a real or test PEM file to the repo (secret scanners would flag
it, and there's no need — generation is fast).
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_mcp_server.tools.discovery import _event_hint_misses


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    """A 2048-bit RSA private key, generated once per test session.

    2048 bits is enough for test signing without making the suite slow.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _reset_event_hint_cache():
    """The event-hint negative cache is module-level state; clear it around
    every test (suite-wide) so cross-test ticker reuse can't leak a cached
    verdict. Lives in conftest so it applies to every test file, not just
    test_discovery.py."""
    _event_hint_misses.clear()
    yield
    _event_hint_misses.clear()
