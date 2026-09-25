# Contract migration plan

Tasks implementing ADRs 0001–0005 (`docs/adr/`). Registry of current vs target state:
`docs/contracts.md`.

Each task names a **single owning repo**, its **inputs**, its **outputs** and a **measurable
acceptance criterion**, per the Planner gate in `docs/agent-fleet.md` §3.1. Cross-repo tasks are
routed through the Contract Owner first (§3.4).

**Sequencing.** R0 is a hard prerequisite: the local stack does not currently start, so no task after
it can be verified. R1 precedes the service work because both services vendor from it. R4 is cloud
infrastructure and is independent of the local path, so it can run in parallel. E2E-1 is the acceptance
gate for ADRs 0001, 0003 and 0005 and runs last.

```
R0 (root: make the stack runnable)
   └─ R1 (root: contracts/)
        ├─ R2 (ingestion-func)
        └─ R3 (processing-func)
             └─ E2E-1 (acceptance gate: ADR 0001, 0003, 0005)

R4 (root: infrastructure) ── parallel; gates cloud deploy, not E2E-1
```

---

## R0 — Workspace root: make the local stack runnable *(prerequisite)*

Owning repo: **anomalia-platform (root)**. None of this is a design change; it is drift that blocks
verification of everything else.

### R0.1 Fix the compose build contexts
- **Input:** `docker-compose.yml:74,104` (`./ingestion_func`, `./processing_func`)
- **Output:** contexts pointing at `services/ingestion-func`, `services/processing-func`
- **Accept:** `docker compose build` completes without a "path not found" error.

### R0.2 Make `.env.example` able to start both services
- **Input:** `.env.example`, the required-variable lists in `src/config.py:153,176-177,216-225` and
  `Program.cs:85-90,96-97,152,160`
- **Output:** `.env.example` defines `SERVICE_BUS_QUEUE_NAME` (replacing `SERVICE_BUS_TOPIC_NAME`),
  `SERVICEBUS_QUEUE_NAME`, `SERVICEBUS_BRONZE_QUEUE_NAME`, `ADLS_ACCOUNT_URL`, `ADLS_CONTAINER_NAME`,
  and the correct Azurite well-known key for `AzureWebJobsStorage` (currently the Service Bus emulator
  key, `.env:11`)
- **Accept:** `cp .env.example .env && docker compose up` brings both functions to a healthy state with
  no `ConfigurationError` in either log.

### R0.3 Reconcile the emulator topology
- **Input:** `servicebus-emulator/config.json`, `services/processing-func/emulator/Config.json`
- **Output:** one authoritative root config declaring `raw-energy-events`, `pvdaq-historical-work` and
  `dataset-validated` **as queues**; the submodule copy either removed from the root stack's path or
  documented as standalone-only. `pvdaq-dead-letter` is **not** declared — ADR 0001 retires it in favour
  of quarantine plus each queue's own broker DLQ.
- **Accept:** every queue name read from configuration by either service exists in the root emulator
  config, verified by a script that diffs the two name sets and exits non-zero on any difference.

### R0.4 Purge tracked emulator state and a committed tenant name
- **Input:** `git ls-files` in `services/ingestion-func` returns `AzuriteConfig`,
  `__blobstorage__/AzuriteConfig` and three `__blobstorage__/<guid>` blobs; both services' settings
  files contain `jorgevr.servicebus.windows.net`
- **Output:** tracked Azurite artifacts removed with `git rm --cached`; the real namespace replaced by
  a placeholder
- **Accept:** `git ls-files | grep -E '__blobstorage__|AzuriteConfig'` returns empty in both submodules,
  and no tracked file contains the literal tenant namespace.
- **Note:** owned by each submodule's agent, not the root — the Contract Owner raises it, the service
  agent performs it.

---

## R1 — Workspace root: establish `contracts/` *(ADR 0004)*

Owning repo: **anomalia-platform (root)**.

### R1.1 Create the authoritative contracts folder
- **Input:** the current scattered copies under `services/*/specs/*/contracts/`
- **Output:** `contracts/dataset-available.v1.json`, `contracts/dataset-validated.v1.json`,
  `contracts/work-item.v1.json`, `contracts/dead-letter.v1.json`, `contracts/metadata-file.v1.json`
