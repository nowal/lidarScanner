# Home Guide dev console

A single static page for walking through every ingested home and room and
chatting with the agent as the homeowner would, with the flow state
(step, slots, scope, gates, active room, quote request) shown next to each
reply. It uses only the public API the app and operations use, so it needs
no changes to the backend to run and never touches delivery code.

```
.venv/Scripts/python scripts/dev_console/serve.py --port 8020
```

Open http://127.0.0.1:8020/, enter the backend base URL (default
`http://127.0.0.1:8010`) and the ops token, and connect.

Two pages:

- **`index.html`** — the walkthrough across every ingested scan, with the
  flow state decoded beside each reply (the working console).
- **`phone.html?home=<homeId>`** — the presentation view: an iPhone frame
  showing the chat as the homeowner sees it (the app's own colours, the
  quote-request card with its Confirm, the returned-quote card with
  Choose). The home's **floor plan is pinned at the top of the chat**,
  drawn from the room footprints in the index; tap a room to move the
  conversation there, switch floors with the tabs, collapse it with the
  chevron (a chip strip takes its place). Beside the phone: a home picker
  for every ingested scan, the rooms, the flow machine, the gates, demo
  controls (scan processing toggle, submit, operations entering two
  quotes, "any news?") and, once submitted, what operations receives.
  Defaults to `quintin-house`; `base` and `ops` query parameters override
  the backend URL and ops token (otherwise the values saved by
  `index.html` are used, then `e2e-ops-token`).

**One-click start:** `start_demo.cmd` in this folder launches the local
backend (`run_local_backend.py`, port 8010, mail captured to the outbox,
Supabase homes available) and the pages (port 8020) in their own windows
and opens the phone view. A desktop shortcut can point at
`http://127.0.0.1:8020/phone.html?home=quintin-house` once those are up.

- **Scans received** lists every home from `GET /api/v1/ops/homes` (local
  and durable) with a preview when `thumbs/<homeId>.jpg` exists (copy each
  export's `thumbnail.jpg` there; the folder is git-ignored), plus, when
  TakeShape's `captured-room.json` test fixture is checked out alongside
  the repo, a single-room capture that exercises the step-5/6 path without
  a home index.
- **This home** shows areas, floors, square footage, photos, stored models
  and named rooms, then each room with its fixtures, floor and whether a
  baked model is stored; clicking a room drafts "Let's talk about the …".
- **Chips** under the transcript are a ready-made walkthrough: a name,
  a room, the three scope statements, the "scan the rest now?" push, zip,
  the project, and the address turn.
- **Conversation**: opening turn, free chat, the processing state and
  `localModelReady` flag the app would send, `scanMode`, then quote
  submission, the ops view, a quote upload and the results turn. Switching
  spaces starts a new thread; sending a different `homeId` on the same
  thread is also supported by the backend (a fresh subject).
- **Flow state** decodes the `flow` object from the last reply.

The backend must allow the console's origin (`LIDARAI_CORS_ORIGINS`,
default `*`). The generated `fixtures/` directory is ignored by git.
