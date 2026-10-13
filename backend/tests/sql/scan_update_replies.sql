-- Run after the scan upload and scan-update migrations. Synthetic rows roll back.
begin;
set local role service_role;
do $$
declare
    home uuid := gen_random_uuid(); owner uuid := (select id from public.homeowners limit 1);
    first uuid := gen_random_uuid(); next uuid := gen_random_uuid(); rebuild uuid := gen_random_uuid();
    newer uuid := gen_random_uuid(); token uuid := gen_random_uuid(); loser uuid := gen_random_uuid();
    thread text := 'scan-event-test-' || gen_random_uuid()::text;
    old jsonb; fresh jsonb; original_state jsonb; result jsonb; receipt uuid; baseline jsonb;
begin
    old := jsonb_build_object('rooms',jsonb_build_array(jsonb_build_object('key','room-1','contextFingerprint','one')),
        'upload',jsonb_build_object('revision',first,'contextReady',true,'modelsReady',false,'captureFingerprint','capture-one'));
    fresh := jsonb_build_object('rooms',jsonb_build_array(jsonb_build_object('key','room-1','contextFingerprint','two')),
        'upload',jsonb_build_object('revision',next,'contextReady',true,'modelsReady',false,'captureFingerprint','capture-two'));
    original_state := jsonb_build_object('home_id',home,'opening_scan_revision',first,'active_room_key','room-1',
        'user_turns',8,'step',9,'request_accepted',true,'slots',jsonb_build_object('first_name','Jo','address','123 Sample'),
        'quote_request',jsonb_build_object('id','existing','presented_quote_count',2));
    insert into public.flow_states(thread_id,homeowner_id,state) values(thread,owner,original_state);
    perform public.scan_upload_transition(home,'begin',first,owner,jsonb_build_object('generation',1,'objectPath','one.zip'));
    perform public.scan_upload_transition(home,'claim',first,owner,jsonb_build_object('token',token));
    result := public.scan_update_transition(home,thread,first,owner,'claim',jsonb_build_object('token',token));
    assert result->>'error'='context_not_ready', 'Pending ingestion cannot claim receipt';
    perform public.scan_upload_transition(home,'fail',first,owner,jsonb_build_object('token',token,'progress',jsonb_build_object('status','failed')));
    result := public.scan_update_transition(home,thread,first,owner,'claim',jsonb_build_object('token',token));
    assert result->>'error'='context_not_ready', 'Failed ingestion cannot claim receipt';
    perform public.scan_upload_transition(home,'claim',first,owner,jsonb_build_object('token',token));
    perform public.scan_upload_transition(home,'context_complete',first,owner,jsonb_build_object('token',token,'index',old));
    result := public.scan_update_transition(home,thread,first,owner,'claim',jsonb_build_object('token',token));
    assert result->'baselineIndex'=old and result->'contextIndex'=old, 'First opener is the baseline';
    perform public.scan_update_transition(home,thread,first,owner,'skip',jsonb_build_object('token',token));
    perform public.scan_upload_transition(home,'begin',next,owner,jsonb_build_object('generation',2,'objectPath','two.zip'));
    perform public.scan_upload_transition(home,'claim',next,owner,jsonb_build_object('token',token));
    perform public.scan_upload_transition(home,'context_complete',next,owner,jsonb_build_object('token',token,'index',fresh));
    result := public.scan_update_transition(home,thread,next,gen_random_uuid(),'claim',jsonb_build_object('token',token));
    assert result->>'error'='owner_mismatch';
    result := public.scan_update_transition(home,thread,next,null,'claim',jsonb_build_object('token',token));
    assert result->>'error'='owner_mismatch';
    result := public.scan_update_transition(home,'foreign-thread',next,owner,'claim',jsonb_build_object('token',token));
    assert result->>'error'='thread_not_ready';
    update public.flow_states set state=jsonb_set(state,'{home_id}',to_jsonb(gen_random_uuid()::text)) where thread_id=thread;
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',token));
    assert result->>'error'='thread_home_mismatch';
    update public.flow_states set state=state - 'home_id' || jsonb_build_object('home_id',home) where thread_id=thread;
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',token));
    receipt := (result->>'eventId')::uuid;
    assert result->>'claimed'='true' and result->'baselineIndex'=old;
    assert result->'contextIndex'->'upload'->>'modelsReady'='false', 'No model-upload wait';
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',loser));
    assert result->>'claimed'='false', 'Concurrent worker cannot call AI';
    result := public.scan_update_transition(home,thread,next,owner,'finish',jsonb_build_object('token',loser,'message',jsonb_build_object('content','wrong')));
    assert result->>'error'='lease_lost';
    -- Failed/crashed generation is reclaimable without a delivered flag.
    perform public.scan_update_transition(home,thread,next,owner,'fail',jsonb_build_object('token',token));
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',loser));
    assert result->>'claimed'='false', 'Backoff avoids an AI retry storm';
    update public.scan_update_replies set retry_after=now()-interval '1 second' where event_id=receipt;
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',loser));
    assert result->>'claimed'='true';
    update public.scan_update_replies set lease_until=now()-interval '1 second' where event_id=receipt;
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',token));
    assert result->>'claimed'='true', 'Crashed worker lease expires';
    result := public.scan_update_transition(home,thread,next,owner,'finish',jsonb_build_object('token',token,
        'message',jsonb_build_object('id',receipt,'role','assistant','content','Updated kitchen details.')));
    assert result->>'status'='ready' and result->>'generatedAt' is not null and result->>'deliveredAt' is null;
    result := public.scan_update_transition(home,thread,next,owner,'claim',jsonb_build_object('token',loser));
    assert result->>'claimed'='false' and result->'message'->>'content'='Updated kitchen details.';
    -- Model-only rebuild uses the same immutable capture receipt, even when
    -- its upload revision changes before the phone receives the generated reply.
    perform public.scan_upload_transition(home,'begin',rebuild,owner,jsonb_build_object('generation',3,'objectPath','two.zip'));
    result := public.scan_update_transition(home,thread,next,owner,'delivered',jsonb_build_object('eventId',receipt));
    assert result->>'error'='revision_mismatch';
    result := public.scan_update_transition(home,thread,rebuild,owner,'claim',jsonb_build_object('token',loser));
    assert result->>'eventId'=receipt::text and result->>'claimed'='false';
    result := public.scan_update_transition(home,thread,rebuild,owner,'delivered',jsonb_build_object('eventId',gen_random_uuid()));
    assert result->>'error'='event_not_ready';
    result := public.scan_update_transition(home,thread,rebuild,owner,'delivered',jsonb_build_object('eventId',receipt));
    assert result->>'deliveredAt' is not null;
    result := public.scan_update_transition(home,thread,rebuild,owner,'claim',jsonb_build_object('token',loser));
    assert result->'message'->>'content'='Updated kitchen details.', 'Delivery does not destroy recoverable reply';
    select baseline_index into baseline from public.scan_update_cursors where thread_id=thread and home_id=home;
    assert baseline=fresh, 'Only delivered context advances the baseline';
    perform public.scan_upload_transition(home,'begin',newer,owner,jsonb_build_object('generation',4,'objectPath','three.zip'));
    result := public.scan_update_transition(home,thread,rebuild,owner,'finish',jsonb_build_object('token',token));
    assert result->>'error'='revision_mismatch', 'Superseding update fences a late reply';
    assert (select f.state from public.flow_states f where f.thread_id=thread)=original_state, 'Flow/room/contact/quote state unchanged';
    assert not has_function_privilege('anon','public.scan_update_transition(uuid,text,uuid,uuid,text,jsonb)','EXECUTE');
    assert not has_function_privilege('authenticated','public.scan_update_transition(uuid,text,uuid,uuid,text,jsonb)','EXECUTE');
    assert has_function_privilege('service_role','public.scan_update_transition(uuid,text,uuid,uuid,text,jsonb)','EXECUTE');
    assert not has_table_privilege('anon','public.scan_update_replies','SELECT');
    assert not has_table_privilege('authenticated','public.scan_update_replies','INSERT');
    assert (select relrowsecurity from pg_class where oid='public.scan_update_replies'::regclass);
    delete from public.flow_states where thread_id=thread;
    assert not exists(select 1 from public.scan_update_replies where event_id=receipt), 'Deletion cascades event data';
end $$;
rollback;
