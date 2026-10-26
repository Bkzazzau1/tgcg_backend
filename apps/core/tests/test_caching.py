"""Shared short-lived answers for heavy area summaries (apps/core/caching.py)."""

import pytest
from django.core.cache import cache

from apps.core.caching import remembered


@pytest.fixture
def sharing(settings):
    settings.SUMMARY_CACHE_SECONDS = 30
    cache.clear()
    yield
    cache.clear()


def counter():
    calls = []

    def compute():
        calls.append(1)
        return {"total": len(calls)}

    return calls, compute


def test_an_answer_is_computed_once_and_shared(sharing):
    calls, compute = counter()
    first = remembered("totals", {"scope": "KD"}, compute)
    again = remembered("totals", {"scope": "KD"}, compute)
    assert len(calls) == 1
    assert first == again
    assert first["total"] == 1 and "computed_at" in first
    # Another area is its own answer.
    assert remembered("totals", {"scope": "LA"}, compute)["total"] == 2


def test_switched_off_every_call_computes(settings):
    settings.SUMMARY_CACHE_SECONDS = 0
    calls, compute = counter()
    remembered("totals", {"scope": "KD"}, compute)
    remembered("totals", {"scope": "KD"}, compute)
    assert len(calls) == 2
