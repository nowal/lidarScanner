-- Run against a migrated database. All synthetic state is rolled back.
begin;
do $$
declare
    home uuid := gen_random_uuid();
    owner uuid := (select id from public.homeowners limit 1);
    r1 uuid := gen_random_uuid(); r2 uuid := gen_random_uuid(); r3 uuid := gen_random_uuid();
    t1 uuid := gen_random_uuid(); t2 uuid := gen_random_uuid();
    result jsonb; old jsonb; fresh jsonb;
begin
    assert owner is not null, 'Requires one existing homeowner (never modified)';
    old := jsonb_build_object('rooms',jsonb_build_array(jsonb_build_object('key','room-1','model',jsonb_build_object('object','old-room'))),
        'homeModel',jsonb_build_object('object','old-home'),'upload',jsonb_build_object('revision',r1::text,'modelsReady',true));
    result := public.scan_upload_transition(home,'begin',r1,owner,jsonb_build_object('generation',100,'objectPath','context-one.zip','previous_index',old));
    assert result->'publishedIndex'=old;
    result := public.scan_upload_transition(home,'claim',r1,owner,jsonb_build_object('token',t1));
    assert result->>'claimed'='true';
    result := public.scan_upload_transition(home,'claim',r1,owner,jsonb_build_object('token',t2));
    assert result->>'claimed'='false', 'Duplicate worker must not acquire a live lease';
    result := public.scan_upload_transition(home,'context_complete',r1,owner,jsonb_build_object('token',t2,'index',old));
    assert result->>'error'='lease_lost';
    update public.scan_upload_jobs set lease_until=now()-interval '1 second' where home_id=home;
    result := public.scan_upload_transition(home,'claim',r1,owner,jsonb_build_object('token',t2));
    assert result->>'claimed'='true', 'A crashed worker lease must be reclaimable';
    result := public.scan_upload_transition(home,'heartbeat',r1,owner,jsonb_build_object('token',t1,'progress','{}'::jsonb));
    assert result->>'error'='lease_lost', 'Expired worker is fenced after reclaim';
    result := public.scan_upload_transition(home,'begin',r2,owner,jsonb_build_object('generation',200,'objectPath','context-two.zip'));
    assert result->'publishedIndex'=old, 'Beginning update must retain active model';
    result := public.scan_upload_transition(home,'context_complete',r1,owner,jsonb_build_object('token',t2,'index',old));
    assert result->>'error'='revision_mismatch';
    result := public.scan_upload_transition(home,'begin',r3,owner,jsonb_build_object('generation',150,'objectPath','late-context.zip'));
    assert result->>'error'='revision_mismatch', 'Late first request from old build is fenced';
    result := public.scan_upload_transition(home,'claim',r2,owner,jsonb_build_object('token',t2));
    fresh := jsonb_set(old,'{upload}',jsonb_build_object('revision',r2::text,'contextReady',true,'modelsReady',false,'durableV2',true));
    result := public.scan_upload_transition(home,'context_complete',r2,owner,jsonb_build_object('token',t2,'index',fresh));
    assert result->'publishedIndex'=old and result->'contextIndex'=fresh, 'Context must not publish new models';
    result := public.scan_upload_transition(home,'publish',r2,owner,jsonb_build_object('models',jsonb_build_object('home',jsonb_build_object('object','new-home'))));
    assert result->>'error'='missing_model';
    result := public.scan_upload_transition(home,'read',null,owner);
    assert result->'publishedIndex'=old, 'Incomplete publish must leave old references untouched';
    result := public.scan_upload_transition(home,'publish',r2,owner,jsonb_build_object('models',jsonb_build_object(
        'home',jsonb_build_object('object','new-home'),'room-1',jsonb_build_object('object','new-room'))));
    assert result->'publishedIndex'->'homeModel'->>'object'='new-home';
    assert result->'publishedIndex'->'rooms'->0->'model'->>'object'='new-room';
    assert result->'publishedIndex'->'upload'->>'modelsReady'='true';
    result := public.scan_upload_transition(home,'publish',r1,owner,'{}'::jsonb);
    assert result->>'error'='revision_mismatch', 'Old publish cannot replace new models';
    result := public.scan_upload_transition(home,'begin',r3,owner,jsonb_build_object('generation',300,'objectPath','context-two.zip'));
    assert result->'previousIndex'->'upload'->>'revision'=r2::text;
    assert result->'contextIndex'->'upload'->>'revision'=r3::text;
    assert result->'contextIndex'->'upload'->>'modelsReady'='false';
    assert result->'publishedIndex'->'homeModel'->>'object'='new-home';
    result := public.scan_upload_transition(home,'read',null,gen_random_uuid());
    assert result->>'error'='owner_mismatch';
    assert not has_function_privilege('anon','public.scan_upload_transition(uuid,text,uuid,uuid,jsonb)','EXECUTE');
    assert not has_function_privilege('authenticated','public.scan_upload_transition(uuid,text,uuid,uuid,jsonb)','EXECUTE');
end $$;
rollback;
select 'leases, stale revisions, atomic publication, reuse, ownership and role grants passed; synthetic data rolled back' as result;
