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

**Status: done.** The registry describes the **target** state from ADRs 0001–0005, not what the code
does. Every difference is listed under its owning R2/R3 task below, under the heading *Contract
differences (target wins)*. A service agent reconciles its code to the contract; it never edits the
contract, and never hand-edits its vendored copy.

### R1.1 Create the authoritative contracts folder
- **Input:** the current scattered copies under `services/*/specs/*/contracts/`
- **Output:** `contracts/dataset-available.v1.json`, `contracts/dataset-validated.v1.json`,
  `contracts/quarantine-record.v1.json`, `contracts/metadata-file.v1.json`, plus
  `contracts/examples/<contract>/{valid,invalid}-*.json`, `contracts/vendoring.json` (the
  `{service: [contract files]}` manifest, empty until R2.3 and R3.7 populate it) and
  `contracts/README.md`.
  - `dead-letter.v1.json` from the ADR 0004 draft list is **replaced by** `quarantine-record.v1.json`:
    ADR 0001 retires `pvdaq-dead-letter` and narrows the broker DLQ to unprocessable messages, so the
    artifact that actually crosses a boundary is the `reason.json` written into `quarantine`.
  - `work-item.v1.json` is **not** in `contracts/`: the dispatcher → worker item is
    `ingestion-func`-internal and stays at `services/ingestion-func/schemas/`.
- **Accept:** every file validates as a Draft 2020-12 JSON Schema, and
  `contracts/dataset-available.v1.json` admits the **full** field set in `docs/contracts.md` §2.1 —
  including `data.version`, which today violates `additionalProperties: false`
  (`dataset-event.json:119`). Each contract carries at least 2 valid and 3 invalid examples, each
  invalid one breaking a **different** rule; `scripts/check-contracts.py` asserts all of that, the
  distinctness included.

### R1.2 Publish the drift check in the workspace CI
- **Input:** `contracts/`, `contracts/vendoring.json`, each service's vendored `schemas/contracts/`
- **Output:** `scripts/check-contracts.py` and `.github/workflows/contracts.yml` — a drift check that
  runs in **this (workspace) repo's CI**, which checks out both submodules (`submodules: recursive`)
  and is therefore the only place that can see `contracts/` and both vendored copies at once. It does
  **not** run in the service repos (ADR 0004, rule 4). The workflow also runs
  `scripts/verify-queue-topology.py` (R0.3), so contract and topology drift are one gate.
  The script checks four things: every schema is valid Draft 2020-12 with an `$id` matching its
  filename; every example behaves as its name declares, with no two invalid examples of one contract
  breaking the same rule; every manifest entry is byte-identical to the root copy; and every service
  named in the manifest is a checked-out submodule.
- **Accept:** the check exits non-zero when a vendored copy differs from the root copy by a single byte,
  proven by a deliberate one-character mutation in a test run; and it fails, rather than passing
  vacuously, when a submodule is not checked out.
- **Verified 2026-09-25**, from a throwaway copy of the root so `services/` was never written to:

  | Case | Expected | Result |
  | :--- | :--- | :--- |
  | registry as committed, at the real root | pass | exit 0, 40/40 checks |
  | vendored copy byte-identical, manifest entry present | pass | exit 0, 42/42 checks |
  | one byte mutated in the vendored copy (offset 158, `i` → `T`) | fail | exit 1, names the file and the offset |
  | temporary manifest entry removed | pass | exit 0, 40/40 checks |
  | a listed submodule directory emptied | fail | exit 1, "submodule … is not checked out" |

  The examples were additionally re-validated with `date-time` format assertion forced on, matching the
  CI image (which installs `rfc3339-validator`): no change in outcome, so the result does not depend on
  which format packages happen to be installed.

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
- **Contract differences (target wins)** — `contracts/dataset-available.v1.json`:
  - `data.storage_path` is constrained to `^https?://[^ ]+$`. The cloud branch emits the `abfss://`
    form today (`function_app.py:53-66`), which the contract now rejects; the blob form is the only
    legal value (ADR 0005). Example: `examples/dataset-available.v1/invalid-abfss-storage-path.json`.
  - The same pattern forbids a **literal space**. `_adls_uri` interpolates the dataset name unencoded,
    which is why `processing-func` carries a `Replace(" ", "%20")` hack
    (`ProcessDatasetFunction.cs:70`); percent-encode at the producer so the consumer can drop it
    (see R3.6).

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
  that test. The extra field must be an **envelope** attribute: `data` is open by construction, so an
  extra field there is a legal minor version and will *not* fail (see below).
