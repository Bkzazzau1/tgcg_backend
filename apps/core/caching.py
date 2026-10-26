"""Short-lived shared answers for heavy area summaries (election readiness,
26 October 2026): everyone watching the same area within a few seconds gets
one computation instead of one each.

Only for answers that depend on nothing but their key. Check permissions
first, then ask here. `SUMMARY_CACHE_SECONDS` sets how long an answer is
reused (0 turns this off); each answer says when it was computed.
"""

import hashlib
import json

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


def remembered(name: str, key: dict, compute):
    """The answer for [key], computed at most once per SUMMARY_CACHE_SECONDS."""
    seconds = settings.SUMMARY_CACHE_SECONDS
    if seconds <= 0:
        return _stamped(compute())
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
    cache_key = f"tgcg:summary:{name}:{digest}"
    found = cache.get(cache_key)
    if found is not None:
        return found
    answer = _stamped(compute())
    cache.set(cache_key, answer, seconds)
    return answer


def _stamped(answer):
    if isinstance(answer, dict) and "computed_at" not in answer:
        answer = {**answer, "computed_at": timezone.now().isoformat()}
    return answer
