"""Both approval paths persist, enforce ownership, and keep signed copies private."""
import time
from urllib.parse import urlsplit

import httpx
import pytest

from app.config import settings
from app.flow import ops_email, quote_approvals, quote_media as media, supabase_store
from app.flow_quotes import QuoteRequestStore, quote_store
from test_provider_responses import setup, pdf, isolated

REAL_SEND_OPS_MESSAGE = ops_email.send_ops_message


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
    current = await quote_store.get(rec.id)
    current.firstName = 'Test Homeowner'
    current.contactPhone = '6155550199'
    current.contactEmail = 'homeowner@example.test'
    current.address = '123 Hidden Street, Nashville, TN 37203'
    await quote_store.save(current)
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


CONTACT = dict(fullName='New Homeowner', phone='(615) 555-0123', email='new@example.test',
               address='987 New Street, Nashville, TN 37203, Unit 4B')


@pytest.mark.asyncio
async def test_contact_prefill_is_private_and_not_approval(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    async with client:
        prefill = await client.get(f'/api/v1/ai/quote-requests/{rec.id}/contact')
        assert prefill.status_code == 200
        assert prefill.headers['cache-control'] == 'private, no-store'
        assert prefill.json()['email'] == 'homeowner@example.test'
        saved = await quote_store.get(rec.id)
        assert saved.selectedQuoteId is None and saved.quote_by_id(qid).approvalContact is None
        saved.homeownerId = 'private-owner'
        await quote_store.save(saved)
        assert (await client.get(f'/api/v1/ai/quote-requests/{rec.id}/contact')).status_code == 403
    assert not capture_decision_mail


@pytest.mark.asyncio
@pytest.mark.parametrize('signed', [False, True])
async def test_complete_contact_acceptance_and_corrected_address(monkeypatch, pdf, capture_decision_mail, signed):
    import base64
    rec, client, qid, path, uploads = await quote(monkeypatch, pdf)
    payload = {'contact': CONTACT}
    if signed: payload['signedPDF'] = base64.b64encode(pdf).decode()
    async with client:
        result = await client.post(path, json=payload)
        assert result.status_code == 200, result.text
        saved = await QuoteRequestStore().get(rec.id)
        chosen = saved.quote_by_id(qid)
        assert chosen.approvalContact.model_dump() == CONTACT
        assert saved.ops_view()['address'] == CONTACT['address']
        assert chosen.approvalMethod == ('signature' if signed else 'callback')
        body = capture_decision_mail[0][1]
        assert all(value in body for value in CONTACT.values())
        assert ('Signed quote PDF:' in body) == signed
        assert chosen.acceptanceNotificationDeliveredAt
        assert (await client.post(path, json=payload)).status_code == 200
        assert len(capture_decision_mail) == 1
        assert len(uploads) == (2 if signed else 1)
        # Neither initial provider email nor general viewer exposes accepted contact.
        from app.flow.ops_email import build_ops_email
        _, initial = build_ops_email(saved, [], [])
        assert all(value not in initial for value in CONTACT.values())
        assert 'approvalContact' not in result.text


@pytest.mark.asyncio
async def test_incomplete_legacy_clients_and_legacy_selection_cannot_bypass_contact(monkeypatch, pdf, capture_decision_mail):
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    saved = await quote_store.get(rec.id)
    saved.contactEmail = None
    await quote_store.save(saved)
    async with client:
        assert (await client.post(path, json={'phone': '6155550123'})).status_code == 409
        assert (await client.post(path, content=pdf, headers={'Content-Type': 'application/pdf'})).status_code == 409
        for route, body in [('select', {'quoteId': qid}), ('decide', {'quoteId': qid, 'decision': 'approved'})]:
            assert (await client.post(f'/api/v1/ai/quote-requests/{rec.id}/{route}', json=body)).status_code == 409
    assert (await quote_store.get(rec.id)).selectedQuoteId is None
    assert not capture_decision_mail


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('fullName', '  '), ('phone', 'bad'), ('email', 'no-at'), ('address', '123 Main')])
async def test_invalid_contact_never_saves_or_uploads(monkeypatch, pdf, capture_decision_mail, field, value):
    import base64
    rec, client, qid, path, uploads = await quote(monkeypatch, pdf)
    async with client:
        result = await client.post(path, json={'contact': dict(CONTACT, **{field: value}), 'signedPDF': base64.b64encode(pdf).decode()})
        assert result.status_code == 422
    assert (await quote_store.get(rec.id)).selectedQuoteId is None
    assert len(uploads) == 1 and not capture_decision_mail