- **Accept:** every file validates as a Draft 2020-12 JSON Schema, and
  `contracts/dataset-available.v1.json` admits the **full** field set in `docs/contracts.md` §2.1 —
  including `data.version`, which today violates `additionalProperties: false`
  (`dataset-event.json:119`).

### R1.2 Publish the drift check in the workspace CI
- **Input:** `contracts/`, each service's vendored `schemas/contracts/`
- **Output:** a drift check that runs in **this (workspace) repo's CI**, which checks out both
  submodules and is therefore the only place that can see `contracts/` and both vendored copies at
  once. It does **not** run in the service repos (ADR 0004, rule 4).
- **Accept:** the check exits non-zero when a vendored copy differs from the root copy by a single byte,
  proven by a deliberate one-character mutation in a test run; and it fails, rather than passing
  vacuously, when a submodule is not checked out.

---

## R2 — `ingestion-func` *(Python)*

Owning repo: **services/ingestion-func**. Gates: `ruff check .` and `pytest` (`docs/agent-fleet.md` §4).

### R2.1 Collapse to the Blob API *(ADR 0005)*
- **Input:** `src/adls_store.py:24,83-87,121-126,136,151-158,270-279` (DataLake branch),
  `:69-71,117-118,202,216,266` (existing Blob path), `function_app.py:115-119` (`STORAGE_EMULATOR`)
- **Output:** a single `BlobServiceClient` implementation; the emulator flag replaced by one
  connection-string-or-credential switch; `src/idempotency_store.py:55` gains the local branch it
  currently lacks
- **Accept:** no import of `azure.storage.filedatalake` remains in the repo; the existing integration
  suite passes against Azurite unchanged; the same code path executes locally and in cloud
  configuration, asserted by a test that exercises both credential modes.

### R2.2 Stop emitting the retired event type *(ADR 0002)*
- **Input:** `src/cloudevents_envelope.py:68`, `src/record_pipeline.py:37`, `topics.md:21`
- **Output:** `raw.pvdaq.generation.v1` no longer emitted
- **Accept:** no test or code path produces a `type` beginning `raw.`; `topics.md` lists only currently
  emitted types.

### R2.3 Adopt the versioned type and validate outbound *(ADR 0002, 0004)*
- **Input:** `src/cloudevents_envelope.py:47`, `contracts/dataset-available.v1.json`
- **Output:** type `solar.pvdaq.dataset.available.v1`; a `dataschema` attribute; envelope validated
  against the vendored contract **before** send; `mapping_version` populated rather than hardcoded
  `"unknown"` (`:53`)
- **Accept:** a contract test asserts every emitted envelope validates against
  `schemas/contracts/dataset-available.v1.json`, and an envelope with an unknown extra field fails
  that test.

### R2.4 Real trace context *(ADR 0003)*
- **Input:** `function_app.py:176-178,464-466` (synthesised IDs), `:385-393` (work item),
  `src/service_bus_emitter.py:145-153` (message properties), `requirements.txt`
- **Output:** Azure Monitor OpenTelemetry distro configured; `traceparent` derived from the live span
  context; `traceparent` written to Service Bus `application_properties` **and** the envelope;
  `correlation_id` and `message_id` set on `ServiceBusMessage`; `traceparent` added to the work item
- **Accept:** an integration test asserts the `traceparent` on the emitted message parses as valid W3C
  trace context **and** shares its `trace-id` with the span active at emission — i.e. it is no longer a
  random value. Dispatcher and worker spans share one trace ID.

### R2.5 Correct stale documentation *(ADR 0001)*
- **Input:** `src/adls_store.py:44`, `CLAUDE.md:10`,
  `specs/002-pvdaq-historical-ingestion/contracts/work-item-message.json:42`
- **Output:** all three describe the configured container `bronze` and the real layout
- **Accept:** no occurrence of the container name `raw` remains outside historical spec text.

