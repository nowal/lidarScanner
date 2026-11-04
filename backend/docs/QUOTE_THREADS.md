# Saved quote threads

Quote conversations are independent saved threads. A homeowner may start another thread before any earlier quote is accepted. Threads retain their scan association, transcript, pending retries, request receipt and approvals; selecting a thread never resubmits it.

`GET /ai/quote-intake/threads` lists only the verified homeowner’s threads, newest activity first, with `nextOffset` pagination. Titles use the AI-extracted service and target scope; dates and status are separate fields. `POST /threads/new` and `POST /threads/select` take `conversationId` and `homeId`. Creation is idempotent on the client-generated UUID. Selection is atomic and validates owner/home. Persisted threads can finish their own turn or submission while another thread is selected on a second device.

The older `/active` and acceptance-gated `/start-next` contracts remain for deployed clients. A new thread cannot inherit a legacy direct request from an old phone cache. An uncertain legacy dispatch attaches its eventual receipt only to its original thread. Receipt immutability and acceptance notification idempotency remain enforced.

Browsing is not an explicit pause. A subsequent project request proceeds normally. Explicit stops still block submission; a paused follow-up cannot claim readiness. The app’s explicit Resume request action restores even an intake paused by an older release, without relying on the model to classify that command. Reading or migrating history never dispatches a request.

Apply `20261102171138_quote_threads.sql` before deploying this backend or mobile build 19. The migration adds dates/indexing and the private `select_quote_thread` function; existing request and approval records are retained. It was verified with local PostgreSQL tests and production rollback-only transactions. Production retained 14 intakes, 9 receipts and 4 acceptances at migration time.

Validation: full backend suite passed 1,470 tests (2 skipped); 8 focused thread/recovery tests passed after adding the legacy-dispatch regression. Local PostgreSQL tests cover create retries, history restoration, dates, immutable receipts, owner/home checks and grants. Thread tables/functions are backend-service-only with RLS enabled. Existing unrelated database advisories are outside this change.
