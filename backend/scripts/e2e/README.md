# Live end-to-end drivers

Each script drives a RUNNING backend over HTTP the way the app and
operations do, with a live model, and prints a PASS/FAIL checklist. They
complement `scripts/acceptance_rehearsal.py` (in-process, the SOW §4
rehearsal) by exercising the startup-owned email worker, restarts, scope
intent, and real whole-home exports. Results land in `smoke/`.

Environment (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `E2E_BASE` | `http://127.0.0.1:8010` | the backend under test |
| `E2E_OPS_TOKEN` | `e2e-ops-token` | its `LIDARAI_OPS_TOKEN` |
| `E2E_STORAGE_DIR` | `backend_storage` | its `LIDARAI_STORAGE_DIR`, to read the captured outbox email and journal |

Run the backend with every mail transport blank so the lead email is
captured in `{storage}/ops_outbox/` rather than sent.

```
python scripts/e2e/e2e_ops_email.py 1          # first lead, upload, results; then restart + wipe storage and
python scripts/e2e/e2e_ops_email.py 2          # second lead: past quoters survive the restart
python scripts/e2e/e2e_scope.py single_room|selected_rooms|whole_home [processor_job|device_bake]
python scripts/e2e/e2e_home.py <homeId> [<otherHomeId>]   # a real ingested export, plus a mid-thread home switch
```

The scope and ops-email drivers use the app's single-room test fixture
(`takeshape-mobile/LidarAITests/captured-room.json`, checked out beside
this repo). A run costs a few cents of model calls.