### R2.6 Retire `pvdaq-dead-letter`; record rejects go to quarantine *(ADR 0001)*
- **Input:** `src/service_bus_emitter.py:38-47` (record dead-letter body), `function_app.py:445,621`
  (send sites), `src/record_pipeline.py:64,76` (per-record validation), `DEAD_LETTER_QUEUE_NAME` in
  `src/config.py:154,218` and all settings files
- **Output:** record-level validation rejects are written to `quarantine` with a reason code and the
  same identity fields as the processing side (site, category, source URI, `correlation_id`,
  `traceparent`), instead of being published to a queue. Genuinely unprocessable messages — a work item
  failing schema validation (`function_app.py:440`) or an unreadable S3 source — dead-letter to the
  **broker DLQ of `pvdaq-historical-work`**. The `pvdaq-dead-letter` queue and its config key are removed.
- **Accept:** a batch containing invalid records produces quarantine objects and **no** message on any
  application-level dead-letter queue; `DEAD_LETTER_QUEUE_NAME` appears nowhere in code or settings; a
  work item with a malformed body still lands in the `pvdaq-historical-work` DLQ.

---

## R3 — `processing-func` *(.NET)*

Owning repo: **services/processing-func**. Gates: `dotnet format --verify-no-changes`, `dotnet build`,
`dotnet test` (`docs/agent-fleet.md` §4).

### R3.1 Rename the validated layer to `silver` *(ADR 0001)*
- **Input:** `Program.cs:152`, `OneLakeBronzeWriter.cs`, `ProcessDatasetCommandHandler.cs:117-120`,
  `local.settings.json:15`, `local.settings.json.template:13`, `scripts/seed-azurite.js:13`
- **Output:** `SILVER_FILESYSTEM`; `OneLakeBronzeWriter` → `SilverWriter` with its tests renamed
- **Accept:** after a run, `bronze` contains only CSV and `metadata.json`, `silver` contains only
  Parquet, and no code reads `BRONZE_FILESYSTEM`.

### R3.2 Add the quarantine layer *(ADR 0001)*
- **Input:** the eight dead-letter reason codes at `ProcessDatasetFunction.cs:53,76,104,112,121,135,157,172`
- **Output:** rejected data written to `quarantine` with a `reason.json` carrying the reason code,
  full source identity (site, device, category, source URI, version), `correlation_id` and
  `traceparent`; the reason-code vocabulary is **open**, so ADR 0006 can add `UNMAPPED_SCHEMA`
- **Accept:** a file that cannot be parsed at all produces exactly one object in `quarantine` whose
  `reason.json` names the reason code. Adding a new reason code requires no change to any message
  contract.

### R3.3 Split failure routing: DLQ for messages, quarantine for data *(ADR 0001)*
- **Input:** the eight dead-letter sites at `ProcessDatasetFunction.cs:53,76,104,112,121,135,157,172`,
  classified against ADR 0001 §Failure routing
- **Output:** `DeserializationFailed`, `InvalidStoragePath`, `UnknownSchema`, `SourceFileNotFound` and an
  unrecognised `type` continue to dead-letter (unprocessable *messages*). `ValidationFailed`,
  `EmptyDataset` and `UnsupportedEncoding` instead write to `quarantine` and **complete** the message.
- **Accept:** a message whose data is readable but invalid leaves the trigger queue's dead-letter
  sub-queue **empty** and produces quarantine output; a message with an unparseable envelope
  dead-letters and writes nothing. Both asserted in the integration suite.

### R3.4 Row-level quarantine *(ADR 0001)*
- **Input:** `ProcessDatasetCommandHandler.cs:94-100`, which currently fails the whole file on any
  validation failure; `Domain/Services/DataQualityValidator.cs`
- **Output:** the validator partitions records into accepted and rejected rather than throwing; accepted
  rows are written to `silver`, rejected rows to `quarantine` with a per-row reason code. Whole-file
  quarantine is reserved for files that cannot be parsed at all.
- **Accept:** for a fixture with N total rows of which K are invalid, `silver` contains exactly N−K rows,
  `quarantine` contains exactly K rows each carrying a reason code, the message is completed, and
  `N−K + K = N` is asserted rather than assumed. A fixture with K=0 writes nothing to `quarantine`; a
  fixture with K=N writes nothing to `silver`.