- **Vendoring:** add `"ingestion-func": ["dataset-available.v1.json", "metadata-file.v1.json"]` to
  `contracts/vendoring.json` and copy both files byte-for-byte into
  `services/ingestion-func/schemas/contracts/`. Until that entry exists the workspace drift check has
  nothing to compare for this service and passes vacuously.
- **Contract differences (target wins)** — `contracts/dataset-available.v1.json`:
  - `type` is `const "solar.pvdaq.dataset.available.v1"`; `src/cloudevents_envelope.py:47` emits the
    unversioned `solar.pvdaq.dataset.available`.
  - `dataschema` is **required** and `const` the contract's own `$id`
    (`https://github.com/jorgevr/anomalIA-app/contracts/dataset-available.v1.json`). No `dataschema`
    attribute is emitted today.
  - `data.version` is **declared and required**. It is emitted today but rejected by
    `specs/002-.../contracts/dataset-event.json:119`; the registry copy admits it, which is the
    decorative-to-enforced fix ADR 0004 exists for.
  - **The envelope is closed, `data` is open.** Unknown envelope attributes fail; additive `data`
    fields validate, because ADR 0002 rule 2 makes them a minor version that a consumer holding an
    older vendored copy must still accept — this is what lets ADR 0006's `device_id` ship without a
    consumer redeploy. See `examples/dataset-available.v1/valid-rerun-cloud-with-additive-device-id.json`.
  - `mapping_version` keeps `unknown` as a **legal explicit sentinel** (`^(v[0-9]+|unknown)$`): there is
    no field mapping at dataset level, and ADR 0006 is deferred. What this task removes is the
    **hardcoded literal** at `src/cloudevents_envelope.py:53` — the value must come from configuration,
    as `build_envelope` already does via `config.mapping_version_pvdaq`. ADR 0006 later narrows the
    pattern to `^v[0-9]+$`, which is a major-version change to this contract.
  - `correlation_id`, `id` and `ingestion_id` are constrained to **lowercase canonical UUIDs**;
    `tenant_id` must be non-empty.
  - Every `format` is backed by an equivalent `pattern`, so the vendored copy validates identically
    whether or not `jsonschema`'s format assertion and its optional format packages are active. Do not
    rely on `format` alone in the runtime validator.
- **Contract differences (target wins)** — `contracts/metadata-file.v1.json`:
  - Same field set as `specs/002-.../contracts/metadata-file.json`, re-homed with a registry `$id`.
  - `ingestion.batch_id` is tightened from a free string to a UUID: it is the ADR 0003 business key of
    the dispatcher run, and `function_app.py:532` already passes `correlation_id` into it.
  - The sidecar is written but validated by nobody today. ADR 0004 rule 3 requires validation at
    **write** time against the vendored copy, not only in tests.

### R2.4 Real trace context *(ADR 0003)*
- **Input:** `function_app.py:176-178,464-466` (synthesised IDs), `:385-393` (work item),
  `src/service_bus_emitter.py:145-153` (message properties), `requirements.txt`
- **Output:** Azure Monitor OpenTelemetry distro configured; `traceparent` derived from the live span
  context; `traceparent` written to Service Bus `application_properties` **and** the envelope;
  `correlation_id` and `message_id` set on `ServiceBusMessage`; `traceparent` added to the work item
- **Accept:** an integration test asserts the `traceparent` on the emitted message parses as valid W3C
  trace context **and** shares its `trace-id` with the span active at emission — i.e. it is no longer a
  random value. Dispatcher and worker spans share one trace ID.
