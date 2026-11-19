-- Only the backend may discover or delete account data. Storage blobs are
-- removed through the Storage API, never by deleting storage.objects rows.
create table public.account_deletions (
  auth_user_id uuid primary key,
  manifest jsonb not null,
  created_at timestamptz not null default now()
);
alter table public.account_deletions enable row level security;
revoke all on public.account_deletions from public, anon, authenticated;
grant select, insert, update, delete on public.account_deletions to service_role;
create policy account_deletions_service_role on public.account_deletions
  for all to service_role using (true) with check (true);

create function public.account_deletion_manifest(p_user uuid)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  owners text[]; providers_owned text[]; homes text[]; requests text[]; threads text[];
  quote_ids text[]; saved jsonb; result jsonb; objects jsonb;
begin
  if p_user is null then raise exception 'User is required'; end if;
  select manifest into saved from public.account_deletions where auth_user_id = p_user;
  select coalesce(array_agg(distinct id), '{}') into owners from (
    select id::text from public.homeowners where auth_user_id = p_user
    union select jsonb_array_elements_text(coalesce(saved->'owners', '[]'))
  ) s;
  select coalesce(array_agg(distinct id), '{}') into providers_owned from (
    select id::text from public.providers where auth_user_id = p_user
    union select jsonb_array_elements_text(coalesce(saved->'providers', '[]'))
  ) s;
  select coalesce(array_agg(distinct id), '{}') into homes from (
    select home_id::text as id from public.scan_upload_heads where owner_id::text = any(owners)
    union select home_id::text from public.quote_intakes where owner_id::text = any(owners)
    union select record->>'homeId' from public.flow_quote_requests where homeowner_id::text = any(owners)
    union select jsonb_array_elements_text(coalesce(saved->'homes', '[]'))
  ) s where id is not null and id <> '';
  select coalesce(array_agg(distinct id), '{}') into requests from (
    select id from public.flow_quote_requests where homeowner_id::text = any(owners)
    union select jsonb_array_elements_text(coalesce(saved->'requests', '[]'))
  ) s;
  select coalesce(array_agg(distinct id), '{}') into threads from (
    select thread_id as id from public.flow_states where homeowner_id::text = any(owners)
    union select thread_id from public.flow_quote_requests where homeowner_id::text = any(owners)
    union select jsonb_array_elements_text(coalesce(saved->'threads', '[]'))
  ) s where id is not null;
  select coalesce(array_agg(distinct id), '{}') into quote_ids from (
    select id::text from public.quote_requests where homeowner_id::text = any(owners)
      or provider_id::text = any(providers_owned)
    union select jsonb_array_elements_text(coalesce(saved->'quoteIds', '[]'))
  ) s;
  result := jsonb_build_object('owners', owners, 'providers', providers_owned, 'homes', homes,
    'requests', requests, 'threads', threads, 'quoteIds', quote_ids);
  -- Keep the ownership manifest until Auth deletion succeeds, including when
  -- an Auth outage occurs after the profile rows have been removed.
  insert into public.account_deletions(auth_user_id, manifest) values(p_user, result)
    on conflict(auth_user_id) do update set manifest = excluded.manifest;

  select coalesce(jsonb_agg(jsonb_build_object('bucket', bucket_id, 'path', name)), '[]') into objects
  from (
    select bucket_id, name from storage.objects o where
      o.owner_id = p_user::text
      or (o.bucket_id in ('metashape-exports', 'home-assets') and split_part(o.name, '/', 1) = any(owners))
      or (o.bucket_id = 'provider-referral-uploads' and split_part(o.name, '/', 1) = any(providers_owned))
      or (o.bucket_id = 'quote-documents' and split_part(o.name, '/', 1) = any(quote_ids))
      or (o.bucket_id = 'home-assets' and (
        (split_part(o.name, '/', 1) = 'home-models' and split_part(o.name, '/', 2) = any(homes))
        or o.name = any(array(select 'home-indexes/' || unnest(homes) || '.json'))
      ))
    order by bucket_id, name limit 500
  ) batch;
  return result || jsonb_build_object('objects', objects);
end $$;
revoke all on function public.account_deletion_manifest(uuid) from public, anon, authenticated;
grant execute on function public.account_deletion_manifest(uuid) to service_role;

create function public.account_deletion_remove_profiles(p_user uuid)
returns void language plpgsql security invoker set search_path = '' as $$
declare manifest jsonb;
begin
  manifest := public.account_deletion_manifest(p_user);
  if jsonb_array_length(manifest->'objects') <> 0 then
    raise exception 'Account files must be removed first';
  end if;
  -- Provider ownership otherwise uses ON DELETE SET NULL, leaving PII behind.
  delete from public.providers where auth_user_id = p_user;
  delete from public.provider_profiles where auth_user_id = p_user;
  delete from public.homeowners where auth_user_id = p_user;
end $$;
revoke all on function public.account_deletion_remove_profiles(uuid) from public, anon, authenticated;
grant execute on function public.account_deletion_remove_profiles(uuid) to service_role;
