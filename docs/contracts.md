# Contract registry — anomalia-platform

Authoritative registry of Service Bus entities, events, storage containers and schemas.
Owned by the **Contract Owner** role (`docs/agent-fleet.md` §3.4); changes to anything listed here go
through that role.

> **Two states are recorded.** *Current* is what the code does **today**. *Target* is what ADRs
> 0001–0005 decide. They differ — the target is not implemented. Do not read the target column as a
> description of the running system. Migration tasks: `docs/contract-migration.md`.

Citations are `file:line` into the service repos as of 2026-09-24.

---

## 1. Service Bus entities

Namespace tier is **Basic — queues only, no topics or subscriptions** (`ingestion-func/topics.md:6`;
`src/service_bus_emitter.py:139-140`).

| Entity | Kind | Producer | Consumer | Current name | Target name | Config key |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| dataset available | queue | `ingestion-func` (`function_app.py:606-609`) | `processing-func` (`ProcessDatasetFunction.cs:26`) | `raw-energy-events` | `raw-energy-events` | `SERVICE_BUS_QUEUE_NAME` / `SERVICEBUS_QUEUE_NAME` |
| dataset validated | queue | `processing-func` (`Program.cs:85-90`) | downstream (none yet) | `dataset-bronze-available` | **`dataset-validated`** (ADR 0002) | `SERVICEBUS_BRONZE_QUEUE_NAME` |
| historical work | queue | `ingestion-func` (`function_app.py:394-398`) | `ingestion-func` (`function_app.py:411-415`) | `pvdaq-historical-work` | unchanged | `PVDAQ_HISTORICAL_QUEUE_NAME` |
| ingestion dead letter | queue | `ingestion-func` (`function_app.py:445,621`) | operator | `pvdaq-dead-letter` | **retired** (ADR 0001) — record rejects go to `quarantine`; unprocessable messages to the `pvdaq-historical-work` DLQ | `DEAD_LETTER_QUEUE_NAME` (removed) |
| processing dead letter | DLQ sub-queue | `processing-func` (`ProcessDatasetFunction.cs:53...172`) | operator | built-in DLQ of `raw-energy-events` | unchanged, but **narrowed** to unprocessable messages only (ADR 0001) | n/a (`host.json:14`, `autoComplete: false`) |

**Known drift** (see `docs/contract-migration.md`, task set R0):

- Root `servicebus-emulator/config.json:32` declares `raw-energy-events` as a **topic**; both services
  use it as a **queue**.
- `dataset-bronze-available` is absent from the root emulator config (present only in
  `processing-func/emulator/Config.json:18`).
- Cloud IaC provisions topic `dataset-events` + subscription `dataset-ingestion`
  (`service-bus.bicep:13,23`) — **neither queue used by the code exists in IaC**.

---

## 2. Events

### 2.1 `solar.pvdaq.dataset.available` → target `solar.pvdaq.dataset.available.v1`

- **Producer:** `ingestion-func`, `src/cloudevents_envelope.py:47`
- **Consumer:** `processing-func`, `ProcessDatasetFunction.cs:36-38`
- **Queue:** `raw-energy-events` · **Encoding:** `application/cloudevents+json`
- **Contract:** `specs/002-pvdaq-historical-ingestion/contracts/dataset-event.json`
  → target `contracts/dataset-available.v1.json` (ADR 0004)