### R3.5 Partition silver by data date *(ADR 0001)*
- **Input:** `OneLakeBronzeWriter.cs:42` and `ProcessDatasetCommandHandler.cs:120`, which use processing
  time; the mapped timestamp field from the vendor mapping
- **Output:** the partition key derives from the records' own timestamp field. A file spanning two days
  produces two partitions; re-running the same file is idempotent and lands in the same partitions.
- **Accept:** processing a fixture whose rows span two data dates produces exactly two date partitions
  matching those dates, and re-running it changes neither the partition set nor the row counts —
  which fails today, because a re-run would write a new processing-date partition.

### R3.6 Collapse to the Blob API *(ADR 0005)*
- **Input:** `Program.cs:94-113`, `AdlsDatasetReader.cs:32-41`, `OneLakeBronzeWriter.cs:45-56`
- **Output:** `DataLakeServiceClient` removed in favour of the already-registered `BlobServiceClient`;
  the exact-equality connection-string test at `Program.cs:107-109` replaced by a robust check;
  OneLake-flavoured names (`ONELAKE_ENDPOINT`, `OneLakeBronzeWriter`) made storage-neutral
- **Accept:** no reference to `Azure.Storage.Files.DataLake` remains; `dotnet test` passes; the service
  reads a blob-form `storage_path` end to end.


### R3.7 Assert the event type and validate inbound *(ADR 0002, 0004)*
- **Input:** `ProcessDatasetFunction.cs:36-38,190-203`, `contracts/dataset-available.v1.json`
- **Output:** inbound validated against the vendored contract before deserialisation; `type` asserted
  against `solar.pvdaq.dataset.available.v1`; unrecognised types dead-lettered with a distinct reason
- **Accept:** a message with type `some.other.event.v1` dead-letters instead of being processed —
  today it would be processed.

### R3.8 Link the incoming trace *(ADR 0003)*
- **Input:** `ProcessDatasetFunction.cs` entry, `ProcessDatasetCommandHandler.cs:60-63`;
  the requirement already recorded at `specs/001-dataset-ingestion-pipeline/tasks.md:162`
- **Output:** `traceparent` read from `ApplicationProperties`, falling back to the envelope;
  `ActivityContext.Parse` used to parent (or link) `dataset.process`
- **Accept:** a test asserts the `dataset.process` activity's `TraceId` equals the `trace-id` of the
  inbound `traceparent`. This fails today.

### R3.9 Publish the renamed outbound event as a CloudEvent *(ADR 0001, 0002)*
- **Input:** `DatasetBronzeAvailablePublisher.cs:33`, `ServiceBusEventPublisher.cs:29-39`
- **Output:** type `solar.pvdaq.dataset.validated.v1`; CloudEvents envelope with
  `application/cloudevents+json`; `bronze_path` → `silver_path`; queue `dataset-validated`
- **Accept:** the emitted message validates against `contracts/dataset-validated.v1.json` and carries a
  `specversion`.

---

## R4 — Workspace root: cloud infrastructure *(ADR 0005)*

Owning repo: **anomalia-platform (root)** — `infrastructure/`. Independent of the local stack; this gates
cloud deployment, not E2E-1. Today the deployed app cannot start: the only provisioned storage account is
the Functions runtime account (`storage.bicep:6-17`, consumed as `AzureWebJobsStorage` at
`function-app.bicep:27`), neither queue the code uses exists in IaC, and the data-plane app settings are
absent.

### R4.1 Provision the data-lake account with a hierarchical namespace
- **Input:** `infrastructure/modules/storage.bicep`, which creates a flat `StorageV2` account
- **Output:** a **separate** data-lake account with **`isHnsEnabled: true`** (ADLS Gen2), distinct from
  the Functions runtime account, with containers `bronze`, `silver` and `quarantine` (ADR 0001)
- **Accept:** `az deployment group what-if` shows the account with `isHnsEnabled: true` and all three
  containers. HNS **cannot** be enabled after creation, so this must be right the first time — it is the
  one property in this plan that is not reversible in place.

