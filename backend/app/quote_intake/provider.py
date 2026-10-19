"""Independent text-only Anthropic transport; never calls compact_schema."""
import logging
import time
import anthropic
from ..config import settings
from .schema import Result

logger = logging.getLogger(__name__)


async def generate(system: str, payload: str) -> Result:
    if not settings.anthropic_api_key:
        raise RuntimeError('Quote intake Anthropic credentials are not configured')
    started = time.monotonic()
    async with anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key,
            timeout=float(settings.anthropic_request_timeout_seconds), max_retries=0) as client:
        response = await client.messages.parse(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text': payload}]}],
            output_format=Result,
            output_config={'effort': settings.anthropic_effort} if settings.anthropic_effort else {},
        )
    if response.stop_reason != 'end_turn' or response.parsed_output is None:
        raise ValueError('Incomplete or refused structured response')
    logger.info('quote_intake model=%s request=%s input_tokens=%s output_tokens=%s seconds=%.2f',
                response.model, response._request_id, response.usage.input_tokens,
                response.usage.output_tokens, time.monotonic() - started)
    return response.parsed_output
