"""Shared test fixtures."""

import pytest

from app.core import rate_limit
from app.services.normalization import city_resolver


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


@pytest.fixture(autouse=True)
def _reset_city_cache():
    """Clear the cross-resume city lookup cache before each test.

    The cache is process-global ON PURPOSE - it is what stops a warm Lambda from
    re-spending monthly partner quota on the same cities over and over. That also
    means it survives between tests: without this reset, a city resolved by one test
    is served from cache in the next, which silently turns an "did we call the API?"
    assertion into a false pass.
    """
    city_resolver._reset_cache()
    yield
    city_resolver._reset_cache()
