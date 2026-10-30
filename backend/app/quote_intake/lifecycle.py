"""One durable active intake per verified owner/home; start is acceptance-gated."""
import asyncio
import copy
import json
from fastapi import HTTPException
from ..flow import supabase_store, acceptances
from ..flow_quotes import quote_store
from . import store, engine


def _local_cycle(owner, home, identifier, state, previous, request, legacy, accepted):
    with store._db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS active_conversations (owner TEXT, home TEXT, conversation TEXT NOT NULL, PRIMARY KEY(owner,home))')
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT conversation FROM active_conversations WHERE owner=? AND home=?', (owner, home)).fetchone()
        active = active[0] if active else None
        if previous:
            prior = conn.execute('SELECT state FROM intakes WHERE id=? AND owner=? AND home=?', (previous, owner, home)).fetchone()
            prior = json.loads(prior[0]) if prior else None
            if not prior or (prior.get('receipt') or {}).get('quoteRequestId') != request:
                raise HTTPException(409, 'Wrong current request')
            if prior.get('nextConversationId'):
                identifier = active or prior['nextConversationId']
            else:
                if active != previous or not accepted:
                    raise HTTPException(409, 'Accept the current quote before starting another chat')
                if conn.execute('SELECT 1 FROM intakes WHERE id=?', (identifier,)).fetchone():
                    raise HTTPException(409, 'Fresh conversation identity required')
                conn.execute('INSERT INTO intakes VALUES(?,?,?,?,?)', (identifier, owner, home, 0, json.dumps(state)))
                prior['nextConversationId'] = identifier
                conn.execute('UPDATE intakes SET state=?,version=version+1 WHERE id=?', (json.dumps(prior), previous))
                conn.execute('UPDATE active_conversations SET conversation=? WHERE owner=? AND home=?', (identifier, owner, home))
            active = identifier
        elif not active:
            # Existing saved intakes win over an unrelated fresh client identity.
            rows = conn.execute('SELECT id,owner,home,state FROM intakes WHERE owner=? AND home=?', (owner,home)).fetchall()
            if rows:
                def timestamp(row):
                    transcript = json.loads(row[3]).get('transcript', [])
                    return max((t.get('receivedAt', '') for t in transcript), default='')
                active = max(rows, key=timestamp)[0]
            else:
                collision = conn.execute('SELECT 1 FROM intakes WHERE id=?', (identifier,)).fetchone()
                if collision:
                    raise HTTPException(404, 'Intake not found for this homeowner and scan')
                if legacy:
                    state['receipt'] = dict(quoteRequestId=legacy)
                conn.execute('INSERT INTO intakes VALUES(?,?,?,?,?)', (identifier, owner, home, 0, json.dumps(state)))
                active = identifier
            conn.execute('INSERT INTO active_conversations VALUES(?,?,?)', (owner, home, active))
        row = conn.execute('SELECT id,owner,home,version,state FROM intakes WHERE id=?', (active,)).fetchone()
        current = json.loads(row[4])
        if state.get('legacySubmissionId') and not current.get('receipt') and not current.get('transcript') and not current.get('pendingTurn'):
            if current.get('legacySubmissionId') not in (None,state['legacySubmissionId']):
                raise HTTPException(409, 'Recover the original pending submission')
            current['legacySubmissionId'] = state['legacySubmissionId']
            conn.execute('UPDATE intakes SET state=?,version=version+1 WHERE id=?',(json.dumps(current),active))
            row = conn.execute('SELECT id,owner,home,version,state FROM intakes WHERE id=?',(active,)).fetchone()
        return dict(id=row[0], owner_id=row[1], home_id=row[2], version=row[3], state=json.loads(row[4]))


