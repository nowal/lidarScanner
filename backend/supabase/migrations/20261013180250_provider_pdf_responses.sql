-- Immutable, independently inserted provider responses avoid lost updates when
-- several providers respond while conversation/operations saves the parent.
create table public.flow_provider_responses (
    id uuid primary key,
    request_id text not null references public.flow_quote_requests(id) on delete cascade,
    fingerprint text not null check (length(fingerprint) = 64),
    quote jsonb not null,
    created_at timestamptz not null default now()
);
create index flow_provider_responses_request_idx
    on public.flow_provider_responses(request_id, created_at, id);
alter table public.flow_provider_responses enable row level security;
revoke all on public.flow_provider_responses from public, anon, authenticated;
grant select, insert, delete on public.flow_provider_responses to service_role;
