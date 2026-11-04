"""Request cycles exercise the HTTP contract with isolated data and captured mail."""
import asyncio
import base64
from uuid import uuid4
from urllib.parse import urlsplit

import pytest
from app.flow import acceptances, ops_email, quote_approvals, provider_responses
from app.flow_quotes import ReturnedQuote, QuoteDocument, quote_store, ApprovalContact
from app.quote_intake import provider, store
from test_quote_intake import direct, wire, mapped_reply
from test_quote_approvals import CONTACT
from test_provider_responses import pdf

BASE = '/api/v1/ai/quote-intake'


async def submit(client, original, conversation=None):
    turn = wire(original['homeId'], conversation=conversation)
    reply = await client.post(BASE + '/turn', json=turn)
    assert reply.status_code == 200, reply.text
    body = dict(conversationId=turn['conversationId'],homeId=original['homeId'],
                expectedRevision=reply.json()['revision'],uploadRevision=original['revision'])
    response = await client.post(BASE + '/submit',json=body)
    assert response.status_code == 200, response.text
    return turn['conversationId'],response.json()['receipt']['quoteRequestId']


@pytest.mark.asyncio
@pytest.mark.parametrize('signed',[False,True])
async def test_paint_accept_next_flooring_history_late_bids_and_two_devices(direct,monkeypatch,pdf,signed):
    client, original, _, _ = direct
    async def generate(system, payload): return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    messages=[]
    async def mail(subject,body,**kwargs): messages.append((subject,body,kwargs));return 'sent'
    monkeypatch.setattr(ops_email,'send_ops_message',mail)
    async def put(*args,**kwargs): pass
    async def storage(*args,**kwargs): return 'https://storage.example.test/private-signed'
    from app.flow import quote_media, supabase_store
    monkeypatch.setattr(quote_media,'put_bytes',put)
    monkeypatch.setattr(supabase_store,'_signed_storage_url',storage)
    conversation,request=await submit(client,original)
    next_body=dict(homeId=original['homeId'],conversationId=str(uuid4()),previousConversationId=conversation,quoteRequestId=request)
    # No bids, a mere opened sheet, and invalid contact cannot unlock the cycle.
    assert (await client.post(BASE+'/start-next',json=next_body)).status_code==409
    saved=await quote_store.get(request)
    saved.quotes=[ReturnedQuote(id='paint',providerName='Painter',providerEmail='paint@example.test',priceUsd=50,
        document=QuoteDocument(fileName='paint.pdf',byteCount=len(pdf),storageBucket='private',storagePath='paint.pdf')),
        ReturnedQuote(id='other',providerName='Competitor',priceUsd=60)]
    await quote_store.save(saved)
    path=f'/api/v1/ai/quote-requests/{request}/quotes/paint/approve'
    assert (await client.post(BASE+'/start-next',json=next_body)).status_code==409
    assert (await client.post(path,json={'contact':dict(CONTACT,email='invalid')})).status_code==422
    assert (await client.post(BASE+'/start-next',json=next_body)).status_code==409
    payload={'contact':CONTACT}
    if signed: payload['signedPDF']=base64.b64encode(pdf).decode()
    approved=await client.post(path,json=payload)
    assert approved.status_code==200,approved.text
    assert [q['id'] for q in approved.json()['quotes']]==['paint']
    assert approved.json()['accepted'] is True
    assert len(messages)==2
    homeowner=next(m for m in messages if m[2].get('to')==CONTACT['email'])
    assert homeowner[2]['cc']==[] and 'They’ll get in touch with you shortly.' in homeowner[1]
    assert homeowner[2]['idempotency_key'] != messages[0][2]['idempotency_key']
    accepted=await quote_store.get(request)
    link=quote_approvals.document_link(accepted,accepted.quote_by_id('paint')) if signed else None
    if signed: assert link in homeowner[1]
    else: assert all(v in homeowner[1] for v in CONTACT.values())
    assert (await client.post(path,json=payload)).status_code==200
    assert len(messages)==2
    # Two independent devices propose different IDs; exactly one saved successor wins.
    starts=await asyncio.gather(client.post(BASE+'/start-next',json=next_body),
        client.post(BASE+'/start-next',json=dict(next_body,conversationId=str(uuid4()))))
    assert all(r.status_code==200 for r in starts)
    successor=starts[0].json()['conversationId']
    assert starts[1].json()['conversationId']==successor
    assert starts[0].json()['messages']==[] and starts[0].json()['receipt'] is None
    assert (await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=str(uuid4())))).json()['conversationId']==successor
    # Old conversation cannot add turns or open a second new cycle.
    assert (await client.post(BASE+'/turn',json=wire(original['homeId'],revision=1,conversation=conversation))).status_code==409
    _,floor_request=await submit(client,original,successor)
    assert floor_request!=request
    flooring=await quote_store.get(floor_request)
    flooring.quotes=[ReturnedQuote(id='floor',providerName='Flooring Co',priceUsd=70)]
    await quote_store.save(flooring)
    stale=await quote_store.get(request)
    stale.selectedQuoteId=None
    stale.quotes.append(ReturnedQuote(id='late-paint',providerName='Late Painter',priceUsd=40))
    await quote_store.save(stale)
    old=await client.get(f'/api/v1/ai/quote-requests/{request}')
    current=await client.get(f'/api/v1/ai/quote-requests/{floor_request}')
    assert [q['id'] for q in old.json()['quotes']]==['paint']
    assert [q['id'] for q in current.json()['quotes']]==['floor']
    assert len((await quote_store.get(request)).quotes)==3,'competing bids remain durable'
    assert (await store.read(conversation,saved.homeownerId,original['homeId']))['state']['receipt']['quoteRequestId']==request
    if signed: assert (await client.get(urlsplit(link).path)).status_code==307
    assert await ops_email.retry_pending_acceptances()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_channel',['provider','homeowner'])