- **Contract differences (target wins)** — `contracts/dataset-available.v1.json`:
  - `traceparent` rejects the degenerate all-zero `trace-id` and all-zero `parent-id` that W3C forbids
    (`examples/dataset-available.v1/invalid-zeroed-traceparent.json`). Note the limit: a `traceparent`
    synthesised from `uuid4` is **well-formed** and no schema can catch it, which is precisely why this
    task's acceptance is a test against the live span rather than a contract rule.
  - `correlation_id` must be a lowercase canonical UUID, so the value set on
    `ServiceBusMessage.correlation_id` and the envelope copy are the same string, not two spellings.
  - The contract constrains the **envelope** only. That `traceparent` also travels in Service Bus
    `application_properties` (ADR 0003 option C, the load-bearing half) is not expressible here and
    stays a test obligation.

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
- **Vendoring:** add `quarantine-record.v1.json` to this service's `contracts/vendoring.json` entry and
  validate each `reason.json` against the vendored copy before writing it.
- **Contract differences (target wins)** — `contracts/quarantine-record.v1.json` replaces the
  `pvdaq-dead-letter` body at `src/service_bus_emitter.py:38-47`, and the shapes differ substantially:
  - **Destination:** a blob in `quarantine`, not a Service Bus message. There is no
    `dead-letter.v1.json` in the registry; see R1.1.
  - `error_type: "validation_failure"` becomes `reason_code`, drawn from an **open, SCREAMING_SNAKE**
    vocabulary (`^[A-Z][A-Z0-9_]{2,63}$`) shared with `processing-func` — `SCHEMA_VALIDATION_FAILED`,
    `TYPE_MISMATCH`, `MISSING_REQUIRED_FIELD`, and so on. The shape is fixed; membership is not, so
    ADR 0006's `UNMAPPED_SCHEMA` needs no contract change.
  - `scope` (`row` | `file`) is **new** and required. Ingestion's record rejects are `row`, which makes
    `record_count` required too.
  - `source` is a required block carrying `source_vendor`, `site_id`, **`device_id`** (explicitly
    `null` until ADR 0006), `category`, `source_uri` and `version`. Today's body carries `site_id` and
    `source_vendor` only, so category, source URI, device and version are all new — ADR 0001 requires
    full source identity so a quarantined file can be re-driven.
  - `traceparent` is **required**; no current reject payload carries one. This task therefore depends on
    R2.4, not merely follows it.
  - `original_payload` has **no equivalent field**. The rejected records themselves are the quarantined
    data object (`data_path`); `detail` is the open slot for validator context. Do not smuggle a payload
    copy into `detail` — quarantine holds the rows.
  - `error_details[]` maps to `rows[]`, keyed by `row_index` + a per-row `reason_code`, and is explicitly
    a **truncated** summary rather than the authoritative record.

---

## R3 — `processing-func` *(.NET)*

Owning repo: **services/processing-func**. Gates: `dotnet format --verify-no-changes`, `dotnet build`,
`dotnet test` (`docs/agent-fleet.md` §4).

**Execution order:** R3.6 first (the local stack cannot get past the storage client until it is done), then R3.1 → R3.5, R3.7 → R3.9. Task IDs are kept stable so ADR references stay valid.

### R3.6 Collapse to the Blob API *(ADR 0005)*
- **Input:** `Program.cs:94-113`, `AdlsDatasetReader.cs:32-41`, `OneLakeBronzeWriter.cs:45-56`
- **Output:** `DataLakeServiceClient` removed in favour of the already-registered `BlobServiceClient`;
  the exact-equality connection-string test at `Program.cs:107-109` replaced by a robust check;
  OneLake-flavoured names (`ONELAKE_ENDPOINT`, `OneLakeBronzeWriter`) made storage-neutral
- **Accept:** no reference to `Azure.Storage.Files.DataLake` remains; `dotnet test` passes; the service
  reads a blob-form `storage_path` end to end.
