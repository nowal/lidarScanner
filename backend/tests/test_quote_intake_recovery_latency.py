"""Recovery authorization, review transport and request timing regressions."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app import flow_api
from app.config import settings
from app.flow import home_registry, supabase_store
from app.quote_intake import engine, provider
from app.quote_intake.schema import QuestionReview, Result
from test_quote_intake import result, state_for
from test_staged_scan_uploads import HOME, OWNER, staged


@pytest.mark.asyncio
@pytest.mark.parametrize('registered', [OWNER, 'foreign', None])
async def test_authorize_durable_scan_without_loading_full_index(staged, monkeypatch, registered):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    async def owner(home):
        assert home == HOME
        return registered
    async def index(home):
        assert registered is None, 'Registered scan must not hydrate the index'
        return None
    monkeypatch.setattr(supabase_store, 'registered_scan_owner', owner)
    monkeypatch.setattr(home_registry, 'load_index_async', index)
    if registered == 'foreign':
        with pytest.raises(HTTPException) as error:
            await flow_api._scan_upload_owner(HOME, 'valid')
        assert error.value.status_code == 403
    else:
        assert await flow_api._scan_upload_owner(HOME, 'valid') == OWNER


@pytest.mark.asyncio
@pytest.mark.parametrize('owners,status', [(set(), 409), ({'foreign'}, 403), ({OWNER, 'foreign'}, 403)])
async def test_only_unregistered_ownership_is_retryable(staged, monkeypatch, owners, status):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: False)
    async def index(home): return None
    async def records(home): return owners
    monkeypatch.setattr(home_registry, 'load_index_async', index)
    monkeypatch.setattr(supabase_store, 'scan_upload_owners', records)
    with pytest.raises(HTTPException) as error:
        await flow_api._scan_upload_owner(HOME, 'valid')
    assert error.value.status_code == status
    if status == 409:
        assert error.value.detail['code'] == 'scan_ownership_pending'


@pytest.mark.asyncio
async def test_failed_owner_read_never_falls_back_to_stale_scan(staged, monkeypatch):
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    async def unavailable(home): raise RuntimeError('unavailable')
    async def index(home): raise AssertionError('No stale authorization fallback')
    monkeypatch.setattr(supabase_store, 'registered_scan_owner', unavailable)
    monkeypatch.setattr(home_registry, 'load_index_async', index)
    with pytest.raises(HTTPException) as error:
        await flow_api._scan_upload_owner(HOME, 'valid')
    assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize('unchanged', [True, False])
async def test_review_shortcut_preserves_candidate_or_uses_complete_correction(monkeypatch, unchanged):
    monkeypatch.setattr(settings, 'anthropic_api_key', 'fixture-key')
    questions = [{'workItemId': 'work', 'field': 'target_scope'}]
    candidate = result(nextQuestion=questions)
    corrected = result(displayText='Corrected scope')
    captured = {}
    class Client:
        def __init__(self, **kwargs): self.messages = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(stop_reason='end_turn', parsed_output=QuestionReview(
                unchanged=unchanged, revisedResponse=None if unchanged else corrected),
                model='fixture', _request_id='fixture', usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(provider.anthropic, 'AsyncAnthropic', Client)
    payload = json.loads(engine.model_input(state_for(), 'America/New_York'))
    payload['questionReview'] = {'candidateResponse': candidate.model_dump(), 'proposedQuestions': questions}
    output = await provider.generate('system', json.dumps(payload))
    assert output == (candidate if unchanged else corrected)
    assert captured['output_format'] is QuestionReview
    blocks = captured['messages'][0]['content']
    assert len(blocks) == 2 and blocks[0]['cache_control'] == {'type': 'ephemeral'}
    reference, dynamic = (json.loads(block['text']) for block in blocks)
    assert 'transcript' not in reference and 'homeownerAnswers' not in reference
    assert 'cache_control' not in blocks[1]
    assert {**reference, **dynamic} == payload  # No history, answers or rules dropped.


@pytest.mark.asyncio
@pytest.mark.parametrize('repair', [False, True])
async def test_application_fallback_and_validation_repair_require_full_review(monkeypatch, repair):
    monkeypatch.setattr(settings, 'anthropic_api_key', 'fixture-key')
    candidate = result()
    questions = [{'workItemId': 'work', 'field': 'desired_help'}]
    if repair:
        candidate = result(nextQuestion=questions)
    class Client:
        def __init__(self, **kwargs): self.messages = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def parse(self, **kwargs):
            assert kwargs['output_format'] is Result
            return SimpleNamespace(stop_reason='end_turn', parsed_output=candidate,
                model='fixture', _request_id='fixture', usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(provider.anthropic, 'AsyncAnthropic', Client)
    payload = json.loads(engine.model_input(state_for(), 'America/New_York'))
    payload['questionReview'] = {'candidateResponse': candidate.model_dump(), 'proposedQuestions': questions}
    if repair:
        payload['validationFeedback'] = {'error': 'Invalid citation'}
    assert await provider.generate('system', json.dumps(payload)) == candidate


@pytest.mark.parametrize('unchanged,revised', [(True, result()), (False, None)])
def test_review_envelope_requires_exactly_one_outcome(unchanged, revised):
    with pytest.raises(ValueError):
        QuestionReview(unchanged=unchanged, revisedResponse=revised)


@pytest.mark.asyncio
async def test_timing_headers_and_logs_do_not_expose_ids_or_content(caplog):
    import asyncio
    import httpx
    import logging
    from fastapi import FastAPI
    from app.quote_intake.timing import QuoteIntakeTimingMiddleware, timed
    app = FastAPI()
    app.add_middleware(QuoteIntakeTimingMiddleware)
    @app.get('/api/v1/ai/quote-intake/{identifier}')
    @timed('store_read')
    async def read(identifier: str):
        await asyncio.sleep(.001)
        return {'text': 'private homeowner text'}
    with caplog.at_level(logging.INFO, logger='app.quote_intake.timing'):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/v1/ai/quote-intake/private-id')
    assert 'store_read;dur=' in response.headers['server-timing']
    assert 'total;dur=' in response.headers['server-timing']
    records = [r.message for r in caplog.records if r.name == 'app.quote_intake.timing']
    assert len(records) == 1 and 'operation=/restore' in records[0]
    assert 'private-id' not in records[0] and 'private homeowner text' not in records[0]
