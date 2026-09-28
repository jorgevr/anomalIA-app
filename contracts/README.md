# `contracts/` — the authoritative contract registry

These files are the single source of truth for every message and record that crosses a service
boundary (ADR 0004). They describe the **target** state decided by ADRs 0001–0005, which the services
do not implement yet; every known difference is listed under its owning task in
[`docs/contract-migration.md`](../docs/contract-migration.md).

| File | Kind | Producer | Consumer |
| :--- | :--- | :--- | :--- |
| `dataset-available.v1.json` | message — CloudEvent on `raw-energy-events` | `ingestion-func` | `processing-func` |
| `dataset-validated.v1.json` | message — CloudEvent on `dataset-validated` | `processing-func` | downstream (none yet) |
| `quarantine-record.v1.json` | at-rest record — `reason.json` in `quarantine` | both services | operators, re-drive tooling |
| `metadata-file.v1.json` | at-rest record — `metadata.json` in `bronze` | `ingestion-func` | silver layer, operators |

## Ownership

Owned by the **Contract Owner** role (`docs/agent-fleet.md` §3.4, `AGENTS.md` §3). A service agent
never edits these files, and never edits its own vendored copy by hand — it re-vendors. Changes are
arbitrated here first, then flow outward.

Two things deliberately live **outside** this folder:

- **Vendor field mappings** (`processing-func`'s `PVDAQ-v1.json`) are runtime configuration, not
  contracts. They belong in the schema-registry container and version on their own cadence
  (ADR 0004 rule 1).
- **The historical work item** (dispatcher → worker) is `ingestion-func`-internal. It never crosses a
  service boundary, so it stays in that repo.

## Versioning (ADR 0002)

```
{domain}.{vendor}.{entity}.{action}.v{major}
```

- **Major = breaking = a new type string and a new file.** Removing a field, narrowing a type or
  changing a meaning forks `…v1.json` into `…v2.json`. Both may exist while consumers migrate.
- **Minor = additive only, same type string.** The exact revision is identified by the CloudEvents
  `dataschema` attribute, which equals the contract's `$id`.
- Consequently the message envelopes are **closed** (`additionalProperties: false`) while their `data`
  member is **open**: an unknown envelope attribute is a producer bug, but an added `data` field is a
  minor version that a consumer holding an older vendored copy must still accept. The `device_id` of
  ADR 0006 arrives that way.
- Event names never reference a storage layer, so a layer rename cannot force an event rename
  (ADR 0002 rule 3).
- Every `format` that an example relies on is backed by an equivalent `pattern`, so validation is
  deterministic in environments where format assertion is switched off or a format package is absent.

## Examples

`examples/<contract>/valid-*.json` must validate; `examples/<contract>/invalid-*.json` must fail, and
each one breaks a **different** rule — the filename names it. `scripts/check-contracts.py` enforces
both directions plus the distinctness, so the examples are executable documentation rather than
decoration. Several invalid examples are the *current* production shape (an unversioned `type`, an
`abfss://` path, a flat non-CloudEvent body), which is what makes the migration testable.

One limit worth stating: a `traceparent` synthesised from random UUIDs (ADR 0003) is
indistinguishable from a real one by schema alone. The contract rejects only the degenerate all-zero
form; that a trace ID matches the emitting span is asserted by tests in the services (R2.4, R3.8).

## How to vendor

1. Add the contract to `contracts/vendoring.json` under the consuming service:

   ```json
   {
     "ingestion-func": ["dataset-available.v1.json"],
     "processing-func": ["dataset-available.v1.json", "dataset-validated.v1.json"]
   }
   ```

2. Copy the file **byte-for-byte** into `services/<service>/schemas/contracts/<file>`. Byte-identity
   includes newlines, so keep the service repo's `.gitattributes` aligned with this one (all three
   repos use `* text=auto` today); the check names that case explicitly if it ever diverges.
3. Validate real messages against the vendored copy at runtime, not only in tests
   (ADR 0004 rule 3) — outbound before send, inbound before deserialising.
4. Run `python scripts/check-contracts.py` from the workspace root.

Drift detection lives in the workspace repo's CI only (`.github/workflows/contracts.yml`), because it
is the one place that sees `contracts/` and both submodules at once (ADR 0004 rule 4). A service
branch can therefore be green while its vendored copy is stale; the workspace CI is what catches that.
