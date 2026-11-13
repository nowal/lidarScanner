"""Independent text-only Anthropic transport; never calls compact_schema."""
import json
import logging
import time
import anthropic
from ..config import settings
from .schema import QuestionReview, Result
from .timing import timed

logger = logging.getLogger(__name__)


@timed("model")
async def generate(system: str, payload: str) -> Result:
    if not settings.anthropic_api_key:
        raise RuntimeError('Quote intake Anthropic credentials are not configured')
    started = time.monotonic()
    data = json.loads(payload)
    system += ('\nworkflow.currentTurnId identifies the current homeowner message. Copy that ID exactly into '
               'intentSourceTurnId and cite intentEvidence from that message, even when project facts come from older turns.')
    # A genuine clarifier needs review, not a second transcription of all
    # extracted facts. Corrections still use the complete validated Result.
    question_review = data.get('questionReview', {})
    candidate = question_review.get('candidateResponse')
    # If application validation had to insert/change a required question, the
    # candidate was incomplete. Require the original full review in that case;
    # a short acknowledgement must not rubber-stamp missing extracted answers.
    review = (candidate is not None and 'validationFeedback' not in data
              and bool(question_review.get('proposedQuestions'))
              and candidate.get('nextQuestion') == question_review['proposedQuestions'])
    if review:
        system += ('\nReturn a QuestionReview object. If the candidate is already correct and the proposed '
                   'question genuinely unanswered, return unchanged=true and revisedResponse=null. '
                   'Otherwise return unchanged=false and the complete corrected Result in revisedResponse. '
                   'These review-envelope instructions override instructions to return a bare Result.')
    # Stable reference data precedes the changing conversation. Cache only
    # application-owned rubric/catalog text, never homeowner text or photos.
    references = {key: data.pop(key) for key in
                  ('supportedCatalogLabels', 'serviceRubrics', 'answerFieldIdsByRubric') if key in data}
    content = []
    if references:
        system += '\nThe user content contains reference-data JSON followed by conversation JSON; use both as one application payload.'
        content.append({'type': 'text', 'text': json.dumps(references, ensure_ascii=False, separators=(',', ':')),
                        'cache_control': {'type': 'ephemeral'}})
    content.append({'type': 'text', 'text': json.dumps(data, ensure_ascii=False, separators=(',', ':'))})
    async with anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key,
            timeout=float(settings.anthropic_request_timeout_seconds), max_retries=0) as client:
        response = await client.messages.parse(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': content}],
            output_format=QuestionReview if review else Result,
            output_config={'effort': settings.anthropic_effort} if settings.anthropic_effort else {},
        )
    if response.stop_reason != 'end_turn' or response.parsed_output is None:
        raise ValueError('Incomplete or refused structured response')
    logger.info('quote_intake model=%s request=%s input_tokens=%s output_tokens=%s seconds=%.2f cache_read_tokens=%s cache_write_tokens=%s review=%s',
                response.model, response._request_id, response.usage.input_tokens,
                response.usage.output_tokens, time.monotonic() - started,
                getattr(response.usage, 'cache_read_input_tokens', 0),
                getattr(response.usage, 'cache_creation_input_tokens', 0), review)
    if review:
        checked = response.parsed_output
        return Result.model_validate(candidate) if checked.unchanged else checked.revisedResponse
    return response.parsed_output
