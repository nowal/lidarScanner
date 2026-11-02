-- Saved threads are independent of quote acceptance. Existing receipts and
-- acceptance functions remain intact for older clients and retry recovery.
alter table public.quote_intakes add column created_at timestamptz not null default now();
alter table public.quote_intakes add column updated_at timestamptz not null default now();
update public.quote_intakes i set
 created_at=coalesce((select min((t->>'receivedAt')::timestamptz) from jsonb_array_elements(i.state->'transcript') t),(select created_at from public.flow_quote_requests where id=i.request_id),i.created_at),
 updated_at=coalesce((select max((t->>'receivedAt')::timestamptz) from jsonb_array_elements(i.state->'transcript') t),(select created_at from public.flow_quote_requests where id=i.request_id),i.updated_at);
create index quote_intakes_owner_updated on public.quote_intakes(owner_id,updated_at desc,id desc);

create function public.touch_quote_thread() returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
begin
 new.created_at=old.created_at;
 new.updated_at=now();
 return new;
end $$;
create trigger touch_quote_thread before update of state on public.quote_intakes
 for each row execute function public.touch_quote_thread();
revoke all on function public.touch_quote_thread() from public,anon,authenticated;

create function public.select_quote_thread(p_owner uuid,p_home uuid,p_conversation uuid,p_state jsonb default null)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare i public.quote_intakes;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_owner::text||':'||p_home::text,0));
 select * into i from public.quote_intakes where id=p_conversation;
 if found then
  if i.owner_id<>p_owner or i.home_id<>p_home then
   raise sqlstate 'PT404' using message='Thread not found';
  end if;
 else
  if p_state is null then raise sqlstate 'PT404' using message='Thread not found'; end if;
  insert into public.quote_intakes(id,owner_id,home_id,version,state)
   values(p_conversation,p_owner,p_home,0,p_state) returning * into i;
 end if;
 insert into public.quote_active_conversations(owner_id,home_id,conversation_id) values(p_owner,p_home,p_conversation)
 on conflict(owner_id,home_id) do update set conversation_id=excluded.conversation_id;
 return to_jsonb(i);
end $$;
revoke all on function public.select_quote_thread(uuid,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.select_quote_thread(uuid,uuid,uuid,jsonb) to service_role;
