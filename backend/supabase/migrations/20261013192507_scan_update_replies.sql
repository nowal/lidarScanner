-- Assistant-only scan events. No writes to flow_states or quote tables.
-- Backend service role only, including RPC execute rights.
create table public.scan_update_cursors (
    thread_id text not null references public.flow_states(thread_id) on delete cascade,
    home_id uuid not null references public.scan_upload_heads(home_id) on delete cascade,
    baseline_index jsonb not null,
    primary key (thread_id, home_id)
);
create table public.scan_update_replies (
    thread_id text not null,
    home_id uuid not null,
    capture_key text not null,
    event_id uuid not null default gen_random_uuid() unique,
    status text not null default 'pending' check (status in ('pending','ready','skipped')),
    context_index jsonb not null,
    message jsonb,
    lease_token uuid,
    lease_until timestamptz,
    retry_after timestamptz,
    attempts integer not null default 0,
    generated_at timestamptz,
    delivered_at timestamptz,
    primary key (thread_id, home_id, capture_key),
    foreign key (thread_id, home_id) references public.scan_update_cursors(thread_id, home_id) on delete cascade,
    check (status <> 'ready' or message is not null)
);
create index scan_update_cursors_home_idx on public.scan_update_cursors(home_id);
alter table public.scan_update_cursors enable row level security;
alter table public.scan_update_replies enable row level security;
revoke all on public.scan_update_cursors, public.scan_update_replies from public, anon, authenticated;
grant all on public.scan_update_cursors, public.scan_update_replies to service_role;

create function public.scan_update_transition(
    p_home_id uuid, p_thread_id text, p_revision uuid, p_owner_id uuid,
    p_action text, p_payload jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    h public.scan_upload_heads%rowtype;
    j public.scan_upload_jobs%rowtype;
    f public.flow_states%rowtype;
    c public.scan_update_cursors%rowtype;
    e public.scan_update_replies%rowtype;
    baseline jsonb;
    capture text;
    claimed boolean := false;
begin
    -- Same lock as ingestion/publication: a superseding revision cannot slip
    -- between readiness verification and saving or acknowledging this reply.
    select * into h from public.scan_upload_heads where home_id=p_home_id for update;
    if not found then return jsonb_build_object('error','context_not_ready'); end if;
    if p_owner_id is null or h.owner_id <> p_owner_id then return jsonb_build_object('error','owner_mismatch'); end if;
    if h.current_revision is distinct from p_revision then
        return jsonb_build_object('error','revision_mismatch');
    end if;
    -- Read committed, not a client token or a cached snapshot. Ordinary turns
    -- may advance freely: this function never writes conversation state.
    select * into f from public.flow_states where thread_id=p_thread_id;
    if not found then return jsonb_build_object('error','thread_not_ready'); end if;
    if f.homeowner_id is distinct from p_owner_id then
        return jsonb_build_object('error','owner_mismatch');
    end if;
    if lower(f.state->>'home_id') is distinct from p_home_id::text then
        return jsonb_build_object('error','thread_home_mismatch');
    end if;
    select * into j from public.scan_upload_jobs where home_id=p_home_id and revision=p_revision;
    if j.context_index is null or j.progress->>'status' is distinct from 'done'
            or j.context_index->'upload'->>'contextReady' is distinct from 'true' then
        return jsonb_build_object('error','context_not_ready');
    end if;
    capture := coalesce(j.context_index->'upload'->>'captureFingerprint', j.context_object);
    select * into c from public.scan_update_cursors where thread_id=p_thread_id and home_id=p_home_id;
    if not found then
        select context_index into baseline from public.scan_upload_jobs
            where home_id=p_home_id and revision::text=f.state->>'opening_scan_revision';
        baseline := coalesce(baseline, j.base_index, j.context_index);
        insert into public.scan_update_cursors(thread_id,home_id,baseline_index)
            values(p_thread_id,p_home_id,baseline);
        c.baseline_index := baseline;
    end if;
    if p_action = 'claim' then
        insert into public.scan_update_replies(thread_id,home_id,capture_key,context_index)
            values(p_thread_id,p_home_id,capture,j.context_index) on conflict do nothing;
    end if;
    select * into e from public.scan_update_replies
        where thread_id=p_thread_id and home_id=p_home_id and capture_key=capture;
    if not found then return jsonb_build_object('error','event_not_ready'); end if;
    if p_action = 'claim' then
        if e.status='pending' and coalesce(e.lease_until <= now(),true)
                and coalesce(e.retry_after <= now(),true) then
            update public.scan_update_replies set lease_token=(p_payload->>'token')::uuid,
                lease_until=now()+interval '120 seconds', context_index=j.context_index, attempts=attempts+1
                where event_id=e.event_id;
            claimed := true;
        end if;
    elsif p_action in ('finish','skip','fail') then
        if e.lease_token is distinct from (p_payload->>'token')::uuid
                or e.lease_until is null or e.lease_until <= now() then
            -- Lost HTTP response after commit: same stable event can be read
            -- again without making another model call.
            if e.status not in ('ready','skipped') then
                return jsonb_build_object('error','lease_lost');
            end if;
        elsif p_action = 'finish' then
            if nullif(p_payload->'message'->>'content','') is null then
                raise exception 'Empty scan update reply';
            end if;
            update public.scan_update_replies set status='ready', message=p_payload->'message',
                generated_at=now(),lease_token=null,lease_until=null where event_id=e.event_id;
        elsif p_action = 'skip' then
            update public.scan_update_replies set status='skipped',lease_token=null,lease_until=null
                where event_id=e.event_id;
            update public.scan_update_cursors set baseline_index=j.context_index
                where thread_id=p_thread_id and home_id=p_home_id;
        else
            update public.scan_update_replies set lease_token=null,lease_until=null,
                retry_after=now()+make_interval(secs => least(300, 10 * power(2, least(attempts-1,5))))
                where event_id=e.event_id;
        end if;
    elsif p_action = 'delivered' then
        if e.event_id::text is distinct from p_payload->>'eventId' or e.status <> 'ready' then
            return jsonb_build_object('error','event_not_ready');
        end if;
        update public.scan_update_replies set delivered_at=coalesce(delivered_at,now()) where event_id=e.event_id;
        update public.scan_update_cursors set baseline_index=e.context_index
            where thread_id=p_thread_id and home_id=p_home_id;
    else
        raise exception 'Unknown scan update transition';
    end if;
    select * into e from public.scan_update_replies where event_id=e.event_id;
    return jsonb_build_object('status',e.status,'eventId',e.event_id,'claimed',claimed,
        'message',e.message,'generatedAt',e.generated_at,'deliveredAt',e.delivered_at,
        'baselineIndex',c.baseline_index,'contextIndex',j.context_index);
end $$;
revoke all on function public.scan_update_transition(uuid,text,uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.scan_update_transition(uuid,text,uuid,uuid,text,jsonb) to service_role;
