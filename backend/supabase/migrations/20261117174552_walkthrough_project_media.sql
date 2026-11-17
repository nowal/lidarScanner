-- Video projects share the existing owner, revision and immutable quote snapshot
-- lifecycle. No model readiness or geometry is fabricated for a walkthrough.
create function public.walkthrough_upload_transition(
    p_home_id uuid, p_action text, p_revision uuid default null,
    p_owner_id uuid default null, p_payload jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    state jsonb;
    idx jsonb := p_payload->'index';
    current_index jsonb;
begin
    if p_action <> 'publish' or p_owner_id is null or p_revision is null then
        raise exception 'Invalid walkthrough transition';
    end if;
    -- Lock before examining the previous kind. The begin transition uses the
    -- same lock and rejects foreign owners, stale generations and changed paths.
    insert into public.scan_upload_heads(home_id, owner_id)
        values(p_home_id, p_owner_id) on conflict(home_id) do nothing;
    select published_index into current_index from public.scan_upload_heads
        where home_id=p_home_id for update;
    state := public.scan_upload_transition(p_home_id,'read',null,p_owner_id);
    if state ? 'error' then return state; end if;
    current_index := coalesce(nullif(state->'contextIndex','null'::jsonb), current_index);
    if state->>'revision' is not null
       and coalesce(current_index->>'mediaType','scan') <> 'walkthrough' then
        return jsonb_build_object('error','media_type_mismatch');
    end if;
    if idx->>'mediaType' is distinct from 'walkthrough'
       or idx->'upload'->>'ownerId' is distinct from p_owner_id::text
       or idx->'upload'->>'revision' is distinct from p_revision::text
       or idx->'video'->>'object' is distinct from p_payload->>'objectPath'
       or idx->'upload'->>'videoReady' is distinct from 'true'
       or idx->'rooms' is distinct from '[]'::jsonb then
        raise exception 'Invalid walkthrough index';
    end if;
    state := public.scan_upload_transition(p_home_id,'begin',p_revision,p_owner_id,p_payload);
    if state ? 'error' then return state; end if;
    -- Retrying a published revision may not replace its contents.
    if state->'publishedIndex'->'upload'->>'revision' = p_revision::text then
        if state->'publishedIndex' <> idx then
            return jsonb_build_object('error','revision_mismatch');
        end if;
        return state;
    end if;
    update public.scan_upload_jobs set context_index=idx,
        progress='{"status":"done","stage":"ready","mediaType":"walkthrough"}'::jsonb,
        lease_token=null,lease_until=null where home_id=p_home_id and revision=p_revision;
    update public.scan_upload_heads set published_index=idx,updated_at=now() where home_id=p_home_id;
    return public.scan_upload_transition(p_home_id,'read',null,p_owner_id);
end $$;
revoke all on function public.walkthrough_upload_transition(uuid,text,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.walkthrough_upload_transition(uuid,text,uuid,uuid,jsonb) to service_role;
