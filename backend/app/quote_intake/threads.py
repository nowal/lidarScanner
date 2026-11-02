"""Owner-scoped saved threads; selecting one never changes its request receipt."""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from ..flow import supabase_store, acceptances
from . import engine, store


def _select(owner, home, identifier, state):
    with store._db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS active_conversations (owner TEXT, home TEXT, conversation TEXT NOT NULL, PRIMARY KEY(owner,home))')
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT id,owner,home,version,state FROM intakes WHERE id=?', (identifier,)).fetchone()
        if row and (row[1] != owner or row[2] != home):
            raise HTTPException(404, 'Thread not found')
        if not row:
            if state is None:
                raise HTTPException(404, 'Thread not found')
            conn.execute('INSERT INTO intakes VALUES(?,?,?,?,?)', (identifier,owner,home,0,json.dumps(state)))
            row = (identifier,owner,home,0,json.dumps(state))
        conn.execute('INSERT INTO active_conversations VALUES(?,?,?) ON CONFLICT(owner,home) DO UPDATE SET conversation=excluded.conversation', (owner,home,identifier))
        return dict(id=row[0],owner_id=row[1],home_id=row[2],version=row[3],state=json.loads(row[4]))


async def select(owner, home, identifier, *, create=False):
    state = None
    if create:
        try:
            state = engine.initial_state()
        except (ValueError, OSError) as exc:
            raise HTTPException(503, 'Could not start a thread. Please retry.') from exc
        state['threadVersion'] = 1
        state['createdAt'] = datetime.now(timezone.utc).isoformat()
    if supabase_store.enabled():
        return await acceptances.rpc('select_quote_thread', dict(p_owner=owner,p_home=home,p_conversation=identifier,p_state=state))
    return await asyncio.to_thread(_select,owner,home,identifier,state)


def summary(row):
    items = row.get('work_items') or []
    services = list(dict.fromkeys(i.get('serviceName') or i.get('service') or 'Project' for i in items))
    title = ' & '.join(services) if services else 'New thread'
    if len(items) == 1:
        scope = next((a.get('value') for a in items[0].get('answers', []) if a['field']=='target_scope' and a['status']=='answered'), None)
        if scope:
            title += ' — ' + scope
    if row.get('receipt') and not services:
        title = 'Quote request'
    status = ('submitted' if row.get('receipt') else 'submitting' if row.get('pending') else
              'paused' if row.get('paused') else 'draft')
    return dict(conversationId=row['id'],homeId=row['home_id'],title=' '.join(title.split())[:100],
                createdAt=row.get('created_at'),updatedAt=row.get('updated_at'),status=status,
                quoteRequestId=(row.get('receipt') or {}).get('quoteRequestId'))


async def list_threads(owner, offset=0, limit=40):
    try:
        if supabase_store.enabled():
            response = await supabase_store._rest().get('/quote_intakes', params={
                'owner_id':'eq.'+owner, 'order':'updated_at.desc,id.desc', 'offset':str(offset), 'limit':str(limit+1),
                'select':'id,home_id,created_at,updated_at,work_items:state->workItems,receipt:state->receipt,paused:state->paused,pending:state->pendingSubmission'})
            response.raise_for_status()
            rows = response.json()
        else:
            def read():
                with store._db() as conn:
                    rows = []
                    for identifier,home,raw in conn.execute('SELECT id,home,state FROM intakes WHERE owner=?', (owner,)):
                        state = json.loads(raw)
                        times = [t['receivedAt'] for t in state.get('transcript', []) if t.get('receivedAt')]
                        rows.append(dict(id=identifier,home_id=home,work_items=state.get('workItems'),receipt=state.get('receipt'),
                            paused=state.get('paused'),pending=state.get('pendingSubmission'),
                            created_at=state.get('createdAt') or min(times,default=None),updated_at=max(times,default=state.get('createdAt'))))
                    return sorted(rows,key=lambda r:(r['updated_at'] or '',r['id']),reverse=True)[offset:offset+limit+1]
            rows = await asyncio.to_thread(read)
        return dict(threads=[summary(r) for r in rows[:limit]],nextOffset=offset+limit if len(rows)>limit else None)
    except Exception as exc:
        raise HTTPException(503, 'Could not load your threads. Please retry.') from exc
