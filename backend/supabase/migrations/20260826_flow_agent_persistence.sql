-- Flow agent persistence (SOW §4 logging deliverable, §12 retention/deletion).
--
-- These tables are written exclusively by the backend with the service role;
-- no client role can read or write them (RLS enabled with no policies, plus
-- explicit revokes). homeowner_id is nullable on purpose: beta conversations
-- may be anonymous until the app sends X-Homeowner-Token, and the journal
-- must not lose those turns. PII in journal records is masked at write by
-- the backend (see backend/app/flow/pii.py).

-- Server-owned flow state per conversation thread (survives deploys; the
-- signed client token remains the first-choice source, this the second).
create table if not exists public.flow_states (
    thread_id text primary key,
    homeowner_id uuid references public.homeowners (id) on delete cascade,
    state jsonb not null,
    updated_at timestamptz not null default now()
);

-- Per-turn wording journal: which wording ran at each step, what the user
-- said, gates evaluated, suppressed drafts. Masked at write.
create table if not exists public.flow_journal (
    id uuid primary key default gen_random_uuid(),
    thread_id text not null,
    homeowner_id uuid references public.homeowners (id) on delete cascade,
    kind text not null default 'chat',
    step int,
    record jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists flow_journal_thread_idx
    on public.flow_journal (thread_id, created_at);
create index if not exists flow_journal_created_idx
    on public.flow_journal (created_at);

-- Quote requests + lead packages + returned quotes (full record as jsonb;
-- status/thread lifted out for querying). The street address lives inside
-- record and is only released through the backend's ops view after the
-- homeowner selects a quote (SOW §12).
create table if not exists public.flow_quote_requests (
    id text primary key,
    thread_id text not null,
    homeowner_id uuid references public.homeowners (id) on delete cascade,
    status text not null default 'submitted',
    record jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists flow_quote_requests_status_idx
    on public.flow_quote_requests (status, created_at);

-- Service-role only: enable RLS with no policies and revoke client grants.
alter table public.flow_states enable row level security;
alter table public.flow_journal enable row level security;
alter table public.flow_quote_requests enable row level security;
revoke all on public.flow_states from anon, authenticated;
revoke all on public.flow_journal from anon, authenticated;
revoke all on public.flow_quote_requests from anon, authenticated;

-- §12 retention window: delete journal entries older than the configured
-- window. Invoked by backend/scripts/retention_purge.py.
create or replace function public.purge_flow_journal(older_than_days integer)
returns integer
language plpgsql
security definer
as $$
declare
    removed integer;
begin
    delete from public.flow_journal
    where created_at < now() - make_interval(days => older_than_days);
    get diagnostics removed = row_count;
    return removed;
end;
$$;

-- §12 deletion path (CCPA and comparable regimes): remove every flow-agent
-- record attached to one homeowner.
create or replace function public.delete_flow_data_for_homeowner(target uuid)
returns void
language plpgsql
security definer
as $$
begin
    delete from public.flow_journal where homeowner_id = target;
    delete from public.flow_states where homeowner_id = target;
    delete from public.flow_quote_requests where homeowner_id = target;
end;
$$;

revoke all on function public.purge_flow_journal (integer) from anon, authenticated;
revoke all on function public.delete_flow_data_for_homeowner (uuid) from anon, authenticated;
