# Supabase Pro migration preparation — 2026-09-22

Schema/data migration and pre-cutover checks completed on September 22, 2026. Render deployment and its final live check are tracked in the private deployment report.

## Confirmed scope and completed work

Destination: **TakeShapeApp** (`pxnbfdsyuzweevkvremi`) in the TakeShape Pro organization. Nathan's existing history and stored files were included. TakeShapeDB was not changed.

- Pulled Vlad's main commit `9a85080`; its scan tests passed with the upload changes.
- Backed up source/destination public data, Auth data, Storage metadata, and original configurations privately, outside Git.
- Applied the four backend SQL files below as one hosted migration, `20260922154321_import_flow_schema_and_guest_ownership`.
- Copied 410 states, 2,560 journal entries, 55 quote requests, six partners, and the guest homeowner, preserving their IDs.
- Copied all 27 source Storage objects (529,915,694 bytes), verifying each SHA-256. Existing destination objects were not replaced.
- Regenerated eight saved model links and one quote-document link with target-project signatures. Historical source-project links already distributed remain dependent on the old project until replaced or expired.
- Preserved all existing destination public records. Nathan's 18 sample providers exactly matched existing destination records except IDs/timestamps, so they were mapped instead of duplicated. The permanent source account matched an existing destination email; its existing destination credentials were retained. It owned no source homeowner/flow data.
- Imported the guest Auth user/session/refresh token without consuming the real device's refresh token. A separate temporary guest demonstrated that the copied token obtains a target JWT with the same user ID; both test accounts were removed.
- Added an iOS Keychain handoff before upload/chat guest creation, plus immediate guest signup completion when the project auto-confirms email.
- Passed 14 auth/upload tests after these changes, plus a Release simulator build. The prior scan/upload/auth run passed 33 tests.
- A local backend configured with the destination passed a real 53 MiB guest ZIP upload, ingestion, durable 52 MiB model upload, real Anthropic chat, and owned state/journal persistence. Temporary fixtures were removed.
- Verified that a second guest cannot read another guest's Storage object.

## Verified inventory

| Item | Nathan: takeshape-agent-dev | Noah: TakeShapeApp |
| --- | --- | --- |
| Project ref | xflkhtsmjyifzvumvcul | pxnbfdsyuzweevkvremi |
| Auth users | 2: one guest, one permanent | 5 permanent |
| Homeowners | 1 guest profile | 3 profiles |
| Shared app columns | Compatible | Only email nullability differs |
| AI flow tables | 4 tables | Absent |
| AI state / journal / quote / partner rows | 410 / 2,560 / 55 / 6 | None yet |
| Storage | 27 objects, 529,915,694 bytes | 99 objects, 3,122,023,708 bytes |
| Guest sign-ins | Enabled | Disabled |
| Edge Functions | None | Not needed for Nathan's flow |

The source has two owned flow states and two owned journal rows; the other flow records have nullable ownership. Storage is separate from SQL data and must be copied through Storage APIs. The source's metashape-exports bucket currently contains no completed uploads.

Existing destination homeowners, providers, provider imports/leads, quotes, and Storage objects must be preserved. A full database restore over this populated project is inappropriate.

## Schema work

The source's shared-table columns and constraints match TakeShapeApp except that guest profiles require nullable `homeowners.email`. Required backend migrations, in dependency order:

1. `20260826_flow_agent_persistence.sql`
2. `20260909_flow_partners.sql`
3. `20260921122044_guest_homeowners.sql`
4. `20260922151925_flow_service_role_access.sql`

For this populated destination, apply the reviewed SQL together in one transaction through the hosted migration API. Applying the access restrictions in the same transaction avoids briefly exposing maintenance RPCs through their default PUBLIC execute grant. The last migration makes these RPCs SECURITY INVOKER, pins search_path, revokes client/PUBLIC access, and explicitly grants backend service-role access.

Keep RLS enabled on all four flow tables with no client policies. Clients reach them through the backend. Existing homeowner Storage policies already match Nathan's authenticated ownership paths and work for Supabase anonymous users, which carry the authenticated database role.

Do not blindly run `scripts/apply_supabase_migrations.py` against the populated destination: it can include sibling mobile migrations and seed data; even its current --dry-run creates/modifies its ledger. Review and apply only the required migrations.

Nathan's legacy ai_messages/ai_events policies differ, but the active flow uses server-owned flow_* tables. Keep destination provider-specific behavior intact. Review unrelated policy differences separately rather than replacing every policy wholesale.

## Dashboard and server credentials

The connected tools can inspect and migrate SQL but do not provide the destination service-role secret or update Auth/Storage settings.

For the confirmed destination:

