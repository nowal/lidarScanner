-- Synthetic home, jobs and assets are rolled back; existing owner is read only.
begin;
do $$
declare
    home uuid := gen_random_uuid();
    owner uuid := (select id from public.homeowners limit 1);
    rev uuid := gen_random_uuid(); later uuid := gen_random_uuid();
    t1 uuid := gen_random_uuid(); t2 uuid := gen_random_uuid();
    asset_id uuid := gen_random_uuid();
    result jsonb; old jsonb; package jsonb; models jsonb; assets jsonb;
begin
    old := jsonb_build_object('rooms',jsonb_build_array(jsonb_build_object('key','room-1','model',jsonb_build_object('object','old-room'))),
        'homeModel',jsonb_build_object('object','old-home'),'upload',jsonb_build_object('revision',rev::text,'modelsReady',true));
    perform public.scan_upload_transition(home,'begin',rev,owner,jsonb_build_object('generation',100,'objectPath','context.zip','previous_index',old));
    perform public.scan_upload_transition(home,'claim',rev,owner,jsonb_build_object('token',t1));
    perform public.scan_upload_transition(home,'context_complete',rev,owner,jsonb_build_object('token',t1,'index',old));
    package := '{"objectPath":"home-archive.usdz.gz","bytes":100}'::jsonb;
    perform public.scan_model_upload_transition(home,'begin',rev,owner,jsonb_build_object('package',package));
    result := public.scan_model_upload_transition(home,'claim',rev,owner,jsonb_build_object('token',t1));
    assert result->>'claimed'='true';
    result := public.scan_model_upload_transition(home,'claim',rev,owner,jsonb_build_object('token',t2));
    assert result->>'claimed'='false';
    result := public.scan_model_upload_transition(home,'begin',rev,owner,'{"package":{"objectPath":"other"}}');
    assert result->>'error'='archive_mismatch';
    perform public.scan_model_upload_transition(home,'heartbeat',rev,owner,jsonb_build_object('token',t1,
        'progress','{"status":"running","uploads":{"home":"saved-tus-session"}}'::jsonb));
    update public.scan_upload_jobs set model_lease_until=now()-interval '1 second' where home_id=home;
    result := public.scan_model_upload_transition(home,'claim',rev,owner,jsonb_build_object('token',t2));
    assert result->>'claimed'='true' and result->'progress'->'uploads'->>'home'='saved-tus-session';
    result := public.scan_model_upload_transition(home,'complete',rev,owner,jsonb_build_object('token',t1));
    assert result->>'error'='lease_lost';
    models := '{"home":{"object":"new-home"},"room-1":{"object":"new-room"}}'::jsonb;
    assets := jsonb_build_array(jsonb_build_object('id',asset_id,'asset_type','lidar_model','storage_path','new-home','metadata_json','{}'::jsonb));
    result := public.scan_model_upload_transition(home,'complete',rev,owner,jsonb_build_object('token',t2,
        'models',models-'room-1','assets',assets));
    assert result->>'error'='missing_model';
    assert not exists(select 1 from public.home_assets where id=asset_id);
    result := public.scan_upload_transition(home,'read',null,owner);
    assert result->'publishedIndex'=old;
    -- A ledger error must roll the model-pointer change back, too.
    begin
        perform public.scan_model_upload_transition(home,'complete',rev,owner,jsonb_build_object('token',t2,
            'models',models,'assets',jsonb_build_array(jsonb_build_object('id','invalid-uuid'))));
        raise exception 'Invalid asset unexpectedly published';
    exception when invalid_text_representation then null;
    end;
    result := public.scan_upload_transition(home,'read',null,owner);
    assert result->'publishedIndex'=old;
    result := public.scan_model_upload_transition(home,'complete',rev,owner,jsonb_build_object('token',t2,'models',models,'assets',assets));
    assert result->'progress'->>'status'='done';
    assert exists(select 1 from public.home_assets where id=asset_id and homeowner_id=owner);
    result := public.scan_model_upload_transition(home,'claim',rev,owner,jsonb_build_object('token',t1));
    assert result->>'claimed'='false', 'Retry after publication must not decompress again';
    result := public.scan_upload_transition(home,'read',null,owner);
    assert result->'publishedIndex'->'homeModel'->>'object'='new-home';
    perform public.scan_upload_transition(home,'begin',later,owner,jsonb_build_object('generation',200,'objectPath','context.zip'));
    result := public.scan_model_upload_transition(home,'complete',rev,owner,jsonb_build_object('token',t2,'models',models));
    assert result->>'error'='revision_mismatch';
    result := public.scan_model_upload_transition(home,'read',later,gen_random_uuid());
    assert result->>'error'='owner_mismatch';
    assert not has_function_privilege('anon','public.scan_model_upload_transition(uuid,text,uuid,uuid,jsonb)','EXECUTE');
    assert not has_function_privilege('authenticated','public.scan_model_upload_transition(uuid,text,uuid,uuid,jsonb)','EXECUTE');
end $$;
rollback;
select 'compressed model leases, resume, atomic assets/publication and stale-worker fencing passed' as result;
