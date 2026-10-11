from .engine import _request_cache


class AccessCacheMiddleware:
    """Gives each request a fresh cache of computed permissions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_cache.set({})
        try:
            return self.get_response(request)
        finally:
            _request_cache.reset(token)