### R4.2 Provision the queues the code actually uses
- **Input:** `infrastructure/modules/service-bus.bicep:13,22`, which provisions topic `dataset-events`
  and subscription `dataset-ingestion` — neither of which any code reads
- **Output:** queues `raw-energy-events`, `pvdaq-historical-work` and `dataset-validated`. The unused
  topic and subscription are removed; `pvdaq-dead-letter` is **not** provisioned (retired by ADR 0001).
- **Accept:** `az deployment group what-if` shows exactly those three queues, and every queue name read
  from configuration by either service appears in the plan.

### R4.3 Supply the missing app settings
- **Input:** `infrastructure/modules/function-app.bicep:26-36`, which sets only `ONELAKE_ENDPOINT` from
  the data-plane group; `SCHEMA_REGISTRY_ACCOUNT` is read at `Program.cs:111` but defined in no settings
  file at all
- **Output:** the storage endpoint (blob form, per ADR 0005), `SILVER_FILESYSTEM`, the quarantine
  container, `SCHEMA_REGISTRY_CONTAINER`, `SCHEMA_REGISTRY_ACCOUNT` and both queue names, on both the
  production and staging slots; the `ONELAKE_*` names retired with the code rename in R3.6
- **Accept:** `az deployment group what-if` shows every variable the two services require at startup, and
  no setting is present on the production slot but missing from staging.

---

## E2E-1 — End-to-end acceptance gate *(ADR 0001, 0003, 0005)*

Owning repo: **anomalia-platform (root)**. This is the acceptance check for layer naming, trace
propagation and storage parity. It runs entirely on the local emulator stack — which ADR 0005 makes
possible, since there is no longer a cloud-only code path.

- **Input:** the root emulator stack (`docker-compose.yml`) after R0; a fixture of **four** inputs — one
  fully valid CSV; one CSV with a known number of invalid rows; one structurally unparseable CSV; and one
  message with a deliberately invalid envelope.
- **Steps:** trigger ingestion → `bronze` → `raw-energy-events` → processing → `silver` /
  `quarantine` → `dataset-validated`.
- **Accept — all must hold:**
  1. **Row counts reconcile per layer, at row granularity** (ADR 0001 §Failure routing). For a fixture
     of N rows of which K are invalid:
     - `bronze` holds N source rows (the exact source copy, always all of them);
     - `silver` holds exactly **N−K** rows;
     - `quarantine` holds exactly **K** rows, each with a reason code;
     - `silver + quarantine = bronze` holds for every fixture — the no-silent-drops identity required by
       `docs/agent-fleet.md` §5.

     For the fully valid fixture K=0, so `quarantine` stays empty and `silver` = N. The partly invalid
     fixture must yield a **partial** load, not a failed one: K>0 and N−K>0 must both be observed, which
     is what distinguishes row-level from file-level handling.
  2. **Failure routing is respected** (ADR 0001). The partly invalid fixture's message is **completed**,
     leaving the `raw-energy-events` dead-letter sub-queue **empty**. The unparseable CSV is quarantined
     whole, with a file-level reason code, and its message is also completed. Only the invalid-envelope
     message dead-letters — and it writes nothing to any layer. `bronze` contains only CSV plus
     `metadata.json`; `silver` contains only Parquet.
  3. **One trace spans both services.** The `dataset.process` activity's trace ID equals the trace ID
     of the span that emitted the event, and one `correlation_id` appears in both services' logs for a
     single file.
  4. **No cloud-only path.** The whole run completes against Azurite with no real Azure credential and
     no `azure.storage.filedatalake` / `Azure.Storage.Files.DataLake` reference loaded.
  5. **Contracts enforced.** Every message crossing `raw-energy-events` validates against
     `contracts/dataset-available.v1.json`; every message on `dataset-validated` validates against
     `contracts/dataset-validated.v1.json`.

---

## Not in scope

ADR 0006 (per-device schema mapping) is **deferred by design**. The obligations that keep it possible
are already carried by tasks R2.3 (`mapping_version` populated), R3.2 and R3.4 (open reason-code
vocabulary, row-level quarantine retaining source identity) and R3.8 (trace linked onto quarantined
records). No task here implements fingerprint resolution.