- Authentication > Sign In / Providers: enable Anonymous Sign-Ins and Allow manual linking.
- Authentication > URL Configuration: allow `takeshape://auth-callback`.
- Storage > Settings: raise Global file size limit above the largest expected project ZIP. Check bucket limits too. Pro permits a larger setting but the setting still needs to be raised.
- Project Settings > API Keys > Legacy API Keys: obtain the destination `service_role` key for the existing backend integration. It belongs only in private deployment configuration/Render, never the app or Git.
- Leave existing Auth settings intact unless changing them is necessary. Email signups are enabled and email auto-confirmation is currently enabled on TakeShapeApp.

The destination public and server keys were verified. The private deployment directory contains the staged configuration and original values for rollback; no server secrets are committed. Anonymous sign-ins, manual linking, and the callback allowlist all passed live checks. Storage accepts 1,048,576,000 bytes (1,000 MiB) and rejects one byte more. The backend model cap is 512 MiB.

Official Storage limits: https://supabase.com/docs/guides/storage/uploads/file-limits
Official migration guide: https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore

## Existing accounts and conversations

The guest's original Auth ID, homeowner ID, scan IDs, thread IDs, session, refresh token, and AMR claim were preserved. Supabase Swift stores sessions by project, so the new app explicitly exchanges the previous project's stored guest refresh token against TakeShapeApp before upload/chat can create a new identity. The old access JWT is never sent to the backend as target authentication. Concurrent callers share a task; failed migration blocks new guest creation and can be retried. A Keychain completion marker prevents a later sign-out from resurrecting the old guest.

This is a one-time migration, not ongoing synchronization. Stop using source builds after cutover. Install the new app over the existing installation to retain local scans and threads. Permanent users sign into their existing destination accounts. Existing-account login still does not automatically merge another guest's history.

## App and Render cutover

Only after schema, data scope, credentials, and dashboard settings are verified:

- Save current private configuration and a deployment rollback snapshot.
- Change mobile `Config/Secrets.xcconfig`: SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY.
- Change Render: LIDARAI_SUPABASE_URL and LIDARAI_SUPABASE_SERVICE_ROLE_KEY.
- Keep LIDARAI_FLOW_TOKEN_SECRET, LIDARAI_AUTH_TOKEN, LIDARAI_OPS_TOKEN, and the Anthropic key stable.
- Keep the legacy LIDARAI_SUPABASE_JWT_SECRET blank: current backend identity verification uses Supabase Auth.
- Raise LIDARAI_MODEL_UPLOAD_MAX_MB from its current 50 MB default to a tested value compatible with the destination's limits and server memory. This is separate from the full ZIP upload limit.
- Coordinate the backend change with installing the new app build. Old app builds still use Nathan's URL and tokens and will not work against a backend switched to Noah's project.
- Rebuild and install the app; changing a secrets file does not update an already-installed app.

Current Render already has a working Anthropic key. Resend, ops recipient, and sender settings are absent. Chat/scan ingestion can work without those, but automatic lead emails require Nathan's Resend handoff (or a TakeShape Resend account) plus recipient/sender configuration.

## Verification for future repeats

1. Inspect the target table definitions, grants, RLS, and maintenance RPC access; run security/performance advisors.
2. For a data migration, compare source/target IDs, counts, links, object sizes/checksums, and verify existing target records are unchanged.
3. Create a temporary guest, save its homeowner row, and upload a ZIP larger than 50 MiB through TUS. Confirm another guest cannot read it. Remove test fixtures afterwards.
4. Ingest the same stable scan ID, make real AI turns, and verify persisted flow state/journal ownership in the destination.
5. Verify model files larger than 50 MB are stored instead of skipped and model links resolve to the new project.
6. Verify the app's auth callback and guest conversion without sending unsolicited test emails.
7. Verify the chosen existing-conversation transition on a previously used device.
8. Keep the source available for rollback; account for any new destination writes before rolling back.

## Pre-existing destination access issue

Five provider/import tables currently have RLS disabled and anon SELECT grants, including `provider_sourced_homeowner_leads` (7,388 rows). Four also grant anon INSERT. The older ai_conversations update policy has no ownership condition, and legacy raw-anon Storage policies remain under anonymous/ paths.

These were discovered during the comparison, not introduced by this migration. They need a separate review before broader use; avoid breaking the existing provider integration by changing its permissions without understanding its access paths. The new flow tables and maintenance RPCs will be restricted independently.

Supabase RLS remediation: https://supabase.com/docs/guides/database/database-linter?lint=0013_rls_disabled_in_public


## Existing model limitations

The copied indexes contain 13 model entries that the old deployment had already skipped. Those model files were never stored and are not recoverable from the Supabase copy. Re-ingest the original scan ZIPs to populate them under the increased limits. All files that actually existed in source Storage were copied and verified.
