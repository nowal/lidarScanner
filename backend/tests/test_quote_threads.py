"""Saved-thread isolation, recovery, authorization, and the browsing regression."""
import asyncio
from uuid import uuid4

import pytest
from app.quote_intake import engine, provider, store, threads
from app.flow_quotes import quote_store
from test_quote_intake import direct, wire, mapped_reply, result, state_for
from test_quote_lifecycle import submit, BASE
from test_quote_media import OWNER


def test_browsing_then_painting_does_not_latch_pause():
    state = state_for('What can you do?')
    state = engine.apply_result(state, result([], intent='browse', intentEvidence='What can you do?',
        zipUpdates=[], serviceCandidates=[], action='ask', displayText='What project are you working on?'))
    assert not state['paused'] and not state['ready']
    state['transcript'].append(dict(id='turn-2',role='homeowner',text='Cool let’s do painting. Paint the living room walls only, full repaint, you choose the finish, ZIP 01234.'))
    state['pendingTurn']={'clientTurnId':'turn-2'}
    data = result().model_dump_json().replace('turn-1','turn-2')
    from app.quote_intake.schema import Result
    state = engine.apply_result(state,Result.model_validate_json(data))
    assert state['ready'] and not state['paused']


def test_paused_legacy_thread_cannot_claim_ready_and_explicit_resume_recovers():
    state = state_for()
    state['paused'] = True  # Already saved by the old browse-as-pause release.
    paused = engine.apply_result(state,result(displayText='Your painting request is ready.'))
    assert paused['paused'] and not paused['ready']
    assert 'paused' in paused['result']['displayText']
    paused['transcript'].append(dict(id='resume',role='homeowner',text='Resume this request.'))
    paused['pendingTurn']={'clientTurnId':'resume'}
    recovered = engine.apply_result(paused,result(intent='browse',intentSourceTurnId='resume',intentEvidence='Resume',zipUpdates=[]))
    assert recovered['ready'] and not recovered['paused']


def test_browsing_does_not_resume_explicit_stop():
    state = state_for()
    state['paused'] = True
    browsing = engine.apply_result(state,result(intent='browse'))
    assert browsing['paused'] and not browsing['ready']


@pytest.mark.asyncio
async def test_threads_before_acceptance_keep_receipts_and_can_reopen(direct,monkeypatch):
    client,original,_,scheduled=direct
    async def generate(system,payload): return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    old,request=await submit(client,original)
    body=dict(homeId=original['homeId'],conversationId=str(uuid4()))
    starts=await asyncio.gather(*(client.post(BASE+'/threads/new',json=body) for _ in range(3)))
    assert all(r.status_code==200 for r in starts)
    assert {r.json()['conversationId'] for r in starts}=={body['conversationId']}
    assert all(not r.json()['messages'] and r.json()['receipt'] is None for r in starts)
    assert not (await quote_store.get(request)).accepted
    assert starts[0].json()['threadVersion']==1
    _,other_request=await submit(client,original,body['conversationId'])
    assert other_request!=request
    # Reopening history neither dispatches again nor crosses quote receipts.
    before=len(scheduled)
    selected=await client.post(BASE+'/threads/select',json=dict(body,conversationId=old))
    assert selected.json()['receipt']['quoteRequestId']==request
    assert len(scheduled)==before
    index=(await client.get(BASE+'/threads')).json()
    assert len(index['threads'])==2
    assert all(t['status']=='submitted' for t in index['threads'])
    assert all('Painting' in t['title'] for t in index['threads'])
    assert {t['quoteRequestId'] for t in index['threads']}=={request,other_request}


@pytest.mark.asyncio
async def test_new_thread_from_draft_and_old_thread_retry_are_independent(direct,monkeypatch):
    client,original,_,_=direct
    async def generate(system,payload): return mapped_reply(payload)
    monkeypatch.setattr(provider,'generate',generate)
    first,second=str(uuid4()),str(uuid4())
    for identifier in [first,second]:
        assert (await client.post(BASE+'/threads/new',json=dict(homeId=original['homeId'],conversationId=identifier))).status_code==200
    # The first device can finish its owned thread while another thread is selected.
    _,request=await submit(client,original,first)
    active=await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=str(uuid4())))
    assert active.json()['conversationId']==second and active.json()['receipt'] is None
    assert (await store.read(first,OWNER,original['homeId']))['state']['receipt']['quoteRequestId']==request


@pytest.mark.asyncio
async def test_thread_listing_and_selection_are_owner_scoped_and_paginated(direct):
    client,original,_,_=direct
    home=original['homeId']
    ours=await threads.select(OWNER,home,str(uuid4()),create=True)
    foreign=await threads.select(str(uuid4()),home,str(uuid4()),create=True)
    assert (await client.get(BASE+'/threads',headers={'X-Homeowner-Token':'wrong'})).status_code==401
    index=(await client.get(BASE+'/threads')).json()
    assert [t['conversationId'] for t in index['threads']]==[ours['id']]
    for path in ['/threads/select','/threads/new']:
        assert (await client.post(BASE+path,json=dict(homeId=home,conversationId=foreign['id']))).status_code==404
    assert (await client.post(BASE+'/threads/select',json=dict(homeId=home,conversationId=str(uuid4())))).status_code==404
    for _ in range(3): await threads.select(OWNER,home,str(uuid4()),create=True)
    first=await threads.list_threads(OWNER,limit=2)
    second=await threads.list_threads(OWNER,offset=first['nextOffset'],limit=2)
    assert len(first['threads'])==len(second['threads'])==2 and second['nextOffset'] is None
    assert not {r['conversationId'] for r in first['threads']} & {r['conversationId'] for r in second['threads']}


@pytest.mark.asyncio
async def test_legacy_cache_cannot_attach_previous_request_to_empty_new_thread(direct):
    client,original,_,_=direct
    fresh=str(uuid4())
    await client.post(BASE+'/threads/new',json=dict(homeId=original['homeId'],conversationId=fresh))
    response=await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=str(uuid4()),legacySubmissionId=original['submissionId']))
    assert response.json()['conversationId']==fresh
    assert not response.json()['submissionPending'] and response.json()['receipt'] is None


@pytest.mark.asyncio
async def test_pending_legacy_dispatch_finishes_in_original_thread_after_switch(direct):
    client,original,_,_=direct
    old,fresh=str(uuid4()),str(uuid4())
    active=await client.post(BASE+'/active',json=dict(homeId=original['homeId'],conversationId=old,legacySubmissionId=original['submissionId']))
    assert active.json()['submissionPending']
    await client.post(BASE+'/threads/new',json=dict(homeId=original['homeId'],conversationId=fresh))
    sent=await client.post('/api/v1/ai/quote-requests/direct',json=original)
    assert sent.status_code==201,sent.text
    old_state=(await store.read(old,OWNER,original['homeId']))['state']
    fresh_state=(await store.read(fresh,OWNER,original['homeId']))['state']
    assert old_state['receipt']['quoteRequestId']==sent.json()['quoteRequestId']
    assert fresh_state['receipt'] is None and fresh_state['pendingSubmission'] is None