async def test_independent_delivery_retry_and_concurrent_workers(direct,monkeypatch,failed_channel):
    client,original,_,_=direct
    response=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    record=await quote_store.get(response.json()['quoteRequestId'])
    record.quotes=[ReturnedQuote(id='q',providerName='Painter',priceUsd=10)]
    await quote_store.save(record)
    attempts=[]
    async def send(subject,body,**kwargs):
        channel='homeowner' if kwargs.get('to')==CONTACT['email'] else 'provider'
        attempts.append(channel)
        return 'failed' if channel==failed_channel and attempts.count(channel)==1 else 'sent'
    monkeypatch.setattr(ops_email,'send_ops_message',send)
    response=await client.post(f'/api/v1/ai/quote-requests/{record.id}/quotes/q/approve',json={'contact':CONTACT})
    assert response.status_code==200
    await asyncio.gather(*(ops_email.deliver_acceptance(record.id,'q') for _ in range(5)))
    assert attempts.count(failed_channel)==2
    assert attempts.count('homeowner' if failed_channel=='provider' else 'provider')==1
    # Simulate another process that does not share the Python delivery lock.
    assert not await acceptances.delivery(record.id,failed_channel,str(uuid4()))
    assert (await quote_store.get(record.id)).accepted


@pytest.mark.asyncio
async def test_direct_cannot_bypass_active_cycle_and_legacy_pending_recovers(direct,monkeypatch):
    client,original,_,_=direct
    conversation=str(uuid4())
    active=await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=conversation,legacySubmissionId=original['submissionId']))
    assert active.status_code==200 and active.json()['submissionPending']
    assert (await client.post(BASE+'/turn',json=wire(original['homeId'],conversation=conversation))).status_code==409
    assert (await client.post('/api/v1/ai/quote-requests/direct',json=dict(original,submissionId=str(uuid4())))).status_code==409
    result=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    assert result.status_code==201
    restored=await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=str(uuid4())))
    assert restored.json()['receipt']['quoteRequestId']==result.json()['quoteRequestId']
    assert (await client.post('/api/v1/ai/quote-requests/direct',json=original)).status_code==200


