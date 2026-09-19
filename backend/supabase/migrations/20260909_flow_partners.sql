-- Provider table for the ops lead email (partners, previous quoters,
-- prospects, and businesses found by discovery), previously a JSON file on
-- the host's ephemeral disk that reset to the sample seed on every deploy.
--
-- One row per provider, keyed by the normalised name. ``record`` is the
-- full row exactly as backend/app/flow/partners.py reads and writes it:
-- contact fields, service types, zips, relationship, quotedCount /
-- lastQuotedAt (written by note_quoted when a real quote lands), and
-- ``presences`` -- one entry per platform with nullable rating / review
-- count / follower count, the source, and the retrieval timestamp so any
-- ranking can be audited later (docs/adr/provider-ranking.md).
--
-- Service-role only, like the other flow tables: RLS on, no policies.
create table if not exists public.flow_partners (
    key text primary key,
    name text not null,
    relationship text not null default 'prospect',
    record jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists flow_partners_relationship_idx
    on public.flow_partners (relationship);

alter table public.flow_partners enable row level security;
revoke all on public.flow_partners from anon, authenticated;
