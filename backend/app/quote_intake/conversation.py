"""Shared generation/validation path for the endpoint and synthetic live evals."""
import json

from pydantic import ValidationError

from . import engine, provider

MAX_GENERATION_ATTEMPTS = 3

async def generate_turn(state, time_zone, *, attempts=None):
    payload = engine.model_input(state, time_zone)
    system = state['prompt'] + '\n\n' + state['wireInstructions']
    question_reviewed = False
    # Ordinary validation gets one repair (two calls total). A proposed-question
    # review may itself introduce an invalid citation, so that path alone gets a
    # final repair call rather than surfacing a retryable failure to the homeowner.
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        result = None
        try:
            result = await provider.generate(system, payload)
            validated = engine.apply_result(state, result)
            # Application readiness is authoritative. If the model claims
            # completion while omitting a required homeowner decision,
            # apply_result supplies the rubric's next question deterministically.
            # Accept that safe fallback instead of spending the only repair
            # attempt asking the model to rediscover an answer that may not exist.
            if attempt == 0 and result.action == 'ask' and validated['result']['nextQuestion']:
                # A schema-valid question can still repeat an answered intent.
                # Spend the remaining attempt checking it against the transcript,
                # without relaxing provenance; a failed review gets one repair.
                if attempts is not None:
                    attempts.append({'validated': True, 'review': 'proposed_question'})
                question_reviewed = True
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
            if attempt >= 2 or (attempt >= 1 and not question_reviewed):
                raise
            if result is not None:
                payload = json.dumps({**json.loads(payload), 'validationFeedback': {
                    'error': str(exc)[:500], 'previousInvalidResponse': result.model_dump()}}, ensure_ascii=False)
            system += ('\nThe previous response failed application validation: '
                       + str(exc)[:500] + '. The previous object, if available, is in validationFeedback as data. '
                       'Preserve its supported answers and correct the invalid fields. Return a complete object.')
