-- Independent model leases let context remain ready while a compressed home is
-- expanded. Every operation takes the same home lock as revision publication.
alter table public.scan_upload_jobs
    add column model_package jsonb,
    add column model_progress jsonb,
    add column model_lease_token uuid,
    add column model_lease_until timestamptz;

create function public.scan_model_upload_transition(
    p_home_id uuid, p_action text, p_revision uuid,
    p_owner_id uuid, p_payload jsonb default '{}'::jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    h public.scan_upload_heads%rowtype;
    j public.scan_upload_jobs%rowtype;
    claimed boolean := false;
    published jsonb;
    asset jsonb;
begin
    select * into h from public.scan_upload_heads where home_id=p_home_id for update;
    if not found or h.current_revision is distinct from p_revision then
        return jsonb_build_object('error','revision_mismatch');
    end if;
    if p_owner_id is null or h.owner_id <> p_owner_id then
        return jsonb_build_object('error','owner_mismatch');
    end if;
    select * into j from public.scan_upload_jobs where home_id=p_home_id and revision=p_revision;
    if p_action = 'begin' then
        if j.context_index is null then return jsonb_build_object('error','context_not_ready'); end if;
        if j.model_package is not null and j.model_package <> p_payload->'package' then
            return jsonb_build_object('error','archive_mismatch');
        end if;
        if j.model_package is null then
            update public.scan_upload_jobs set model_package=p_payload->'package',
                model_progress='{"status":"queued","stage":"preparing_models"}'::jsonb
                where home_id=p_home_id and revision=p_revision;
        end if;
    elsif p_action = 'claim' then
        if j.model_package is not null
           and j.model_progress->>'status' <> 'done'
           and (j.model_progress->>'status' <> 'failed' or j.model_progress->>'retryable' = 'true')
           and (j.model_lease_until is null or j.model_lease_until <= now()) then
            update public.scan_upload_jobs set model_lease_token=(p_payload->>'token')::uuid,
                model_lease_until=now()+interval '120 seconds',
                model_progress=j.model_progress || '{"status":"running","stage":"preparing_models"}'::jsonb
                where home_id=p_home_id and revision=p_revision;
            claimed := true;
        end if;
    elsif p_action in ('heartbeat','complete','fail') then
        if j.model_lease_token is null or j.model_lease_token is distinct from (p_payload->>'token')::uuid
           or j.model_lease_until is null or j.model_lease_until <= now() then
            return jsonb_build_object('error','lease_lost');
        end if;
        if p_action = 'complete' then
            published := public.scan_upload_transition(p_home_id,'publish',p_revision,p_owner_id,p_payload);
            if published ? 'error' then return published; end if;
            -- Legacy asset readers and the authoritative model pointer move
            -- together. A ledger failure rolls back publication as well.
            for asset in select value from jsonb_array_elements(p_payload->'assets') loop
                insert into public.home_assets(id,homeowner_id,asset_type,storage_path,source,metadata_json)
                values ((asset->>'id')::uuid,p_owner_id,asset->>'asset_type',asset->>'storage_path',
                    'ios_automatic_scan_upload',asset->'metadata_json') on conflict(id) do nothing;
            end loop;
            update public.scan_upload_jobs set model_progress='{"status":"done","stage":"ready"}'::jsonb,
                model_lease_token=null,model_lease_until=null where home_id=p_home_id and revision=p_revision;
        elsif p_action = 'fail' then
            update public.scan_upload_jobs set model_progress=p_payload->'progress',
                model_lease_token=null,model_lease_until=null where home_id=p_home_id and revision=p_revision;
        else
            update public.scan_upload_jobs set model_progress=p_payload->'progress',
                model_lease_until=now()+interval '120 seconds' where home_id=p_home_id and revision=p_revision;
        end if;
    elsif p_action <> 'read' then
        raise exception 'Unknown model upload transition';
    end if;
    select * into j from public.scan_upload_jobs where home_id=p_home_id and revision=p_revision;
    return jsonb_build_object('revision',p_revision,'package',j.model_package,'progress',j.model_progress,
        'leaseActive',coalesce(j.model_lease_until>now(),false),'claimed',claimed);
end $$;
revoke all on function public.scan_model_upload_transition(uuid,text,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.scan_model_upload_transition(uuid,text,uuid,uuid,jsonb) to service_role;