| Envelope field | Type | Produced | Consumed | Note |
| :--- | :--- | :--- | :--- | :--- |
| `specversion` | `"1.0"` | yes | no | |
| `type` | string | yes | **no** | never asserted by the consumer — ADR 0002 requires it |
| `source` | string | yes | no | `/energy-ingestion-boundary/pvdaq` |
| `id`, `time`, `datacontenttype` | string | yes | no | |
| `tenant_id` | string | yes | no | non-standard extension |
| `source_vendor` | string | yes | **yes** | `"PVDAQ"`; registry lookup key |
| `schema_version` | string | yes | **yes** | registry lookup key |
| `mapping_version` | string | yes | no | hardcoded `"unknown"` (`:53`); meaningful under ADR 0006 |
| `correlation_id` | string | yes | **yes** | business key |
| `ingestion_timestamp` | string | yes | no | |
| `traceparent` | string | yes | no | synthesised, not a real trace — ADR 0003 |
| `data.site_id` | int | yes | **yes** | |
| `data.category` | string | yes | **yes** | |
| `data.storage_path` | string | yes | **yes** | container parsed from the URI at runtime |
| `data.file_format` | `"csv"` | yes | no | |
| `data.version` | int | yes | no | violates `additionalProperties: false` (`dataset-event.json:119`) |
| `data.ingestion_id` | uuid | yes | no | |
| `data.source_url` | string | yes | no | |
| `data.file_size` | int | yes | no | |
| `data.file_hash` | sha256 | yes | no | |

The consumer models **6 of 22 fields** (`ProcessDatasetFunction.cs:190-203`) and derives its own key
`datasetId` from `site_id` and `category` (`:66`).

### 2.2 `dataset.bronze.available` → target `solar.pvdaq.dataset.validated.v1`

- **Producer:** `processing-func`, `DatasetBronzeAvailablePublisher.cs:33` · **Consumer:** none yet
- **Current encoding:** flat JSON, `application/json` — **not a CloudEvent**
  (`ServiceBusEventPublisher.cs:33-39`). Target: CloudEvents envelope (ADR 0002).
- **Payload:** `dataset_id`, `record_count`, `bronze_path` (→ `silver_path`, ADR 0001),
  `schema_version`, `correlation_id`, `published_at`
- Sets `CorrelationId` and `MessageId` on the broker message — which the producer in §2.1 does not.

### 2.3 Retired types

| Type | Status | Note |
| :--- | :--- | :--- |
| `raw.pvdaq.generation.v1` | retired (`topics.md:21`) — but **still emitted** (`src/cloudevents_envelope.py:68`, `src/record_pipeline.py:37`) | must stop (ADR 0002) |
| `raw.pvdaq.historical.v1` | retired 2026-03-09 | not emitted |

### 2.4 Non-event message bodies

| Message | Fields | Cite |
| :--- | :--- | :--- |
| historical work item | `site_id`, `s3_key`, `file_name`, `category`, `correlation_id`, `enqueued_at`, `last_modified` | `function_app.py:385-393` |
| record dead letter — **retired**, becomes a quarantine `reason.json` (ADR 0001) | `original_payload`, `error_type`, `error_details[]`, `correlation_id`, `site_id`, `timestamp`, `source_vendor`, `schema_version` | `src/service_bus_emitter.py:38-47` |
| worker dead letter | `file_reference`, `failure_reason`, `error_type`, `correlation_id` | `function_app.py:446-452` |

The work item carries **no `traceparent`**, breaking the internal dispatcher → worker trace (ADR 0003).

---

## 3. Storage containers

| Layer | Current | Target (ADR 0001) | Contents | Format | Validated | Written by |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| source copy | `bronze` (`local.settings.json:31`) | `bronze` | exact source copy + `metadata.json` sidecar | CSV as fetched | **no** (`src/adls_store.py:147-158`) | `ingestion-func` |
| validated | `bronze` (`Program.cs:152`) — collision | **`silver`** | canonical typed records | Parquet | **yes** (`ProcessDatasetCommandHandler.cs:94`) | `processing-func` |
| rejected | *(none — DLQ only)* | **`quarantine`** | rejected **rows** with reason codes; whole files only when unparseable | rejected rows + `reason.json` | n/a | `processing-func`, `ingestion-func` |
| schema registry | `schema-registry` (`Program.cs:160`) | unchanged | vendor field mappings | JSON | n/a | operator |

Path layouts:

