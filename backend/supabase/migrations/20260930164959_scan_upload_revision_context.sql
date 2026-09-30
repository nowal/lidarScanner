-- Keep homeowner naming/context even before a first model set is published.
-- Client generations are monotonic on the phone; they need not share the old
-- server-assigned generation's clock when upgrading a legacy client.
alter table public.scan_upload_jobs add column base_index jsonb;

create or replace function public.scan_upload_transition(
    p_home_id uuid, p_action text, p_revision uuid default null,
    p_owner_id uuid default null, p_payload jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    h public.scan_upload_heads%rowtype;
    j public.scan_upload_jobs%rowtype;
    previous public.scan_upload_jobs%rowtype;
    g bigint;
    idx jsonb;
    item jsonb;
    room jsonb;
    rooms jsonb;
    claimed boolean := false;
begin
    if p_action = 'begin' then
        insert into public.scan_upload_heads(home_id, owner_id, published_index)
        values (p_home_id, p_owner_id,
            case when p_payload->'previous_index'->'upload'->>'modelsReady' = 'true'
                 then p_payload->'previous_index' else null end)
        on conflict (home_id) do nothing;
    end if;
    select * into h from public.scan_upload_heads where home_id = p_home_id for update;
    if not found then return null; end if;
    if p_owner_id is not null and h.owner_id <> p_owner_id then
        return jsonb_build_object('error','owner_mismatch');
    end if;
    select * into previous from public.scan_upload_jobs
        where home_id = p_home_id and revision = h.current_revision;
    if p_action = 'begin' then
        select * into j from public.scan_upload_jobs where home_id = p_home_id and revision = p_revision;
        if found then
            if h.current_revision <> p_revision or j.context_object <> p_payload->>'objectPath' then
                return jsonb_build_object('error','revision_mismatch');
            end if;
        else
            g := coalesce((p_payload->>'generation')::bigint,
                          greatest((extract(epoch from clock_timestamp()) * 1000)::bigint, coalesce(previous.generation,0)+1));
            if previous.revision is not null and
               ((previous.client_generation and p_payload->>'generation' is null)
                or (previous.client_generation and g <= previous.generation)) then
                return jsonb_build_object('error','revision_mismatch');
            end if;
            -- A geometry-only rebuild can reuse its already indexed context.
            if previous.context_object = p_payload->>'objectPath' and previous.context_index is not null then
                idx := jsonb_set(previous.context_index, '{upload,revision}', to_jsonb(p_revision::text));
                idx := jsonb_set(idx, '{upload,modelsReady}', 'false'::jsonb);
            end if;
            insert into public.scan_upload_jobs(home_id,revision,generation,client_generation,context_object,context_index,base_index,progress)
            values (p_home_id,p_revision,g,p_payload->>'generation' is not null,p_payload->>'objectPath',idx,coalesce(previous.context_index, previous.base_index, h.published_index, p_payload->'previous_index'),
                case when idx is null then '{"status":"queued","stage":"reading_scan"}'::jsonb
                     else jsonb_build_object('status','done','stage','ready','roomCount',jsonb_array_length(idx->'rooms')) end);
            update public.scan_upload_heads set current_revision=p_revision,updated_at=now() where home_id=p_home_id;
            h.current_revision := p_revision;
        end if;
    end if;
    select * into j from public.scan_upload_jobs where home_id=p_home_id and revision=h.current_revision;
    if p_action not in ('begin','read','delete') and p_revision is distinct from h.current_revision then
        return jsonb_build_object('error','revision_mismatch');
    end if;
    if p_action = 'claim' then
        if j.context_index is null and (j.lease_until is null or j.lease_until <= now()) then
            update public.scan_upload_jobs set lease_token=(p_payload->>'token')::uuid,
                lease_until=now()+interval '120 seconds', progress='{"status":"running","stage":"reading_scan"}'::jsonb
            where home_id=p_home_id and revision=p_revision;
            claimed := true;
        end if;
    elsif p_action in ('heartbeat','context_complete','fail') then
        if j.lease_token is distinct from (p_payload->>'token')::uuid or j.lease_until <= now() then
            return jsonb_build_object('error','lease_lost');
        end if;
        if p_action = 'context_complete' then
            update public.scan_upload_jobs set context_index=p_payload->'index',
                progress=jsonb_build_object('status','done','stage','ready','roomCount',jsonb_array_length(p_payload->'index'->'rooms')),
                lease_token=null,lease_until=null where home_id=p_home_id and revision=p_revision;
        elsif p_action = 'fail' then
            update public.scan_upload_jobs set progress=p_payload->'progress',lease_token=null,lease_until=null
                where home_id=p_home_id and revision=p_revision;
        else
            update public.scan_upload_jobs set progress=p_payload->'progress',lease_until=now()+interval '120 seconds'
                where home_id=p_home_id and revision=p_revision;
        end if;
    elsif p_action = 'publish' then
        if j.context_index is null then return jsonb_build_object('error','context_not_ready'); end if;
        idx := j.context_index;
        rooms := '[]'::jsonb;
        for room in select value from jsonb_array_elements(idx->'rooms') loop
            item := p_payload->'models'->(room->>'key');
            if item is null then return jsonb_build_object('error','missing_model'); end if;
            rooms := rooms || jsonb_build_array(jsonb_set(room,'{model}',item));
        end loop;
        if p_payload->'models'->'home' is null then return jsonb_build_object('error','missing_model'); end if;
        idx := jsonb_set(idx,'{rooms}',rooms);
        idx := jsonb_set(idx,'{homeModel}',p_payload->'models'->'home');
        idx := jsonb_set(idx,'{upload,modelsReady}','true'::jsonb);
        update public.scan_upload_jobs set context_index=idx where home_id=p_home_id and revision=p_revision;
        update public.scan_upload_heads set published_index=idx,updated_at=now() where home_id=p_home_id;
    elsif p_action = 'rename' then
        -- Preserve edits without letting a cached snapshot rewrite model links.
        idx := j.context_index;
        if idx is not null then
            rooms := '[]'::jsonb;
            for room in select value from jsonb_array_elements(idx->'rooms') loop
                item := p_payload->'names'->(room->>'key');
                if item is not null then room := room || item; end if;
                rooms := rooms || jsonb_build_array(room);
            end loop;
            idx := jsonb_set(idx,'{rooms}',rooms);
            update public.scan_upload_jobs set context_index=idx where home_id=p_home_id and revision=p_revision;
            if h.published_index->'upload'->>'revision'=p_revision::text then
                update public.scan_upload_heads set published_index=idx where home_id=p_home_id;
            end if;
        end if;
    elsif p_action = 'delete' then
        delete from public.scan_upload_heads where home_id=p_home_id;
        return jsonb_build_object('deleted',true);
    elsif p_action not in ('begin','read') then
        raise exception 'Unknown scan upload transition';
    end if;
    select * into j from public.scan_upload_jobs where home_id=p_home_id and revision=h.current_revision;
    select * into h from public.scan_upload_heads where home_id=p_home_id;
    return jsonb_build_object('ownerId',h.owner_id,'revision',j.revision,'objectPath',j.context_object,
        'generation',j.generation,'progress',j.progress,'contextIndex',j.context_index,
        'publishedIndex',h.published_index,'previousIndex',j.base_index,'leaseActive',coalesce(j.lease_until>now(),false),'claimed',claimed);
end $$;
revoke all on function public.scan_upload_transition(uuid,text,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.scan_upload_transition(uuid,text,uuid,uuid,jsonb) to service_role;
