# End to end across five sample scans, 2026-09-18

SOW §4 makes this an acceptance condition: "the flow runs end to end on 5
sample scans without critical failures". This is that run. A critical
failure is defined in the same clause as "a defect that prevents the flow
from completing"; none of the runs below hit one.

Transcripts: `e2e_five_scans_2026-09-18/`.

## How it was run

A real `uvicorn` process driven over HTTP the way the app and operations
do, with the live model (Anthropic claude-sonnet-5, effort low) and the
development Supabase project (flow state, journal, quote requests, provider
table, home indexes, room models). Driver: `scripts/e2e/e2e_home.py <homeId>
<otherHomeId>`, which walks one ingested export end to end and then switches
home mid-thread.

Every mail transport was blanked, so each lead email is composed by the
startup-owned worker and captured in `{storage}/ops_outbox/` instead of
being sent. Nothing left the machine. `LIDARAI_AUTH_TOKEN` was left unset
because the driver sends no service token; the health endpoint reports
`degraded` for that reason alone, which is expected for this harness and
not a finding.

Homeowner data is synthetic throughout: "Dana", zip 37203, "118 Maple
Street". The homes are real consented exports.

## Results

| # | Home | Rooms | Checks | Failed |
|---|---|---|---|---|
| 1 | `quintin-house` | 6 | 21 | 1 — see A |
| 2 | `noah-house` | 19 | 22 | 1 — see B |
| 3 | `5b579d84…` | 8 | 22 | 0 |
| 4 | `e38e05ff…` | 8 | 21 | 0 |
| 5 | `d3a2c134…` | 4 | 22 | 0 |
| | | | **106 passed** | **2** |

Each run covers: the ops home listing, room naming and confidence, a
grounded opening that names a real room, an unresolved room the scan could
not identify, the homeowner naming that space (and the rename persisting),
scope capture in real room names, the extension-prompt gate, the quote
request, the lead package with room-level measurements and a model link,
the composed email with the entry link and no address, and a mid-thread
switch to a second home.

## A — `quintin-house`: "email composed" timed out, but the email is correct

Not a defect. The driver waits 90 seconds for the outbox file. On the first
lead of a cold process the provider research has nothing cached, so it took
**195 seconds**: request created 11:12:25, email written 11:15:40. The four
later runs reused the cache and passed comfortably.

The email itself is right — subject `New quote request: Painting — kitchen +
bathroom in 37203 (Dana) — qr_8bd137df88fe`, entry link present, street
address absent.

Worth knowing operationally: the first lead after a restart can take a few
minutes to arrive. That is by design — research is deliberately kept off the
homeowner's submit — but it is longer than people expect. Two options if it
matters: raise the driver's wait, or warm the research cache at startup.

## B — `noah-house`: no paintable wall area on the kitchen

A real gap, though not a critical failure. The check requires
`paintableWallSquareFeet` in the lead package. For this room the package
carried floor area, fixtures, and door and window counts, but no wall area:

    {"room": "kitchen", "storey": 3, "floorAreaSquareFeet": 451.1,
     "doorCount": 2, "windowCount": 1, "photoCount": 56,
     "fixtures": ["5x storage", "2x sofa", "2x table", "2x chair", "1x sink"]}

Compare a room that does produce it, which also carries `perimeterFeet` and
`wallCount`. So the wall surfaces are missing from this room in the index,
and the derived paintable area goes with them.

It matters because painting is priced off wall area. A painter receiving
this lead gets floor area and has to ask. Worth an issue: either derive
perimeter from the room polygon when wall surfaces are absent, or say
plainly in the package that wall area could not be measured, rather than
omitting the field.

## What this does not cover

- TestFlight. Every run here drives the backend over HTTP; none of it ran on
  a physical device. That remains the last unverified link.
- The five scans are four of Quintin's walks plus Noah's house. If TakeShape
  wants a different five for acceptance, rerun with those ids — the driver
  takes them as arguments.
