# ADR 0003 — Correlation and trace propagation across Service Bus

- **Status:** Accepted
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** `ingestion-func`, `processing-func`

## Context

`docs/agent-fleet.md` §5 already sets the standard: *"One correlation ID per ingested file/batch,
propagated through Service Bus message application properties and the CloudEvents envelope."*
**Neither service complies**, and no distributed trace exists today.

Producer side (`ingestion-func`):
- `correlation_id` is a fresh `uuid4` per invocation (`function_app.py:171,318`); the worker does
  inherit it from the work item (`:430`).
- `traceparent` is **synthesised from random UUIDs**, not from any real span context:
  `trace_id = uuid4().hex; span_id = uuid4().hex[:16]` (`function_app.py:176-178,464-466`).
  It is a well-formed string that corresponds to no actual trace.
- Both are carried **only inside the CloudEvents JSON body**
  (`src/cloudevents_envelope.py:56,58`). The Service Bus message itself sets
  `application_properties = {source_vendor, schema_version}` only, and sets **neither
  `correlation_id` nor `message_id`** (`src/service_bus_emitter.py:145-153`).
- **No OpenTelemetry SDK is installed at all** — `configure_azure_monitor` is absent and the Azure
  Monitor distro is not in `requirements.txt`.
- The internal dispatcher → worker hop carries no `traceparent` in the work item
  (`function_app.py:385-393`), so even that trace is broken.

Consumer side (`processing-func`):
- `correlation_id` is read only from the envelope JSON (`ProcessDatasetFunction.cs:58`), falling back
  to `Guid.NewGuid()`. Service Bus `ApplicationProperties` and `message.CorrelationId` are never read.
- **`traceparent` is never extracted** — no `ActivityContext`, no propagator, anywhere in `src/`.
  `specs/001-dataset-ingestion-pipeline/tasks.md:162` explicitly required `ActivityContext.Parse` at
  trigger entry; it was never implemented.
- `StartActivity("dataset.process")` (`ProcessDatasetCommandHandler.cs:60-63`) is started with **no
  parent context and no `ActivityLink`**, so the producer's trace is discarded. OpenTelemetry is
  otherwise correctly wired (`Program.cs:46-59`, `host.json:3`).

Net effect: the only cross-service join key is a `correlation_id` **tag**, queryable by hand but not a
linked distributed trace.

## Options

**A. W3C `traceparent` in Service Bus `ApplicationProperties` only.**
Broker-native; the Azure SDK and Azure Monitor stitch producer and consumer automatically. Lost if a
message is replayed from a dead-letter dump or rebuilt from storage.

**B. CloudEvents Distributed Tracing extension (in the envelope) only.**
Survives replay and protocol hops because it travels with the payload; but broker-level
auto-instrumentation cannot see it, extraction is manual, and Azure Monitor's built-in Service Bus
correlation stays dark.

**C. Both — application properties as the live transport, envelope copy as the durable record.**
Belt and braces; one extra field to keep consistent.

## Decision

**Option C, with A as the load-bearing half.** `traceparent` travels in Service Bus
`ApplicationProperties` (so the platform correlates automatically) **and** in the CloudEvents envelope
(so a replayed or re-driven message keeps its provenance).

Two identifiers are defined distinctly, and both are required:

| Field | Meaning | Lifetime |
| :--- | :--- | :--- |
| `correlation_id` | business key — one per ingested file/batch | stable across retries and re-drives |
| `traceparent` | W3C trace context for one distributed operation | new on every replay |

Implementation order — each step depends on the one before it:

1. **Wire the Azure Monitor OpenTelemetry distro into `ingestion-func`** (`configure_azure_monitor`).
   This is the real blocker: every later step presupposes a genuine span exists.
2. **Stop synthesising trace IDs** (`function_app.py:176-178,464-466`); derive `traceparent` from the
   current span context.
3. **Set `ServiceBusMessage.correlation_id` and `message_id`** on the producer
   (`src/service_bus_emitter.py:145-153`) — the consumer already does this on its own outbound
   (`ServiceBusEventPublisher.cs:33-39`).
4. **Extract and parent on the consumer**: `ActivityContext.Parse` at `ProcessDatasetFunction.cs`
   entry, passed as parent (or as an `ActivityLink` when processing in batches) to `StartActivity`.
5. **Fix the internal hop**: put `traceparent` in the work-item message
   (`function_app.py:385-393`) so dispatcher → worker is also linked.

## Consequences

**Positive**
- One trace spans OEDI fetch → bronze write → Service Bus → validation → silver write.
- Azure Monitor correlates without bespoke queries, satisfying `docs/agent-fleet.md` §5.
- A dead-lettered message still carries its origin trace, so re-drives stay attributable.

**Negative / required work**
- `ingestion-func` gains an OpenTelemetry dependency and an exporter configuration it does not have
  today — the largest single piece of work in this ADR.
- Two copies of `traceparent` must be written consistently; the envelope copy is authoritative only
  when application properties are absent (i.e. after a replay).

**Forward compatibility (ADR 0006)**
- Quarantined records must carry `correlation_id` **and** `traceparent`, so that a file quarantined as
  `UNMAPPED_SCHEMA` can be traced back to its original ingestion run when it is re-driven after a
  mapping is published.
