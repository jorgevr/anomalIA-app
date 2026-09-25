# ADR 0006 — Per-device schema mapping by structure fingerprint (deferred)

- **Status:** Deferred — design only, not scheduled. Recorded so ADRs 0001–0005 do not preclude it.
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** `processing-func` (primarily), event contract (additively)

## Context

PVDAQ source structure varies per site and per device: two files from the same vendor, and even from
the same site, may present different column sets. Today `processing-func` resolves exactly one mapping
per vendor and version, keyed `{vendorId}-{schemaVersion}.json`
(`BlobSchemaRegistry.cs:46-54`), with **no fallback** — a miss raises `UnknownSchemaException` and
dead-letters the message (`BlobSchemaRegistry.cs:59-63`, `ProcessDatasetFunction.cs:157`). The mapping
is cached in-process and never invalidated (`BlobSchemaRegistry.cs:20,39-42`).

That model cannot express "this device reports a different column set from the rest of its site".

## Intended design (not yet decided in detail)

1. Mappings are **versioned artifacts** in the schema registry.
2. A mapping is resolved by **fingerprinting the response structure** of the source file (e.g. a
   canonical hash of the header/column set), not by trusting a declared version.
3. Resolution follows a **precedence chain: device → site → vendor default.** The most specific
   published mapping wins.
4. Mapping is applied in `processing-func`, during the **source copy → validated** transition
   (bronze → silver, ADR 0001).
5. A structure with no mapping at any level is **not dead-lettered** — it is written to `quarantine`
   with reason code `UNMAPPED_SCHEMA`, and can be re-driven once a mapping is published.

## Compatibility check against ADRs 0001–0005

This is the purpose of the record: confirming nothing already decided blocks the above.

| ADR | Effect on this design | Verdict |
| :--- | :--- | :--- |
| **0001** layer naming | `quarantine` with reason codes is exactly the required landing zone, and "applied in processing (source copy → validated)" is precisely the bronze → silver transition. | **Enables** |
| **0002** event versioning | Device-level identity (`device_id`) is an *additive* `data` field, which rule 2 makes a minor version — no new type, no consumer redeploy. `mapping_version` already exists in the envelope. | **Enables** |
| **0003** tracing | `correlation_id` + `traceparent` on quarantined records make a re-drive traceable to its original ingestion run. | **Enables** |
| **0004** contract location | Vendor mappings are explicitly *not* message contracts, so they version on their own cadence with no cross-service coordination. | **Enables** |
| **0005** Blob everywhere | Fingerprinting and quarantine writes are plain object puts — no rename, no directory semantics, no ACLs. | **Neutral** |

**Conclusion: no decision in ADRs 0001–0005 prevents this design.** Five obligations are carried by
those ADRs so that it stays possible:

- **ADR 0001** — the quarantine reason-code vocabulary is open and extensible (`UNMAPPED_SCHEMA` can be
  added without a contract break), and quarantine records retain full source identity (site, device,
  category, source URI, version) for re-drive.
- **ADR 0002** — `data` is additively extensible, so `device_id` can be added later as a minor version;
  `mapping_version` must stop being hardcoded to `"unknown"` (`src/cloudevents_envelope.py:53`).
- **ADR 0003** — quarantined records carry `correlation_id` and `traceparent`.
- **ADR 0004** — the registry lookup key must generalise from flat `{vendorId}-{schemaVersion}` to a
  precedence chain; this is `processing-func`-internal with no message-contract impact.
- **ADR 0005** — no storage primitive beyond object put/get is introduced.

## Open questions (to resolve when this is scheduled)

- What exactly is fingerprinted — ordered column names, the set, types, or a sample of values?
- Is the fingerprint computed in `ingestion-func` (and carried on the event) or in `processing-func`
  (from the bronze copy)? Computing it downstream keeps the producer ignorant of mapping concerns and
  is the default assumption above.
- How is the in-process registry cache invalidated when a new mapping is published, given it is
  currently never invalidated?
- Does re-driving quarantined files happen automatically on mapping publication, or by an operator
  action?
