-- Anonymous Supabase Auth users own a normal homeowners row. Keep its ID
-- when an email identity is linked, so storage paths and flow FKs stay valid.
-- PostgreSQL's existing UNIQUE constraint still protects non-null emails.
alter table public.homeowners alter column email drop not null;

comment on column public.homeowners.email is
  'Nullable for a guest Auth user; populated after email confirmation. Ownership is auth_user_id, never email.';
