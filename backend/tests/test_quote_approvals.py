"""Both approval paths persist, enforce ownership, and keep signed copies private."""
import time
from urllib.parse import urlsplit

import httpx
import pytest

from app.config import settings
from app.flow import ops_email, quote_approvals, quote_media as media, supabase_store
from app.flow_quotes import QuoteRequestStore, quote_store
from test_provider_responses import setup, pdf, isolated


@pytest.fixture(autouse=True)
def capture_decision_mail(monkeypatch):
    sent = []
    async def capture(subject, body, **kwargs):
        sent.append((subject, body))
        return 'sent'
    monkeypatch.setattr(ops_email, 'send_ops_message', capture)
    return sent


async def quote(monkeypatch, pdf):
    rec, client, path, params, headers, uploaded = await setup(monkeypatch)
    response = await client.post(path + '/responses', params=params, headers=headers, content=pdf)
    assert response.status_code == 201
    quote_id = response.json()['quoteId']
    await client.aclose()
    from app.main import app
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
    return rec, client, quote_id, f'/api/v1/ai/quote-requests/{rec.id}/quotes/{quote_id}/approve', uploaded


@pytest.mark.asyncio
async def test_callback_releases_only_chosen_number_and_survives_restart(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, uploaded = await quote(monkeypatch, pdf)
    async with client:
        response = await client.post(path, json={'phone': '(615) 555-0199'})
        assert response.status_code == 200, response.text
        view = response.json()
        assert view['selectedQuoteId'] == qid
        assert view['quotes'][0]['approvalMethod'] == 'callback'
        assert view['quotes'][0]['callbackPhone'] == '(615) 555-0199'
        assert response.headers['cache-control'] == 'private, no-store'
        assert '(615) 555-0199' in capture_decision_mail[0][1]
        assert 'ask them to call' in capture_decision_mail[0][1]
        # Network retries do not change consent or notify twice.
        assert (await client.post(path, json={'phone': '(615) 555-0199'})).status_code == 200
        assert len(capture_decision_mail) == 1
    cold = await QuoteRequestStore().get(rec.id)
    assert cold.quote_by_id(qid).callbackPhone == '(615) 555-0199'
    assert len(uploaded) == 1, 'a callback does not upload a signed PDF'


@pytest.mark.asyncio
async def test_signed_pdf_persists_without_replacing_original_and_private_link_revokes(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, uploaded = await quote(monkeypatch, pdf)
    signed_pdf = pdf[:-6] + b'\n% signature fixture\n%%EOF'
    async with client:
        response = await client.post(path, content=signed_pdf, headers={'Content-Type': 'application/pdf'})
        assert response.status_code == 200, response.text
        view = response.json()['quotes'][0]
        assert view['approvalMethod'] == 'signature'
        assert view['signedDocument']['url'] != view['document']['url']
        assert 'storagePath' not in response.text and 'storageBucket' not in response.text
        assert uploaded[-1][1] == signed_pdf
        assert (await client.post(path, content=signed_pdf, headers={'Content-Type': 'application/pdf'})).status_code == 200
        assert len(uploaded) == 2
        saved = await QuoteRequestStore().get(rec.id)
        signed = saved.quote_by_id(qid)
        assert signed.document.storagePath != signed.signedDocument.storagePath
        link = quote_approvals.document_link(saved, signed)
        assert link in capture_decision_mail[0][1]
        result = await client.get(urlsplit(link).path)
        assert result.status_code == 307
        assert result.headers['cache-control'] == 'private, no-store'
        assert (await client.get(urlsplit(link).path[:-1] + ('a' if link[-1] != 'a' else 'b'))).status_code == 403
        expired = int(time.time()) - 1
        assert (await client.get(f'/api/v1/quote-approvals/{rec.id}/{qid}/{expired}/{quote_approvals.signature(saved, signed, expired)}')).status_code == 410
        saved.selectedQuoteId = None
        await quote_store.save(saved)
        assert (await client.get(urlsplit(link).path)).status_code == 404


@pytest.mark.asyncio
async def test_approval_rejects_unauthorized_invalid_and_failed_uploads(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, uploaded = await quote(monkeypatch, pdf)
    async with client:
        for phone in ['', 'not a phone', '123', '1234567<script>', '+1' * 20]:
            assert (await client.post(path, json={'phone': phone})).status_code == 422
        assert (await client.post(path, content=b'not PDF', headers={'Content-Type': 'application/pdf'})).status_code == 422
        current = await quote_store.get(rec.id)
        assert current.selectedQuoteId is None
        current.homeownerId = 'private-owner'
        await quote_store.save(current)
        assert (await client.post(path, json={'phone': '6155550199'})).status_code == 403
        assert len(uploaded) == 1 and not capture_decision_mail
        current.homeownerId = None
        await quote_store.save(current)
        async def offline(*args): raise RuntimeError('offline')
        monkeypatch.setattr(media, 'put_bytes', offline)
        assert (await client.post(path, content=pdf, headers={'Content-Type': 'application/pdf'})).status_code == 503
        assert (await quote_store.get(rec.id)).selectedQuoteId is None


@pytest.mark.asyncio
async def test_approval_cannot_select_another_quote_or_closed_request(monkeypatch, pdf):
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    current = await quote_store.get(rec.id)
    current.selectedQuoteId = 'another-provider'
    await quote_store.save(current)
    async with client:
        assert (await client.post(path, json={'phone': '6155550199'})).status_code == 409
        current.selectedQuoteId = None
        current.status = 'closed'
        await quote_store.save(current)
        assert (await client.post(path, json={'phone': '6155550199'})).status_code == 409


@pytest.mark.asyncio
async def test_durable_save_failure_never_reports_approval_success(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    async def unavailable(*args, **kwargs): return False
    # Keep retrieval local while exercising the configured durable-write requirement.
    monkeypatch.setattr(supabase_store, 'upsert_quote_request', unavailable)
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    async def no_record(*args): return None
    monkeypatch.setattr(supabase_store, 'get_quote_request', no_record)
    from app.flow import provider_responses
    original = await QuoteRequestStore().get(rec.id, include_provider_responses=False)
    # Use an already merged parent to avoid a separate mocked REST receipt query.
    from app.flow_quotes import ReturnedQuote, QuoteDocument
    original.quotes = [ReturnedQuote(id=qid, providerName='Brightline', priceUsd=2450,
                                    document=QuoteDocument(fileName='quote.pdf', byteCount=len(pdf)))]
    quote_store._path(rec.id).write_text(original.model_dump_json())
    async def no_responses(*args): return []
    monkeypatch.setattr(provider_responses, 'responses', no_responses)
    async with client:
        response = await client.post(path, json={'phone': '6155550199'})
        assert response.status_code == 503
    assert not capture_decision_mail
    assert quote_store._get_local(rec.id).selectedQuoteId is None
