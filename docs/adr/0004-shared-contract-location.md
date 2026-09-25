# ADR 0004 — Where shared schemas and contracts live

- **Status:** Accepted
- **Date:** 2026-09-24
- **Owner:** Contract Owner (`docs/agent-fleet.md` §3.4)
- **Affects:** workspace root, `ingestion-func`, `processing-func`

## Context

Contracts are scattered across two repos and two *kinds* of artifact are conflated under one name.

- `ingestion-func` holds a runtime record schema at `schemas/pvdaq-v1.json`, loaded at import and
  enforced per record (`src/schema_validator.py:14-16`, `src/record_pipeline.py:64`).
- `processing-func` holds `schemas/PVDAQ-v1.json` — **not a JSON Schema at all**, but a vendor field
  *mapping* config (`ColumnMappings`, `RequiredFields`, `UnitConversion`,
  `schemas/PVDAQ-v1.json:1-97`), fetched at runtime from a blob container by the key
  `{vendorId}-{schemaVersion}.json` (`BlobSchemaRegistry.cs:46-54`).
  Two files whose names differ only by case, meaning entirely different things.
- The **cross-service** contract — `specs/002-pvdaq-historical-ingestion/contracts/dataset-event.json` —
  is **never loaded by code or by tests**. It has already rotted: the producer emits `data.version`
  while the contract declares `additionalProperties: false` (`dataset-event.json:119`).
- Other contracts live under `specs/001-*/contracts/` and are referenced only by tests, or by nothing.

The root cause is that **living contracts are stored inside frozen spec-kit artifacts**. `specs/` is a
historical record of a feature; a contract is a current obligation. Storing one inside the other
guarantees drift.

A structural constraint shapes the options: the two services are **independent git repositories**
(submodules, `.gitmodules:2,6`). A folder at the workspace root is *not* reachable from a submodule's
own build or CI without extra plumbing.

## Options

**A. Root `contracts/` as the single source, consumed directly.**
One copy, no drift. But a submodule cannot reference a parent-repo path at build or test time, so this
does not actually work without vendoring or packaging.

**B. Contracts in their own repo, added as a submodule or published as pip/NuGet packages.**
True single source with versioned releases; costs a third repository, a release process, and a version
bump dance on every change.

**C. Root `contracts/` authoritative + vendored copies + a CI drift check.**
Each service vendors the subset it needs into `schemas/contracts/`; a drift check asserts byte-identity
with the root copy, and each service validates real messages against its vendored copy. No new repo
plumbing; drift is *caught* rather than structurally prevented.

## Decision

**Option C.** `contracts/` at the workspace root is authoritative and Contract-Owner-owned, consistent
with `docs/agent-fleet.md` §3.4 and `AGENTS.md` §3.

```
contracts/
  dataset-available.v1.json     # ingestion -> processing
  dataset-validated.v1.json     # processing -> downstream
  work-item.v1.json             # ingestion internal (dispatcher -> worker)
  dead-letter.v1.json           # both
  metadata-file.v1.json         # bronze sidecar
```

Three rules follow:

1. **Separate message contracts from vendor mappings.** A *message contract* is a JSON Schema
   describing a cross-service message and lives in `contracts/`, owned by the Contract Owner. A
   *vendor field mapping* (`PVDAQ-v1.json`) is `processing-func` runtime configuration and belongs in
   the schema-registry container — **not** in `contracts/`. They version independently.
2. **Contracts leave `specs/`.** `specs/*/contracts/*.json` copies are deleted or reduced to pointers.
   Specs stay frozen; contracts stay live.
3. **Validate at runtime, not only in tests.** `ingestion-func` validates its outbound envelope before
   sending (today it validates only *inbound* work items, `function_app.py:440`); `processing-func`
   validates inbound *before* deserialising into its partial model.
4. **The drift check runs in the workspace repo's CI, not in the service repos.** The workspace repo is
   the only place that checks out `contracts/` *and* both submodules, so it is the only place that can
   compare them. Putting the check in a service repo would require that repo to fetch a contract copy it
   cannot see — reintroducing the plumbing option C exists to avoid. The services keep **runtime
   validation** against their vendored copy (rule 3); the workspace keeps **drift detection**.

## Consequences

**Positive**
- One authoritative place for the Contract Owner to arbitrate, matching the role definition.
- The `dataset-available` contract becomes enforced rather than decorative, so violations like
  `data.version` fail CI instead of shipping.
- Vendor mappings can churn per device (ADR 0006) without touching message contracts.
- Drift detection lives in one place, so it cannot be half-configured across two repos or silently
  skipped in the one that happens to lag.

**Negative / required work**
- Two copies of each contract exist by construction; correctness depends on the workspace drift check
  actually running, and on submodule pointers being current when it does.
- Each service gains a vendoring step and a runtime validation call; the workspace gains the drift check.
- A service repo's CI alone cannot prove its vendored copy is current — that guarantee exists only at
  the workspace level, so a service branch can be green while out of step until the workspace CI runs.
- The `specs/*/contracts/` copies must be removed, which touches spec-kit history.

**Forward compatibility (ADR 0006)**
- Mappings are explicitly *not* message contracts, so a per-device mapping can be versioned and
  published on its own cadence with no cross-service coordination.
- The registry lookup key must generalise from the flat `{vendorId}-{schemaVersion}.json`
  (`BlobSchemaRegistry.cs:46`) to a precedence chain (device → site → vendor default). That is a
  `processing-func`-internal change with **no** message-contract impact — which is exactly the property
  this ADR is designed to give.
