# ADR 0001 — Storage layer naming: bronze / silver / quarantine

- **Status:** Accepted
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** `ingestion-func`, `processing-func`, root config

## Context

The two services currently write to **the same container with two different meanings**.

- `ingestion-func` lands a byte-for-byte source copy of each PVDAQ CSV in the container named by
  `ADLS_CONTAINER_NAME`, whose configured value is **`bronze`**
  (`services/ingestion-func/local.settings.json:31`), under
  `source=pvdaq/dataset={site_id}_{category}/ingestion_date={YYYY-MM-DD}/{dataset}_v{n}.csv`
  (`function_app.py:94-100`). The bytes are streamed and hashed but never parsed or validated
  (`src/adls_store.py:147-158`).
- `processing-func` writes **validated Parquet** to the container named by `BRONZE_FILESYSTEM`,
  default **`bronze`** (`Program.cs:152`), under `{datasetId}/{date}/data.parquet`
  (`OneLakeBronzeWriter.cs:42`).
- The local seed script puts source CSVs into `bronze` as well (`scripts/seed-azurite.js:13,69`).

So `bronze` simultaneously means "raw landing zone" and "curated output", with two incompatible path
layouts in one container. The name `raw` is configured **nowhere** — it survives only in docstrings
(`src/adls_store.py:44`, `services/ingestion-func/CLAUDE.md:10`) and in unit tests that read from
`abfss://raw@store.dfs.core.windows.net/...` (`ProcessDatasetCommandHandlerTests.cs:32`), revealing
that the original design intended `raw` = source and `bronze` = validated.

Rejected data has nowhere to go: failures dead-letter to Service Bus
(`ProcessDatasetFunction.cs:53,76,104,112,121,135,157,172`) and the rejected rows themselves are lost.

## Options

**A. `bronze` = exact source copy, `silver` = validated/canonical, `quarantine` = rejects.**
Matches the stated principle. Ingestion's container is *already* `bronze`, so every `storage_path`
the producer emits stays valid and the inbound event contract is untouched; the change is confined
to the consumer and to config.

**B. `raw` = source, `bronze` = validated.**
Matches the original design and the Fabric Lakehouse convention, but contradicts the stated principle
*and* changes every emitted `storage_path`, breaking payload values, seed scripts and all fixtures.

**C. One container, prefix-separated (`bronze/`, `silver/`).**
Cheapest config change, but forfeits container-level RBAC, lifecycle policy, retention and access
tiering — the operational reasons to split layers — and keeps the ambiguity alive inside path strings.

## Decision

**Option A.** Three containers with one meaning each:

| Container | Contents | Format | Validated | Written by |
| :--- | :--- | :--- | :--- | :--- |
| `bronze` | exact source copy, immutable, append-only versions | CSV as fetched + `metadata.json` | no | `ingestion-func` |
| `silver` | validated, canonical, typed | Parquet | yes | `processing-func` |
| `quarantine` | rejected rows (normally) or whole unparseable files, each with a reason code | rejected rows + `reason.json` | n/a | `processing-func`, `ingestion-func` |

Layer publication is signalled by **an event**, never by a directory rename — see ADR 0005, which
keeps atomic rename out of the primitives the code uses.

`silver` is partitioned by **data date, derived from the records themselves**, not by processing date.
The current implementation partitions by processing-time UTC (`OneLakeBronzeWriter.cs:42`,
`ProcessDatasetCommandHandler.cs:120`), which means a re-run or a late backfill scatters one day's
readings across several partitions and makes a date-ranged read both wrong and non-deterministic.

## Failure routing

The layer split is only meaningful if rejects have a defined destination. The rule is **the broker DLQ
is for messages, quarantine is for data** — and the two must not be conflated.

**Broker DLQ — unprocessable *messages* only.** A message goes to the dead-letter sub-queue of the queue
that delivered it when the service cannot act on the message at all:

- the envelope is invalid or fails contract validation (ADR 0004)
- the event `type` is unrecognised (ADR 0002)
- the referenced source object cannot be read (missing, unauthorised, unreachable)

These are integration faults. They are operator-actionable, carry no rows, and must be retried or fixed
upstream — never silently absorbed.

**Quarantine — rejected *data*.** When the message is valid and the source is readable, the data is the
problem, and the message is **completed, not dead-lettered**. Dead-lettering readable-but-invalid data
is the defect this rule removes: it hides a data-quality signal inside an infrastructure queue, where a
redelivery will fail identically forever.

- **Row-level (the normal case).** Invalid rows are written to `quarantine` with a reason code; valid
  rows proceed to `silver`. A partly bad file is therefore *partly* ingested, and
  `silver rows + quarantine rows = bronze source rows` always holds. No silent drops
  (`docs/agent-fleet.md` §5).
- **File-level (the exception).** Only when the file cannot be parsed at all — unreadable encoding,
  structurally broken CSV, no recoverable rows — is the whole file quarantined with a file-level reason
  code.

**`pvdaq-dead-letter` is retired.** `ingestion-func` currently routes record-level validation rejects to
a bespoke application queue (`src/service_bus_emitter.py:38-47`, `function_app.py:445,621`). That is the
same category error: rejected records are data, not undeliverable messages. **Decision:** the
`pvdaq-dead-letter` queue is removed; record-level rejects from ingestion are written to `quarantine`
under the same reason-code vocabulary, and genuinely unprocessable messages use the **broker DLQ of
their own queue** (`pvdaq-historical-work`, `raw-energy-events`). This leaves exactly one reject
destination per category across both services, rather than three mechanisms doing overlapping jobs.

## Consequences

**Positive**
- Ingestion's write path is unchanged; no emitted `storage_path` value moves, so the inbound event
  contract (ADR 0002) is unaffected by this decision.
- Each layer gets its own RBAC scope, lifecycle policy and retention — a source copy and a derived
  artifact have genuinely different retention needs.
- Rejected rows become durable and inspectable instead of vanishing into a dead-letter queue.

**Negative / required work**
- `processing-func` renames its output target and writer: `Program.cs:152`,
  `OneLakeBronzeWriter.cs` → `SilverWriter` (+ unit tests), `ProcessDatasetCommandHandler.cs:117-120`,
  `local.settings.json:15`, `local.settings.json.template:13`, `scripts/seed-azurite.js:13`,
  root `.env.example:43`.
- The outbound event `dataset.bronze.available` is named after a layer and must be renamed; ADR 0002
  decouples event names from layer names so this cannot recur.
- Stale documentation must be corrected: `src/adls_store.py:44`,
  `services/ingestion-func/CLAUDE.md:10`, and the obsolete layout in
  `specs/002-pvdaq-historical-ingestion/contracts/work-item-message.json:42`.
- **Row-level quarantine is new behaviour, not a rename.** `processing-func` currently fails a whole
  file on any validation failure (`ProcessDatasetCommandHandler.cs:94-100`); it must instead partition
  the record set into accepted and rejected, and write both.
- **Silver repartitioning changes existing paths.** Switching from processing date to data date means
  the partition key must come from the mapped timestamp field, and any already-written silver data is
  laid out under the old scheme.
- **Retiring `pvdaq-dead-letter` removes a queue other things may watch.** It must be removed from the
  emulator config, IaC and both services' settings, not merely left unused.

**Positive (failure routing)**
- One reject destination per category across both services, instead of three overlapping mechanisms.
- A data-quality problem no longer parks itself in an infrastructure queue where redelivery is futile.
- Row-level handling means one bad row no longer costs an entire file's worth of good readings.

**Forward compatibility (ADR 0006)**
- The quarantine **reason-code vocabulary must be open and extensible**, so `UNMAPPED_SCHEMA` can be
  added without a contract break.
- Quarantine records **must retain full source identity** (site, device, category, source URI, version)
  and the `correlation_id`, so a file can be re-driven once a mapping is published.
