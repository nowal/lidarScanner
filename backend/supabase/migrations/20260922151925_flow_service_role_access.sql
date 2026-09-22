-- Keep flow persistence and maintenance RPCs exclusive to the backend.
-- Apply after the flow persistence / partner migrations. PUBLIC grants are
-- inherited by both client roles, so revoking only anon/authenticated is
-- insufficient. The service role already bypasses RLS; these functions do
-- not need to run with their creator's privileges.

revoke all on table public.flow_states, public.flow_journal,
    public.flow_quote_requests, public.flow_partners
    from public, anon, authenticated;
grant select, insert, update, delete on table public.flow_states,
    public.flow_journal, public.flow_quote_requests, public.flow_partners
    to service_role;

alter function public.purge_flow_journal(integer) security invoker;
alter function public.purge_flow_journal(integer) set search_path = '';
alter function public.delete_flow_data_for_homeowner(uuid) security invoker;
alter function public.delete_flow_data_for_homeowner(uuid) set search_path = '';

revoke all on function public.purge_flow_journal(integer),
    public.delete_flow_data_for_homeowner(uuid)
    from public, anon, authenticated;
grant execute on function public.purge_flow_journal(integer),
    public.delete_flow_data_for_homeowner(uuid)
    to service_role;
