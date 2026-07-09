"""Shared test fixtures."""

import pytest

from app.core import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear the in-process rate-limit counters before each test.

    The limiter is module-level global state keyed by API key; without this reset,
    counts from earlier tests that share a key would accumulate and could trip the
    limit in a later, unrelated test.
    """
    rate_limit.reset()
    yield
    rate_limit.reset()
