-- Private, service-role-only intake state. Application checks verified owner
-- and scan on EVERY access; version predicates fence concurrent workers.
create table public.quote_intakes (
  id uuid primary key,
  owner_id uuid not null references public.homeowners(id) on delete cascade,
  home_id uuid not null,
  version bigint not null check (version >= 0),
  state jsonb not null check (jsonb_typeof(state) = 'object')
);
alter table public.quote_intakes enable row level security;
revoke all on public.quote_intakes from public, anon, authenticated;
grant select, insert, update, delete on public.quote_intakes to service_role;
create index quote_intakes_owner_home on public.quote_intakes(owner_id, home_id);
