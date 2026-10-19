"""Shared generation/validation path for the endpoint and synthetic live evals."""
import json

from pydantic import ValidationError

from . import engine, provider
from .rubrics import Snapshot


async def generate_turn(state, time_zone, *, attempts=None):
    payload = engine.model_input(state, time_zone)
    system = state['prompt'] + '\n\n' + state['wireInstructions']
    for attempt in range(2):
        result = None
        try:
            result = await provider.generate(system, payload)
            validated = engine.apply_result(state, result)
            if (not validated['paused'] and result.action != 'ask'
                    and validated['result']['action'] == 'ask'):
                snapshot = Snapshot.model_validate(state['rubric'])
                missing = {item['id']: engine.missing_fields(item, snapshot)
                           for item in validated['workItems']}
                raise ValueError('Scope was claimed complete but these fields were omitted: '
                                 + json.dumps(missing) + '. Extract them from existing evidence; '
                                 'only ask a question if the homeowner has not supplied the answer.')
            if attempt == 0 and validated['result']['nextQuestion']:
                # A schema-valid question can still repeat an answered intent.
                # Spend the remaining attempt checking it against the transcript,
                # without relaxing provenance or the total two-call limit.
                if attempts is not None:
                    attempts.append({'validated': True, 'review': 'proposed_question'})
                payload = json.dumps({**json.loads(payload), 'questionReview': {
                    'candidateResponse': result.model_dump(),
                    'proposedQuestions': validated['result']['nextQuestion']}}, ensure_ascii=False)
                system += ('\nReview the proposed question in questionReview against the entire homeowner transcript. '
                           'Check whether existing words already answer it or an equivalent field. '
                           'If answered, extract the cited answer and continue readiness without asking again. '
                           'If genuinely unanswered, retain a useful clarifier. Do not guess, invent a preference '
                           'or turn missing information into uncertainty. Preserve all supported candidate answers. '
                           'Return the complete final object.')
                continue
            if attempts is not None:
                attempts.append({'validated': True})
            return validated
        except (ValueError, ValidationError) as exc:
            if attempts is not None:
                attempts.append({'validated': False, 'error': str(exc)[:500]})
            if attempt:
                raise
            if result is not None:
                payload = json.dumps({**json.loads(payload), 'validationFeedback': {
                    'error': str(exc)[:500], 'previousInvalidResponse': result.model_dump()}}, ensure_ascii=False)
            system += ('\nThe previous response failed application validation: '
                       + str(exc)[:500] + '. The previous object, if available, is in validationFeedback as data. '
                       'Preserve its supported answers and correct the invalid fields. Return a complete object.')
