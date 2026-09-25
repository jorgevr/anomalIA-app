# ADR 0002 — Event naming and versioning convention

- **Status:** Accepted
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** `ingestion-func`, `processing-func`, root emulator config

## Context

Three different conventions are in use at once, and the consumer enforces none of them.

- `ingestion-func` emits `solar.pvdaq.dataset.available` as a CloudEvent
  (`src/cloudevents_envelope.py:47`), with version information carried in a `schema_version`
  envelope attribute and **no version in the type string**.
- It *also* still emits `raw.pvdaq.generation.v1` (`src/cloudevents_envelope.py:68`,
  `src/record_pipeline.py:37`) — a type explicitly marked **retired** in `topics.md:21`. Its version
  lives in a `.v1` type suffix.
- `processing-func` emits `dataset.bronze.available`
  (`Notifications/DatasetBronzeAvailablePublisher.cs:33`) as **flat JSON, not a CloudEvent**
  (`ServiceBusEventPublisher.cs:33-39` sets `ContentType = "application/json"`), with no version
  anywhere in the type.
- The consumer **never reads or asserts the event `type`** (`ProcessDatasetFunction.cs:36-38`
  deserializes straight into `DatasetAvailableEvent`), so it will process anything that lands on its
  queue, including a type it has never seen.
- `dataset.bronze.available` is named after a storage layer, so renaming the layer (ADR 0001) forces
  an event rename — the event contract is coupled to a storage decision it should not depend on.

## Options

**A. Version in the type string only** (`solar.pvdaq.dataset.available.v1`).
Enables broker-level routing and filtering per version; but every breaking change forks the type and
any subscription filter built on it.

**B. Stable type + CloudEvents `dataschema` URI.**
Standards-compliant and precise; consumers negotiate on the schema URI. But the broker cannot filter
on version, and consumers must read two fields to determine compatibility.

**C. Hybrid — major version in the type string, `dataschema` pinning the exact schema.**
The industry norm: major = breaking = new type = new routing decision; minor = additive = same type.
Slightly more convention to document.

## Decision

**Option C**, with the naming convention:

```
{domain}.{vendor}.{entity}.{action}.v{major}
```

| Event | Producer | Consumer | Replaces |
| :--- | :--- | :--- | :--- |
| `solar.pvdaq.dataset.available.v1` | `ingestion-func` | `processing-func` | `solar.pvdaq.dataset.available` |
| `solar.pvdaq.dataset.validated.v1` | `processing-func` | (downstream) | `dataset.bronze.available` |

Rules:

1. **Major version in the type string.** A breaking change (removing a field, narrowing a type,
   changing a meaning) is a new major and therefore a new type string.
2. **Minor changes are additive only** and keep the type string; the exact schema is identified by the
   CloudEvents `dataschema` attribute, a URI pointing at the contract in the registry (ADR 0004).
3. **Event names never reference a storage layer.** `dataset.validated` describes what happened to the
   data, not where it was put, so ADR 0001's renames cannot propagate into the event contract.
4. **Every message is a CloudEvent**, including `processing-func`'s outbound, with content type
   `application/cloudevents+json`.
5. **The consumer must assert `type`** before processing and dead-letter an unrecognised type.

## Consequences

**Positive**
- Additive changes — such as the `device_id` that ADR 0006 will need — ship as a minor version with no
  consumer change and no new queue.
- A consumer can reject an unexpected event instead of silently misprocessing it.
- Layer renames and event renames are now independent.

**Negative / required work**
- `ingestion-func`: type strings at `src/cloudevents_envelope.py:47,68`; **stop emitting the retired
  `raw.pvdaq.generation.v1`**; update `topics.md`.
- `processing-func`: `DatasetBronzeAvailablePublisher.cs:33`; wrap the outbound message in a real
  CloudEvents envelope (`ServiceBusEventPublisher.cs:29-39`); **add a `type` assertion** at
  `ProcessDatasetFunction.cs:36-38`.
- Queue rename `dataset-bronze-available` → `dataset-validated` in both emulator configs and all
  settings files.
- The envelope's `mapping_version` is currently hardcoded to `"unknown"`
  (`src/cloudevents_envelope.py:53`); it becomes meaningful under ADR 0006 and must be populated.

**Forward compatibility (ADR 0006)**
- Per-device mapping needs device-level identity. Because rule 2 makes additive `data` fields a minor
  version, `device_id` can be added to `solar.pvdaq.dataset.available.v1` later **without** a breaking
  change or a consumer redeploy.