- **bronze** — `source=pvdaq/dataset={site_id}_{category}/ingestion_date={YYYY-MM-DD}/{dataset}_v{n}.csv`
  (`function_app.py:94-100`), sidecar `metadata.json` (`:109-112`)
- **silver** — `{datasetId}/{date}/data.parquet` (`OneLakeBronzeWriter.cs:42`). Currently the date is
  **processing time** (`ProcessDatasetCommandHandler.cs:120`); the target is the **data date taken from
  the records**, so re-runs and backfills are idempotent (ADR 0001)
- **schema registry** — `{vendorId}-{schemaVersion}.json` (`BlobSchemaRegistry.cs:46`)

> **Current collision:** source CSVs and validated Parquet share the container `bronze`, with two
> incompatible layouts. This is the defect ADR 0001 resolves. The name `raw` is configured **nowhere**
> — it survives only in docstrings (`src/adls_store.py:44`).

---

## 4. Storage API

| Service | Path | Current client | Target (ADR 0005) | Cite |
| :--- | :--- | :--- | :--- | :--- |
| `ingestion-func` | cloud | `DataLakeServiceClient` + `DefaultAzureCredential` | **`BlobServiceClient`** | `src/adls_store.py:83-87` |
| `ingestion-func` | local | `BlobServiceClient` (append blob) | `BlobServiceClient` | `src/adls_store.py:69-71,117-118` |
| `processing-func` | data read/write | `DataLakeServiceClient`, pointed at Azurite's **blob** port locally | **`BlobServiceClient`** | `Program.cs:94-102`, `local.settings.json:10` |
| `processing-func` | schema registry | `BlobServiceClient` | unchanged | `BlobSchemaRegistry.cs:4,16` |

**ADLS-only features used: none.** No atomic rename, no directory operations, no ACLs in either repo.
`ingestion-func` uses `append_data` / `flush_data` (`src/adls_store.py:151-158`) on the cloud path only,
and already has a Blob equivalent on its local path. This is the evidence base for ADR 0005.

**Account provisioning (target).** Although the code is Blob-only, the cloud data account is provisioned
with **`isHnsEnabled: true`** (ADLS Gen2), because an HNS account keeps its Blob endpoint fully available
while leaving a Fabric/OneLake shortcut possible with no code change — and HNS cannot be enabled after
creation. No data-lake account exists in IaC today; see task R4.1.

---

## 5. Schemas

| Schema | Kind | Owner | Read by | Enforced | Target location |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `pvdaq-v1.json` | JSON Schema, per record | `ingestion-func` | `src/schema_validator.py:14-16` | write time, per record (`src/record_pipeline.py:64`) | stays in service |
| `PVDAQ-v1.json` | **vendor field mapping**, not a schema | `processing-func` | `BlobSchemaRegistry.cs:46-54` | read time | schema-registry container (ADR 0004) |
| `dataset-event.json` | JSON Schema, cross-service | Contract Owner | **nobody** | **never** | `contracts/dataset-available.v1.json` |
| `work-item-message.json` | JSON Schema | `ingestion-func` | `function_app.py:73-78` | inbound (`:440`) | `contracts/work-item.v1.json` |
| `cloudevents-envelope.json`, `dead-letter-message.json` | JSON Schema | `ingestion-func` | tests only | tests only | `contracts/` |
| `metadata-file.json`, `file-tracking-entity.json` | JSON Schema | `ingestion-func` | **nobody** | **never** | `contracts/metadata-file.v1.json` |

> `pvdaq-v1.json` (a JSON Schema in `ingestion-func`) and `PVDAQ-v1.json` (a field-mapping config in
> `processing-func`) differ only by case and are **unrelated artifacts**. ADR 0004 separates them.

> The cross-service contract `dataset-event.json` is validated by no code and no test, and is already
> violated by the emitted `data.version` field. ADR 0004 makes it enforced.
