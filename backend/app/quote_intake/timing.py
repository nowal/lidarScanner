"""Request-local aggregate timings; never include text, identities or tokens."""
from contextvars import ContextVar
from functools import wraps
import logging
import time

logger = logging.getLogger(__name__)
_current: ContextVar[dict | None] = ContextVar('quote_intake_timings', default=None)


def timed(stage):
    def decorate(function):
        @wraps(function)
        async def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await function(*args, **kwargs)
            finally:
                values = _current.get()
                if values is not None:
                    values[stage] = values.get(stage, 0) + (time.perf_counter() - started) * 1000
        return measured
    return decorate


class QuoteIntakeTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or '/ai/quote-intake' not in path:
            return await self.app(scope, receive, send)
        suffix = path.split('/ai/quote-intake', 1)[1]
        operation = suffix if suffix in ('/active', '/turn', '/submit', '/threads', '/threads/new', '/threads/select', '/start-next') else '/restore'
        values = {}
        token = _current.set(values)
        started = time.perf_counter()
        status = 500

        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                values['total'] = (time.perf_counter() - started) * 1000
                header = ', '.join(f'{key};dur={value:.1f}' for key, value in sorted(values.items()))
                message = {**message, 'headers': [*message.get('headers', []), (b'server-timing', header.encode('ascii'))]}
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            logger.info('quote_intake_request operation=%s status=%s timings_ms=%s',
                        operation, status, {key: round(value, 1) for key, value in values.items()})
            _current.reset(token)
