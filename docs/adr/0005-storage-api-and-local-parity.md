# ADR 0005 — Storage API and local/cloud parity: Blob everywhere

- **Status:** Accepted
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** `ingestion-func`, `processing-func`, IaC

## Context

Both services talk to storage through the **Data Lake (DFS) client in cloud and the Blob client (or a
blob endpoint) locally** — but neither uses a single feature that requires the DFS API.

`ingestion-func` maintains **two genuinely different code paths**:
- cloud: `DataLakeServiceClient` + `DefaultAzureCredential` (`src/adls_store.py:83-87,121-126`)
- local: `BlobServiceClient.from_connection_string` writing append blobs
  (`src/adls_store.py:69-71,117-118,202`), selected by `STORAGE_EMULATOR == "true"`
  (`function_app.py:115-119`)

The DFS path is therefore **never exercised by any local test** — local and cloud run different code.

`processing-func` uses `DataLakeServiceClient` unconditionally, but locally points it at
`http://127.0.0.1:10000/devstoreaccount1` (`local.settings.json:10`) — **Azurite's blob port**. This
works only because the sole operations used are flat file get/put, which are wire-compatible between
the Blob and DFS REST surfaces. The first genuine DFS-only call would fail locally.

**ADLS-only features actually used: none.** Grepping both repos for atomic rename
(`rename_directory`/`RenameAsync`), directory operations, and ACLs (`set_access_control`/
`SetAccessControlAsync`) returns nothing. The entire DataLake surface exercised is:
- `GetFileClient().ReadAsync()` (`AdlsDatasetReader.cs:32-41`)
- `GetFileClient().DeleteIfExistsAsync()/UploadAsync()` (`OneLakeBronzeWriter.cs:45-56`)
- `create_file` / `append_data` / `flush_data` (`src/adls_store.py:136,151-158`) — chunked write, for
  which `ingestion-func` **already has a working Blob implementation** on its local path

All are one-to-one equivalent to `BlobClient` calls. The `/`-separated "directories" are just blob
names.

On the destination: the code carries Fabric/OneLake branding (`OneLakeBronzeWriter`,
`ONELAKE_ENDPOINT`, `README.md:5`, `main.parameters.json:15`), and OneLake is DFS-only with no blob
endpoint. However the Fabric parameters are still unfilled placeholders
(`https://onelake.dfs.fabric.microsoft.com/{workspace-id}/{lakehouse-id}`), and the Contract Owner has
confirmed **Fabric is aspirational, not a commitment**. Note also that the storage account in
`infrastructure/modules/storage.bicep` is *only* `AzureWebJobsStorage` for the Functions runtime
(`function-app.bicep:27`) — there is no data-lake account in IaC at all.

## Options

**A. Blob API everywhere, one code path.**
Azurite emulates Blob faithfully, so local tests exercise production code; no real Azure account in
CI; the dual-path bug class disappears. Forfeits ACLs and atomic rename without a rewrite. Naively this
also closes the OneLake door — the Decision below removes that drawback by keeping the *account* ADLS
Gen2 while the *code* stays Blob-only.

**B. Data Lake API everywhere, tested against a real dev ADLS Gen2 account.**
Keeps Fabric/OneLake viable and removes ingestion's local branch. Costs a paid account, credentials in
CI, slower tests, and no offline development.

**C. A storage port with two adapters, chosen by configuration.**
Defers the decision; costs two adapters, two test matrices, and a CI rule banning ADLS-only APIs to
keep them interchangeable.

## Decision

**Option A — Blob API in code, on an HNS-enabled (ADLS Gen2) account.** The decision has two halves,
and they are deliberately independent:

1. **Code uses the Blob API exclusively.** Justified directly by the inventory: zero ADLS-only features
   are used, `ingestion-func` already ships a working Blob implementation, `processing-func` uses only
   read/upload/delete, and locally the DataLake client is *already* talking to a blob endpoint.
2. **The cloud data account is provisioned with `isHnsEnabled: true`** — i.e. it *is* ADLS Gen2 — even
   though no code uses a DFS-only feature.

These are compatible because an HNS-enabled account keeps its **Blob endpoint fully available**: ADLS
Gen2 exposes both the Blob and DFS surfaces over the same data. So one code path (Blob, Azurite-testable)
runs against storage that is nonetheless a real data lake.

The reason for half 2 is optionality at zero code cost. A Fabric/OneLake **shortcut** can reference an
external ADLS Gen2 account directly, so a Lakehouse can be pointed at `bronze` / `silver` later
**without changing a line of application code** — but only if the account has a hierarchical namespace,
which cannot be enabled retroactively. Provisioning it now costs nothing and keeps that door open;
provisioning it flat would close it permanently.

This decision is **coupled to ADR 0001**: layer publication must never be implemented as
write-to-temp-then-rename, because the Blob API the code uses has no atomic directory rename. Writes go
directly to their final path and the **event** is the publication signal — already the implemented
behaviour (`OneLakeBronzeWriter.cs:45-56`).

## Consequences

**Positive**
- One code path per operation; the code under test is the code that runs in production.
- No paid Azure resources required for integration tests; the emulator stack is sufficient.
- `ingestion-func` sheds an entire duplicated storage implementation.

**Negative / required work**
- `ingestion-func`: delete the DataLake branch (`src/adls_store.py:24,83-87,121-126,136,151-158`) and
  promote the existing Blob path; collapse `STORAGE_EMULATOR` (`function_app.py:115-119`) into a single
  connection-string/credential switch; add the missing local branch to `src/idempotency_store.py:55`,
  which always uses `DefaultAzureCredential` today.
- `processing-func`: drop `DataLakeServiceClient` from `Program.cs:94-113` and reuse the already
  registered `BlobServiceClient`; `AdlsDatasetReader.cs:32-41` → `DownloadStreamingAsync`;
  `OneLakeBronzeWriter.cs:45-56` → `UploadAsync`; replace the brittle exact-equality connection-string
  test at `Program.cs:107-109`.
- Rename OneLake-flavoured symbols (`OneLakeBronzeWriter`, `ONELAKE_ENDPOINT`) to storage-neutral names,
  so the code names a layer rather than a specific lake product.
- `storage_path` becomes `https://{account}.blob.core.windows.net/{container}/{path}` rather than
  `abfss://`; `AdlsDatasetReader.ParseAdlsUri:49-70` already handles the blob form.
- IaC must gain a data-lake storage account **with `isHnsEnabled: true`**, the three containers from
  ADR 0001, the queues the code actually uses, and the missing app settings — the deployed app cannot
  start as written today. Tracked as task R4 in `docs/contract-migration.md`.
- ACLs and atomic rename exist on the account but are **deliberately unused by code**. Adopting either
  later is a code change within this decision, not a reversal of it — the account already supports them.

**Forward compatibility (ADR 0006)**
- Fingerprint resolution and quarantine writes are storage-API-agnostic plain object puts, and need no
  rename, no directory semantics and no ACLs. This decision does not constrain ADR 0006.
