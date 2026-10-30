-- Additive lifecycle. Existing requests, bids, scans and signed documents stay intact.
create table public.flow_quote_acceptances (
 request_id text primary key references public.flow_quote_requests(id) on delete cascade,
 quote jsonb not null check (quote->>'decision' = 'approved' and quote->>'approvalMethod' in ('signature','callback') and jsonb_typeof(quote->'approvalContact') = 'object'),
 provider_delivered text, provider_captured text,
 homeowner_delivered text, homeowner_captured text,
 provider_claim uuid, homeowner_claim uuid,
 provider_until timestamptz, homeowner_until timestamptz
);
alter table public.flow_quote_acceptances enable row level security;
revoke all on public.flow_quote_acceptances from public, anon, authenticated;
grant select, insert, update, delete on public.flow_quote_acceptances to service_role;

-- Freeze existing completed approvals, preserving prior notification receipts.
-- Historical approvals do not unexpectedly queue a new homeowner email.
insert into public.flow_quote_acceptances(request_id,quote,provider_delivered,provider_captured)
select r.id,q,q->>'acceptanceNotificationDeliveredAt',q->>'acceptanceNotificationCapturedAt'
from public.flow_quote_requests r cross join lateral jsonb_array_elements(coalesce(r.record->'quotes','[]')) q
where q->>'id'=r.record->>'selectedQuoteId' and q->>'decision'='approved'
 and q->>'approvalMethod' in ('signature','callback') and jsonb_typeof(q->'approvalContact')='object'
on conflict do nothing;

create function public.accept_quote_once(p_request text,p_owner uuid,p_home text,p_quote jsonb)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.flow_quote_requests; a public.flow_quote_acceptances;
begin
 select * into r from public.flow_quote_requests where id=p_request for update;
 if not found or r.homeowner_id is distinct from p_owner or r.record->>'homeId' is distinct from p_home then
  raise sqlstate 'PT404' using message='Request owner/home mismatch';
 end if;
 select * into a from public.flow_quote_acceptances where request_id=p_request;
 if found then
  if a.quote->>'id' <> p_quote->>'id' then raise sqlstate 'PT409' using message='Another quote is accepted'; end if;
  return to_jsonb(a);
 end if;
 if r.status='closed' or (r.record->>'selectedQuoteId' is not null and r.record->>'selectedQuoteId' <> p_quote->>'id') then
  raise sqlstate 'PT409' using message='Request is already settled';
 end if;
 if not exists(select 1 from jsonb_array_elements(coalesce(r.record->'quotes','[]')) q where q->>'id'=p_quote->>'id')
 and not exists(select 1 from public.flow_provider_responses where request_id=p_request and quote->>'id'=p_quote->>'id') then
  raise sqlstate 'PT404' using message='Quote does not belong to request';
 end if;
 insert into public.flow_quote_acceptances(request_id,quote,provider_delivered,provider_captured,homeowner_delivered,homeowner_captured)
 values(p_request,p_quote,p_quote->>'acceptanceNotificationDeliveredAt',p_quote->>'acceptanceNotificationCapturedAt',p_quote->>'homeownerConfirmationDeliveredAt',p_quote->>'homeownerConfirmationCapturedAt') returning * into a;
 -- Keep old readers and links compatible, atomically with acceptance.
 update public.flow_quote_requests set record=jsonb_set(jsonb_set(record,'{selectedQuoteId}',p_quote->'id'),'{quotes}',
  coalesce((select jsonb_agg(q) from jsonb_array_elements(coalesce(record->'quotes','[]')) q where q->>'id'<>p_quote->>'id'),'[]') || jsonb_build_array(p_quote))
 where id=p_request;
 return to_jsonb(a);