- **Contract differences (target wins)** — `contracts/dataset-available.v1.json`:
  - `data.storage_path` is `^https?://[^ ]+$`, so `AdlsDatasetReader.ParseAdlsUri`'s `abfss://` branch
    becomes dead code once R2.1 lands.
  - The pattern also forbids a literal space, which retires the `Replace(" ", "%20")` workaround at
    `ProcessDatasetFunction.cs:70`: once inbound validation (R3.7) runs, an unencoded path is a
    contract violation to dead-letter, not a shape to repair. Remove the hack rather than keeping it as
    belt and braces — it would mask the violation the contract now catches.

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
- **Vendoring:** add `quarantine-record.v1.json` to this service's `contracts/vendoring.json` entry and
  validate each `reason.json` against the vendored copy before writing it.
- **Contract differences (target wins)** — `contracts/quarantine-record.v1.json`. The current
  dead-letter reason payload (`specs/main/contracts/dead-letter-reason.json`) is *not* the shape:
  - **Reason codes are renamed and their vocabulary re-typed.** PascalCase `error_type` values become
    SCREAMING_SNAKE `reason_code` values matching `^[A-Z][A-Z0-9_]{2,63}$` — `UnsupportedEncoding` →
    `UNSUPPORTED_ENCODING`, `EmptyDataset` → `EMPTY_DATASET`, `ValidationFailed` →
    `SCHEMA_VALIDATION_FAILED`, plus the file-level `UNPARSEABLE_CSV` this task introduces. The field is
    a **pattern, never an enum**: that is what makes ADR 0006's `UNMAPPED_SCHEMA` a no-contract-change
    addition, and it is asserted by
    `examples/quarantine-record.v1/invalid-lowercase-reason-code.json`.
  - Only the codes ADR 0001 classifies as *data* problems appear here. `DeserializationFailed`,
    `InvalidStoragePath`, `UnknownSchema` and `SourceFileNotFound` stay broker DLQ reasons and get no
    quarantine record at all (R3.3) — there is no `reason_code` for them.
  - `dataset_id` is **not a field**. Identity is structured: `source.site_id` + `source.category`, from
    which `dataset_id` is derived. The flat `{datasetId}` string loses the parts a re-drive needs.
  - `source` additionally requires `device_id` (explicitly `null` for now), `source_uri` and `version`,
    none of which the current payload carries.
  - `correlation_id` and `traceparent` are both **required**, so this task depends on R3.8 landing the
    inbound trace context — a quarantine record written without it fails validation rather than
    silently losing provenance.
  - `quarantined_by` and `quarantined_at` are new and required; the reason-code-specific extras
    (`detected_encoding`, `fail_count`) move into the open `detail` object.

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
- **Contract differences (target wins)**:
  - `contracts/quarantine-record.v1.json` makes `record_count` **conditionally required**: present and
    ≥ 1 when `scope` is `row`, omitted when `scope` is `file`. A row-scoped record without it fails
    (`examples/quarantine-record.v1/invalid-row-scope-without-record-count.json`), so the K in the
    reconciliation identity is recorded in the artifact, not only in a log line.
  - Each entry of `rows[]` carries its own `reason_code`, which may differ from the record-level one —
    a file rejected as `SCHEMA_VALIDATION_FAILED` can hold rows failing for `TYPE_MISMATCH` and
    `VALUE_OUT_OF_RANGE` (`examples/quarantine-record.v1/valid-row-level-validation-failures.json`).
  - `contracts/dataset-validated.v1.json` sets `data.record_count` `minimum: 0`, down from the
    superseded contract's `minimum: 1`. The K=N case above produces a zero-row silver write, which the
    old bound made unpublishable.

### R3.5 Partition silver by data date *(ADR 0001)*
- **Input:** `OneLakeBronzeWriter.cs:42` and `ProcessDatasetCommandHandler.cs:120`, which use processing
  time; the mapped timestamp field from the vendor mapping
- **Output:** the partition key derives from the records' own timestamp field. A file spanning two days
  produces two partitions; re-running the same file is idempotent and lands in the same partitions.
