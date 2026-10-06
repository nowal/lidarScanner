# Quote submission media

Quote submission now captures the single published HomeIndex in the durable quote
record. Its immutable context ZIP, selected room keys/names, frame IDs, model
object paths and revision remain fixed when the homeowner scans again. Whole-home
requests use the home model. Selected-room requests use the matching area models;
ambiguous room names produce an explicit missing-media message instead of guessing.
Legacy requests without a snapshot retain their saved download link; they are not
silently attached to a later scan.

The operations worker reads JPEGs from the saved context ZIP, rotates them using
camera gravity, and saves full (up to 768px) and thumbnail (up to 360px) copies in
the same private `metashape-exports/<owner>/<home>/` folder. Existing scan deletion
removes those files. Up to six representative images appear in the HTML email,
with readable area captions. The browser gallery includes up to four views per
area. Missing images do not prevent delivery. The street address remains withheld.

The 30-day media capability is separate from quote-entry authorization. It is
bound to one request and snapshot, permits read-only media access, and never
contains a service credential. Each asset request checks the capability and home
existence and redirects to a private Storage URL lasting at most five minutes.
A resend renews access for the same snapshot and invalidates the previous media
capability. Storage buckets and policies are unchanged; no migration is needed.

## Desktop conversion

USDZ is retained byte-for-byte for download. The desktop viewer uses a self-hosted,
pinned `@google/model-viewer` 4.1.0 and a GLB derivative. USDZ is not the viewer's
rendering format. OpenUSD 25.11 resolves the actual binary USD and nested USDZ
references; the converter carries triangle geometry, world transforms, stage
units/up-axis, normals, indexed UVs, material bindings, diffuse/emissive textures,
opacity and metallic/roughness factors into GLB. Non-unit source normals are normalized without changing their direction.
Embedded PNG/JPEG bytes are copied
without recompression. UV V coordinates are translated to glTF's convention.
The original is never modified. Unsupported shading/geometry or unresolved/external
references fail the derivative rather than quietly dropping parts of the model.

Conversion runs one at a time in a subprocess (20-minute timeout; Linux 3 GiB address
space and 1,100-second CPU limits). Immutable source hash + converter version keys
the cache. A durable ready receipt follows the completed resumable Storage upload.
Repeated requests reuse it. A restart-interrupted preparation can be requeued on
viewer access; a failed conversion cools down for an hour. The page has preparing,
loading, failure and original-download states. No model attachment or conversion
runs on the quote submission response path.

## Delivery configuration

`LIDARAI_OPS_EMAIL` retains the primary operations address. Set
`LIDARAI_OPS_QUOTE_CC=noah@takeshapehome.com` for submission copies. The setting is
only applied to quote-request lead messages, including explicit resends; decision
emails and reply confirmations keep their existing destinations. Resend and SMTP
receive separate validated recipient lists with duplicate addresses removed.
`LIDARAI_OPS_REPLY_TO` and `LIDARAI_OPS_REPLY_SENDERS` retain their existing meanings;
a CC recipient does not gain email-command privileges.

The rendered submission payload is durably saved before transport, so retrying
uses the same content, recipients and Resend idempotency key. Duplicate queued
work checks the delivery stamp. An explicit authenticated resend gets a new
delivery ID. Resend retains idempotency keys for 24 hours; SMTP cannot provide an
atomic provider/database delivery transaction. The usual rare ambiguous-send
window after an extended database outage still requires operator reconciliation.

## Validation

Run `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests` from the repo.
`test_quote_media.py` covers scope/revision, camera orientation, missing photos,
escaping, private access/expiry, recipient handling, unchanged sender trust,
retry payloads, duplicate delivery and conversion recovery/failure.
`test_usdz_to_glb.py` builds three-level nested packages with known geometry,
transforms, scale, UVs, emissive material and texture bytes, and checks exact
conversion results and external/missing-reference rejection.

Production verification must use one clearly labeled email to Noah only. Never
bulk resend historical requests or send development previews to providers.