end $$;
revoke all on function public.accept_quote_once(text,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.accept_quote_once(text,uuid,text,jsonb) to service_role;

-- Every old/new writer preserves the accepted snapshot and its private document.
create function public.preserve_quote_acceptance() returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
declare a public.flow_quote_acceptances; incoming jsonb; frozen jsonb;
begin
 if new.homeowner_id is distinct from old.homeowner_id or new.record->>'homeId' is distinct from old.record->>'homeId' then
  if exists(select 1 from public.flow_quote_acceptances where request_id=old.id)
     or exists(select 1 from public.quote_intakes where state->'receipt'->>'quoteRequestId'=old.id) then
   raise sqlstate 'PT409' using message='Request association is immutable';
  end if;
 end if;
 select * into a from public.flow_quote_acceptances where request_id=new.id;
 if found then
  select q into incoming from jsonb_array_elements(coalesce(new.record->'quotes','[]')) q
   where q->>'id'=a.quote->>'id' and q->>'decidedAt'=a.quote->>'decidedAt';
  -- Rolling deploy compatibility: a previous worker may still save delivery
  -- stamps on the parent. Preserve them without letting it rewrite consent.
  if incoming is not null then
   update public.flow_quote_acceptances set
    provider_delivered=coalesce(provider_delivered,incoming->>'acceptanceNotificationDeliveredAt'),
    provider_captured=coalesce(provider_captured,incoming->>'acceptanceNotificationCapturedAt')
   where request_id=new.id returning * into a;
  end if;
  frozen=a.quote || jsonb_build_object('acceptanceNotificationDeliveredAt',a.provider_delivered,
    'acceptanceNotificationCapturedAt',a.provider_captured,'homeownerConfirmationDeliveredAt',a.homeowner_delivered,
    'homeownerConfirmationCapturedAt',a.homeowner_captured);
  new.record=jsonb_set(jsonb_set(new.record,'{selectedQuoteId}',a.quote->'id'),'{quotes}',
   coalesce((select jsonb_agg(q) from jsonb_array_elements(coalesce(new.record->'quotes','[]')) q where q->>'id'<>a.quote->>'id'),'[]') || jsonb_build_array(frozen));
 end if;
 return new;
end $$;
create trigger preserve_quote_acceptance before update on public.flow_quote_requests for each row execute function public.preserve_quote_acceptance();
revoke all on function public.preserve_quote_acceptance() from public,anon,authenticated;

create function public.quote_acceptance_delivery(p_request text,p_channel text,p_claim uuid,p_result text default null)
returns boolean language plpgsql security invoker set search_path=public,pg_temp as $$
declare a public.flow_quote_acceptances;
begin
 if p_channel not in ('provider','homeowner') then raise exception 'Invalid channel'; end if;
 perform 1 from public.flow_quote_requests where id=p_request for update;
 select * into a from public.flow_quote_acceptances where request_id=p_request for update;
 if not found then return false; end if;
 if p_result is null then
  if p_channel='provider' then
   if a.provider_delivered is not null or a.provider_until>now() then return false; end if;
   update public.flow_quote_acceptances set provider_claim=p_claim,provider_until=now()+interval '3 minutes' where request_id=p_request;
  else
   if a.homeowner_delivered is not null or a.homeowner_until>now() then return false; end if;
   update public.flow_quote_acceptances set homeowner_claim=p_claim,homeowner_until=now()+interval '3 minutes' where request_id=p_request;
  end if;
 else
  if p_channel='provider' then
   if a.provider_claim is distinct from p_claim then return false; end if;
   update public.flow_quote_acceptances set provider_claim=null,provider_until=null,
    provider_delivered=case when p_result='sent' then now()::text else provider_delivered end,
    provider_captured=case when p_result='outbox' then now()::text else provider_captured end where request_id=p_request;
  else
   if a.homeowner_claim is distinct from p_claim then return false; end if;
   update public.flow_quote_acceptances set homeowner_claim=null,homeowner_until=null,
    homeowner_delivered=case when p_result='sent' then now()::text else homeowner_delivered end,
    homeowner_captured=case when p_result='outbox' then now()::text else homeowner_captured end where request_id=p_request;
  end if;
 end if;
 if p_result in ('sent','outbox') then update public.flow_quote_requests set record=record where id=p_request; end if;
 return true;
end $$;
revoke all on function public.quote_acceptance_delivery(text,text,uuid,text) from public,anon,authenticated;
grant execute on function public.quote_acceptance_delivery(text,text,uuid,text) to service_role;

alter table public.quote_intakes add column request_id text unique references public.flow_quote_requests(id);
-- Only unambiguous, matching historical receipts are linked; never guess owners/homes.
update public.quote_intakes i set request_id=r.id from public.flow_quote_requests r
where i.state->'receipt'->>'quoteRequestId'=r.id and i.owner_id=r.homeowner_id and i.home_id::text=r.record->>'homeId'
 and (select count(*) from public.quote_intakes j where j.state->'receipt'->>'quoteRequestId'=r.id)=1;
create table public.quote_active_conversations (
 owner_id uuid not null references public.homeowners(id) on delete cascade,
 home_id uuid not null,
 conversation_id uuid not null references public.quote_intakes(id) on delete cascade,
 primary key(owner_id,home_id)
);
alter table public.quote_active_conversations enable row level security;
revoke all on public.quote_active_conversations from public,anon,authenticated;
grant select,insert,update,delete on public.quote_active_conversations to service_role;
-- Pick the latest saved conversation. Retain every other intake for recovery/history.
insert into public.quote_active_conversations
select distinct on(owner_id,home_id) owner_id,home_id,id from public.quote_intakes
order by owner_id,home_id,coalesce(state->'transcript'->-1->>'receivedAt',state->'transcript'->-2->>'receivedAt','') desc,id;

create function public.link_quote_intake_request() returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
declare r public.flow_quote_requests; rid text;
begin
 rid=new.state->'receipt'->>'quoteRequestId';
 if new.request_id is not null and new.request_id is distinct from rid then
  raise sqlstate 'PT409' using message='Request link must match the durable receipt';
 end if;
 if tg_op='UPDATE' and (new.owner_id<>old.owner_id or new.home_id<>old.home_id or (old.request_id is not null and rid is distinct from old.request_id)) then
  raise sqlstate 'PT409' using message='Intake association is immutable';
 end if;
 if rid is not null then
  select * into r from public.flow_quote_requests where id=rid;
  if not found or r.homeowner_id is distinct from new.owner_id or r.record->>'homeId' is distinct from new.home_id::text then
   raise sqlstate 'PT409' using message='Intake receipt owner/home mismatch';
  end if;
  new.request_id=rid;
 end if;
 return new;
end $$;
create trigger link_quote_intake_request before insert or update on public.quote_intakes for each row execute function public.link_quote_intake_request();
revoke all on function public.link_quote_intake_request() from public,anon,authenticated;

create function public.quote_intake_cycle(p_owner uuid,p_home uuid,p_conversation uuid,p_state jsonb,
 p_previous uuid default null,p_request text default null,p_legacy text default null)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare active uuid; i public.quote_intakes; prior public.quote_intakes; r public.flow_quote_requests;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_owner::text||':'||p_home::text,0));
 select conversation_id into active from public.quote_active_conversations where owner_id=p_owner and home_id=p_home;
 if p_previous is not null then
  select * into prior from public.quote_intakes where id=p_previous and owner_id=p_owner and home_id=p_home;
  if not found or prior.request_id is distinct from p_request then raise sqlstate 'PT409' using message='Wrong current request'; end if;
  -- All concurrent/retried starts return the single saved successor.
  if prior.state->>'nextConversationId' is not null then
   select * into i from public.quote_intakes where id=active;
   return to_jsonb(i);
  end if;
  if active is distinct from p_previous or not exists(select 1 from public.flow_quote_acceptances where request_id=p_request) then
   raise sqlstate 'PT409' using message='Accept the current quote before starting another chat';
  end if;
  if p_conversation=p_previous or exists(select 1 from public.quote_intakes where id=p_conversation) then raise sqlstate 'PT409' using message='Fresh conversation identity required'; end if;
  insert into public.quote_intakes(id,owner_id,home_id,version,state) values(p_conversation,p_owner,p_home,0,p_state);
  update public.quote_intakes set state=jsonb_set(state,'{nextConversationId}',to_jsonb(p_conversation::text)),version=version+1 where id=p_previous;
  update public.quote_active_conversations set conversation_id=p_conversation where owner_id=p_owner and home_id=p_home;
  active=p_conversation;
 elsif active is null then
  select * into i from public.quote_intakes where id=p_conversation;
  if found and (i.owner_id<>p_owner or i.home_id<>p_home) then raise sqlstate 'PT404' using message='Intake owner/home mismatch'; end if;
  if not found then
   -- Restore legacy direct requests even on another device. Explicit IDs must match.
   select * into r from public.flow_quote_requests where homeowner_id=p_owner and record->>'homeId'=p_home::text
     and (p_legacy is null or id=p_legacy) order by created_at desc,id desc limit 1;
   if p_legacy is not null and not found then raise sqlstate 'PT404' using message='Legacy request owner/home mismatch'; end if;
   if found then p_state=jsonb_set(p_state,'{receipt}',jsonb_build_object('quoteRequestId',r.id)); end if;
   insert into public.quote_intakes(id,owner_id,home_id,version,state) values(p_conversation,p_owner,p_home,0,p_state);
  end if;
  insert into public.quote_active_conversations values(p_owner,p_home,p_conversation);
  active=p_conversation;
 end if;
 select * into i from public.quote_intakes where id=active;
 if p_state->>'legacySubmissionId' is not null and i.state->>'receipt' is null
    and jsonb_array_length(coalesce(i.state->'transcript','[]'))=0 and i.state->>'pendingTurn' is null then
  if i.state->>'legacySubmissionId' is not null and i.state->>'legacySubmissionId'<>p_state->>'legacySubmissionId' then
   raise sqlstate 'PT409' using message='Recover the original pending submission';
  end if;
  update public.quote_intakes set state=jsonb_set(state,'{legacySubmissionId}',p_state->'legacySubmissionId'),version=version+1 where id=active returning * into i;
 end if;
 return to_jsonb(i);