async def current(owner, home):
    if supabase_store.enabled():
        try:
            response = await supabase_store._rest().get('/quote_active_conversations', params={
                'owner_id': 'eq.' + owner, 'home_id': 'eq.' + home, 'select': 'conversation_id'})
            response.raise_for_status()
            rows = response.json()
            identifier = rows[0]['conversation_id'] if rows else None
        except Exception as exc:
            raise HTTPException(503, 'Could not restore the active conversation. Please retry.') from exc
    else:
        def read():
            with store._db() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS active_conversations (owner TEXT, home TEXT, conversation TEXT NOT NULL, PRIMARY KEY(owner,home))')
                row = conn.execute('SELECT conversation FROM active_conversations WHERE owner=? AND home=?', (owner,home)).fetchone()
                return row[0] if row else None
        identifier = await asyncio.to_thread(read)
    return await store.read(identifier,owner,home) if identifier else None


async def cycle(owner, home, identifier, *, previous=None, request=None, legacy=None, legacy_submission=None):
    # Existing conversations retain their pinned rubric even if a newly
    # installed rubric cannot initialize a fresh conversation.
    if previous is None and not legacy_submission:
        active = await current(owner,home)
        if active:
            return active
    try:
        state = engine.initial_state()
    except (ValueError, OSError) as exc:
        raise HTTPException(503, 'Quote intake configuration is invalid. Please retry later.') from exc
    if legacy_submission:
        state["legacySubmissionId"] = legacy_submission
    if supabase_store.enabled():
        return await acceptances.rpc('quote_intake_cycle', dict(p_owner=owner,p_home=home,p_conversation=identifier,
            p_state=state,p_previous=previous,p_request=request,p_legacy=legacy))
    accepted = False
    if previous:
        record = await quote_store.get(request)
        accepted = bool(record and record.homeownerId == owner and record.homeId == home and record.accepted)
    else:
        records = [r for r in await quote_store.list() if r.homeownerId == owner and r.homeId == home]
        if legacy and not any(r.id == legacy for r in records):
            raise HTTPException(404, 'Legacy request not found for this homeowner and scan')
        if records and not legacy:
            legacy = max(records, key=lambda r: (r.createdAt,r.id)).id
    return await asyncio.to_thread(_local_cycle, owner, home, identifier, state, previous, request, legacy, accepted)


async def require_active(owner, home, identifier):
    row = await cycle(owner, home, identifier)
    if row['id'] != identifier:
        raise HTTPException(409, 'Another conversation is active for this home. Reload it before continuing.')
    return row


async def validate_direct_submission(owner, home, submission):
    """Old clients can recover their exact payload, but cannot bypass a current intake."""
    active = await current(owner,home)
    identifier = active['id'] if active else None
    if not identifier:
        row = await cycle(owner,home,submission,legacy_submission=submission)
        identifier = row['id']
    if identifier:
        row = await store.read(identifier,owner,home)
        if (row['state'].get('pendingSubmission') or {}).get('submissionId') != submission and row['state'].get('legacySubmissionId') != submission:
            # Pre-upgrade intakes may include an older uncertain dispatch. It
            # may finish its immutable payload, but never become the active chat.
            if supabase_store.enabled():
                response = await supabase_store._rest().get('/quote_intakes', params={
                    'owner_id':'eq.'+owner,'home_id':'eq.'+home,
                    'state->pendingSubmission->>submissionId':'eq.'+submission,'select':'id','limit':'1'})
                response.raise_for_status()
                pending = bool(response.json())
            else:
                def pending_read():
                    with store._db() as conn:
                        return conn.execute("SELECT 1 FROM intakes WHERE owner=? AND home=? AND json_extract(state,'$.pendingSubmission.submissionId')=? LIMIT 1", (owner,home,submission)).fetchone() is not None
                pending = await asyncio.to_thread(pending_read)
            if not pending:
                raise HTTPException(409, 'Continue the current conversation before creating another request.')


async def attach_legacy_submission(owner, home, submission, receipt):
    row = await cycle(owner,home,submission)
    state = copy.deepcopy(row['state'])
    if state.get('legacySubmissionId') != submission or state.get('receipt'):
        return
    state['receipt'] = receipt
    state['pendingSubmission'] = None
    await store.save(row['id'],owner,home,state,row['version'])
