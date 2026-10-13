-- Safe on the deployed schema: every synthetic row is rolled back.
begin;
do $$
declare request_key text := 'qr_receipt_check_' || gen_random_uuid()::text;
        receipt uuid := gen_random_uuid();
begin
  if not (select relrowsecurity from pg_class where oid='public.flow_provider_responses'::regclass)
    or has_table_privilege('anon','public.flow_provider_responses','select')
    or has_table_privilege('authenticated','public.flow_provider_responses','insert')
    or not has_table_privilege('service_role','public.flow_provider_responses','insert') then
    raise exception 'Response table access is incorrect';
  end if;
  insert into public.flow_quote_requests(id,thread_id,status,record)
    values(request_key,'isolated-receipt-check','closed','{}');
  insert into public.flow_provider_responses(id,request_id,fingerprint,quote)
    values(receipt,request_key,repeat('a',64),'{"providerName":"Receipt check","earliestAvailableDate":"2026-11-02"}');
  if (select quote->>'earliestAvailableDate' from public.flow_provider_responses where id=receipt) <> '2026-11-02' then
    raise exception 'Calendar date failed to round-trip';
  end if;
  begin
    insert into public.flow_provider_responses(id,request_id,fingerprint,quote)
      values(receipt,request_key,repeat('a',64),'{}');
    raise exception 'Duplicate response ID was accepted';
  exception when unique_violation then null;
  end;
  delete from public.flow_quote_requests where id=request_key;
  if exists(select 1 from public.flow_provider_responses where id=receipt) then
    raise exception 'Response deletion did not cascade';
  end if;
end $$;
rollback;
select 'RLS/grants, date persistence, duplicate ID rejection, and deletion cascade passed; synthetic rows rolled back' as result;