end $$;
revoke all on function public.quote_intake_cycle(uuid,uuid,uuid,jsonb,uuid,text,text) from public,anon,authenticated;
grant execute on function public.quote_intake_cycle(uuid,uuid,uuid,jsonb,uuid,text,text) to service_role;

create index quote_active_conversations_intake on public.quote_active_conversations(conversation_id);

-- A service-role caller cannot accidentally cross owner/home boundaries by
-- writing the pointer directly instead of using the transition function.
create function public.validate_active_quote_conversation() returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
begin
 if not exists(select 1 from public.quote_intakes where id=new.conversation_id and owner_id=new.owner_id and home_id=new.home_id) then
  raise sqlstate 'PT409' using message='Active conversation owner/home mismatch';
 end if;
 return new;
end $$;
create trigger validate_active_quote_conversation before insert or update on public.quote_active_conversations
 for each row execute function public.validate_active_quote_conversation();
revoke all on function public.validate_active_quote_conversation() from public,anon,authenticated;

create function public.keep_quote_acceptance_immutable() returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
begin
 if new.request_id<>old.request_id or new.quote<>old.quote then
  raise sqlstate 'PT409' using message='Accepted quote and contact are immutable';
 end if;
 return new;
end $$;
create trigger keep_quote_acceptance_immutable before update on public.flow_quote_acceptances
 for each row execute function public.keep_quote_acceptance_immutable();
revoke all on function public.keep_quote_acceptance_immutable() from public,anon,authenticated;