- **Accept:** processing a fixture whose rows span two data dates produces exactly two date partitions
  matching those dates, and re-running it changes neither the partition set nor the row counts —
  which fails today, because a re-run would write a new processing-date partition.

### R3.7 Assert the event type and validate inbound *(ADR 0002, 0004)*
- **Input:** `ProcessDatasetFunction.cs:36-38,190-203`, `contracts/dataset-available.v1.json`
- **Output:** inbound validated against the vendored contract before deserialisation; `type` asserted
  against `solar.pvdaq.dataset.available.v1`; unrecognised types dead-lettered with a distinct reason
- **Accept:** a message with type `some.other.event.v1` dead-letters instead of being processed —
  today it would be processed.
- **Vendoring:** add
  `"processing-func": ["dataset-available.v1.json", "dataset-validated.v1.json", "quarantine-record.v1.json"]`
  to `contracts/vendoring.json` and copy each file byte-for-byte into
  `services/processing-func/schemas/contracts/`. Until that entry exists the workspace drift check has
  nothing to compare for this service and passes vacuously.
- **Contract differences (target wins)** — `contracts/dataset-available.v1.json`:
  - **Validate the whole envelope, then deserialise.** The partial model at
    `ProcessDatasetFunction.cs:190-203` covers 6 of 22 fields, so the hand-rolled null checks at
    `:40-47` are not validation — a message missing `dataschema` or carrying a bad `traceparent` passes
    them. Run the vendored schema first; the null checks then become redundant.
  - **`type` is asserted by the contract itself** (`const`), so an unrecognised type fails validation
    before any type-specific branch runs. Keep the distinct dead-letter reason this task asks for, since
    a wrong type and a malformed envelope are different operator stories.
  - **Do not reject unknown `data` fields.** `data` is open (ADR 0002 rule 2): a minor version adding a
    field must still validate against this vendored copy, or the forward compatibility ADR 0002 promises
    for ADR 0006's `device_id` does not exist. Unknown **envelope** attributes must still fail.
  - The contract validator must not depend on optional `format` packages — each `format` is backed by an
    equivalent `pattern` precisely so .NET and Python validators agree.

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
- **Contract differences (target wins)** — `contracts/dataset-validated.v1.json`. The flat payload at
  `DatasetBronzeAvailablePublisher.cs:22-30` shares only two field names with the target:
  - **It becomes a CloudEvent.** `specversion`, `type`, `source`, `id`, `time`, `datacontenttype` and
    `dataschema` are all new and all required; `ServiceBusEventPublisher.cs:33-39` must send
    `application/cloudevents+json` instead of `application/json`. The current shape is kept as a
    regression fixture: `examples/dataset-validated.v1/invalid-flat-json-not-a-cloudevent.json`.
  - `source` is `const "/dataset-processing/pvdaq"` — a **new value**; the service publishes no `source`
    today. It is deliberately not layer-named (ADR 0002 rule 3).
  - `bronze_path` → `data.silver_path`, in blob form (R3.6). It names the dataset's **partition root**,
    not a single `data.parquet`: R3.5 can write several date partitions from one file, so a path to one
    file would be wrong as often as not.
  - `schema_version` moves from the payload into an **envelope attribute**, alongside a new required
    `source_vendor`, so both events carry one envelope shape.
  - `published_at` is **dropped**: the CloudEvents `time` attribute carries exactly that meaning, and two
    fields for one fact drift apart. It is rejected as an unknown envelope attribute
    (`examples/dataset-validated.v1/invalid-published-at-alongside-time.json`).
  - `traceparent` is **required** — the current message carries none, so this depends on R3.8.
  - `data.record_count` allows 0 (see R3.4).
  - `correlation_id` must be a lowercase canonical UUID. `ProcessDatasetFunction.cs:57-64` falls back to
    `Guid.NewGuid()` when the inbound value is absent, which satisfies the shape — but under R3.7 an
    envelope with no `correlation_id` fails inbound validation first, so that fallback becomes
    unreachable rather than load-bearing.

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