@pytest.mark.asyncio
async def test_notification_failure_is_recovered_without_duplicate_approval(monkeypatch, pdf):
    rec, client, qid, path, uploads = await quote(monkeypatch, pdf)
    deliveries = []
    async def fail_then_succeed(subject, body, **kwargs):
        deliveries.append((subject, body, kwargs))
        return 'failed' if len(deliveries) == 1 else 'sent'
    monkeypatch.setattr(ops_email, 'send_ops_message', fail_then_succeed)
    async with client:
        assert (await client.post(path, json={'contact': CONTACT})).status_code == 200
        saved = await QuoteRequestStore().get(rec.id)
        decided = saved.quote_by_id(qid).decidedAt
        assert saved.quote_by_id(qid).acceptanceNotificationQueuedAt
        assert not saved.quote_by_id(qid).acceptanceNotificationDeliveredAt
        assert await ops_email.retry_pending_acceptances() == 1
        assert await ops_email.retry_pending_acceptances() == 0
        assert (await client.post(path, json={'contact': CONTACT})).status_code == 200
        assert len(deliveries) == 2
        assert deliveries[0] == deliveries[1], 'Identical transport idempotency key and payload on retry'
        assert deliveries[0][2]['cc'] == ['noah@takeshapehome.com']
        assert (await quote_store.get(rec.id)).quote_by_id(qid).decidedAt == decided
        assert len(uploads) == 1


@pytest.mark.asyncio
async def test_acceptance_recipients_do_not_expand_inbound_command_trust(monkeypatch, pdf):
    from app.flow.ops_reply import allowed_reply_senders
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    monkeypatch.setattr(settings, 'ops_email', 'Quintin <quintin@takeshapehome.com>')
    monkeypatch.setattr(settings, 'resend_api_key', 'fake-test-key')
    requests = []
    async def capture(to, subject, body, html=None, **kwargs): requests.append((to, kwargs))
    monkeypatch.setattr(ops_email, '_send_via_resend', capture)
    monkeypatch.setattr(ops_email, 'send_ops_message', REAL_SEND_OPS_MESSAGE)
    async with client:
        assert (await client.post(path, json={'contact': CONTACT})).status_code == 200
    assert ops_email.recipient_list(requests[0][0]) == ['quintin@takeshapehome.com']
    assert requests[0][1]['cc'] == ['noah@takeshapehome.com']
    assert 'noah@takeshapehome.com' not in allowed_reply_senders()


@pytest.mark.asyncio
async def test_cancelled_approval_is_not_released_by_notification_recovery(monkeypatch, pdf):
    rec, client, qid, path, _ = await quote(monkeypatch, pdf)
    async def failed(*args, **kwargs): return 'failed'
    monkeypatch.setattr(ops_email, 'send_ops_message', failed)
    async with client:
        assert (await client.post(path, json={'contact': CONTACT})).status_code == 200
        assert (await client.post(f'/api/v1/ai/quote-requests/{rec.id}/decide',
                                  json={'quoteId': qid, 'decision': 'declined', 'note': 'Changed my mind'})).status_code == 200
    assert await ops_email.retry_pending_acceptances() == 0
    saved = await quote_store.get(rec.id)
    assert saved.ops_view()['approvalContact'] is None
    assert saved.ops_view()['address'] is None
@pytest.mark.asyncio
async def test_notification_recovery_can_list_requests_beyond_first_database_page(monkeypatch):
    from app.flow import supabase_store
    offsets = []
    class REST:
        async def get(self, path, *, params):
            offset = int(params['offset'])
            offsets.append(offset)
            rows = [{'record': {'id': f'qr_{i}'}} for i in range(offset, min(offset + 1000, 1001))]
            return httpx.Response(200, json=rows, request=httpx.Request('GET', 'https://example.test'))
    monkeypatch.setattr(supabase_store, 'enabled', lambda: True)
    monkeypatch.setattr(supabase_store, '_rest', lambda: REST())
    records = await supabase_store.list_quote_requests()
    assert len(records) == 1001 and records[-1]['id'] == 'qr_1000'
    assert offsets == [0, 1000]