@pytest.mark.asyncio
async def test_competing_acceptances_without_process_lock_select_once(direct,monkeypatch):
    client,original,_,_=direct
    response=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    record=await quote_store.get(response.json()['quoteRequestId'])
    record.quotes=[ReturnedQuote(id=id,providerName=id,priceUsd=1,decision='approved',approvalMethod='callback',approvalContact=ApprovalContact(**CONTACT)) for id in ['one','two']]
    await quote_store.save(record)
    outcomes=await asyncio.gather(*(acceptances.commit(record.model_copy(deep=True),q) for q in record.quotes),return_exceptions=True)
    assert sum(not isinstance(r,Exception) for r in outcomes)==1
    assert next(r for r in outcomes if isinstance(r,Exception)).status_code==409
    assert (await quote_store.get(record.id)).accepted


@pytest.mark.asyncio
async def test_legacy_uncertain_submission_can_finish_without_replacing_active_chat(direct,monkeypatch):
    from app.quote_intake import engine,lifecycle
    from test_quote_media import OWNER
    client,original,_,_=direct
    old,current=str(uuid4()),str(uuid4())
    # Simulate two already-persisted conversations from the previous release.
    pending=engine.initial_state()
    pending['submissionId']=original['submissionId']
    pending['pendingSubmission']=original
    pending['transcript']=[dict(id='old',role='homeowner',text='Paint',receivedAt='2026-01-01T00:00:00Z')]
    await store.save(old,OWNER,original['homeId'],pending,None)
    latest=engine.initial_state()
    latest['transcript']=[dict(id='latest',role='homeowner',text='Flooring',receivedAt='2026-02-01T00:00:00Z')]
    await store.save(current,OWNER,original['homeId'],latest,None)
    assert (await lifecycle.cycle(OWNER,original['homeId'],str(uuid4())))['id']==current
    result=await client.post(BASE+'/submit',json=dict(homeId=original['homeId'],conversationId=old,
        expectedRevision=0,uploadRevision=original['revision']))
    assert result.status_code==200,result.text
    assert result.json()['receipt']
    assert (await lifecycle.cycle(OWNER,original['homeId'],str(uuid4())))['id']==current
    assert (await store.read(current,OWNER,original['homeId']))['state']['receipt'] is None


@pytest.mark.asyncio
async def test_receipt_and_next_transition_reject_foreign_associations(direct):
    from app.quote_intake import engine
    from test_quote_media import OWNER
    from fastapi import HTTPException
    client,original,_,_=direct
    result=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    state=engine.initial_state()
    state['receipt']={'quoteRequestId':result.json()['quoteRequestId']}
    with pytest.raises(HTTPException) as error:
        await store.save(str(uuid4()),OWNER,str(uuid4()),state,None)
    assert error.value.status_code==409
    with pytest.raises(HTTPException) as error:
        await store.save(str(uuid4()),str(uuid4()),original['homeId'],state,None)
    assert error.value.status_code==409
    with pytest.raises(HTTPException) as error:
        await store.save(str(uuid4()),OWNER,original['homeId'],state,None)
    assert error.value.status_code==409, 'one request cannot be rebound to another conversation'


@pytest.mark.asyncio
async def test_upgrading_legacy_acceptance_preserves_delivered_email_receipt(direct,monkeypatch):
    client,original,_,_=direct
    response=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    record=await quote_store.get(response.json()['quoteRequestId'])
    record.selectedQuoteId='legacy'
    record.quotes=[ReturnedQuote(id='legacy',providerName='Old Provider',priceUsd=5,decision='approved',approvalMethod='callback',
        approvalContact=ApprovalContact(**CONTACT),acceptanceNotificationQueuedAt='2026-01-01T00:00:00Z',
        acceptanceNotificationDeliveredAt='2026-01-01T00:01:00Z')]
    await quote_store.save(record)
    async def no_mail(*args,**kwargs): raise AssertionError('Existing delivered acceptance must not resend')
    monkeypatch.setattr(ops_email,'send_ops_message',no_mail)
    assert await ops_email.deliver_acceptance(record.id,'legacy')=='already_sent'
    upgraded=(await quote_store.get(record.id)).quote_by_id('legacy')
    assert upgraded.acceptanceNotificationDeliveredAt=='2026-01-01T00:01:00Z'
    assert upgraded.homeownerConfirmationQueuedAt is None
