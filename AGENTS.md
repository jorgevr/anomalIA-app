# AGENTS.md — anomalia-platform (workspace root)

Fleet design/rationale: see [docs/agent-fleet.md](docs/agent-fleet.md). This file is
the operational summary agents actually load.

## 1. Repo map

- `services/ingestion-func/` (submodule) — Python 3.11, Azure Functions v4. Pulls PVDAQ
  CSVs from the OEDI S3 data lake and lands them in ADLS Gen2 `bronze`.
- `services/processing-func/` (submodule) — .NET 10 Isolated Worker (`DatasetProcessingFunction.slnx`).
  Consumes dataset-available events and processes bronze datasets.
- `contracts/` — **authoritative** message and at-rest record schemas (ADR 0004), their
  examples, the `vendoring.json` manifest and `README.md`. Contract Owner only.
- `scripts/` — root checks: `check-contracts.py` (schemas, examples, vendoring drift),
  `verify-queue-topology.py` (queue names vs the emulator config). Both run in
  `.github/workflows/contracts.yml`.
- `infrastructure/` — local storage (`storage/azurite`) and docker assets for the emulator stack.
- `servicebus-emulator/` — config for the Azure Service Bus emulator (`config.json`).
- `docker-compose.yml` — local stack: Azurite, SQL Server (Service Bus emulator dep),
  Service Bus emulator, both functions.
- `data/` — sample data. `notebooks/` — exploration. `docs/` — fleet design + diagrams.

## 2. Data flow

`ingestion-func` (`historical_dispatcher` timer → `historical_worker`) streams OEDI CSVs
into ADLS Gen2 `bronze`, then emits a CloudEvent (`solar.pvdaq.dataset.available`) onto
Service Bus queue `raw-energy-events`. `processing-func` (`ProcessDatasetFunction`, Service
Bus trigger) consumes it and runs the processing pipeline. Diagram:
[docs/ingestion-processing-flow.excalidraw](docs/ingestion-processing-flow.excalidraw).

## 3. Boundaries

- An agent edits only the repo (submodule) it was assigned.
- Shared contracts — root `contracts/` (authoritative), each service's vendored
  `schemas/contracts/`, `services/*/schemas/`, message/CloudEvents envelope shapes,
  `.env.example`, root `docker-compose.yml` — change only via the Contract Owner role.
- A service agent never edits `contracts/` and never hand-edits its vendored copy: it
  re-copies from the root and runs `python scripts/check-contracts.py`. The copies must be
  byte-identical, and only the workspace repo's CI can prove it (ADR 0004 rule 4).
- Specs live in each service's own `specs/` (spec-kit), never at the workspace root.
- Agents started at the workspace root edit only root files unless explicitly assigned a service.

## 4. Commands

Not duplicated here — see each service's own `CLAUDE.md`:
[services/ingestion-func/CLAUDE.md](services/ingestion-func/CLAUDE.md),
[services/processing-func/CLAUDE.md](services/processing-func/CLAUDE.md).

## 5. Git rules

- One agent = one worktree = one branch (`git worktree add` inside the submodule for
  parallel agents — never share a working tree).
- Commit order for cross-repo work: submodule first, then the workspace repo pointer.
- Never commit `.env`, `local.settings.json`, or Azurite runtime data
  (`azurite-data/`, `__azurite_db_*`, `AzuriteConfig`, `.azurite/`).
- Run read-only git commands with `--no-optional-locks`.

## 6. Definition of done (every task, every repo)

Learned from real failures in this repo; a task is not done until all that apply hold:

- **Configuration changes** need one test with *only* the local settings set
  (connection strings, no cloud keys) and one with *only* the cloud settings set
  (account URL / namespace, no connection strings), for every config loader touched.
- **Configuration is validated at host startup**, not lazily on first invocation — a bad
  setting must make the container unhealthy, not fail the first run hours later.
- **Runtime behaviour is proven on the compose stack from a clean state**
  (`docker compose down -v` then `up -d --build`), with all services healthy. Mocked
  "integration" tests do not count as proof.
- **Anything read at runtime is inside the image.** Code never loads files from `specs/`,
  `docs/` or other paths the Dockerfile does not copy.
- **Deterministic failures are not retried.** Missing files, invalid config, unknown event
  types or schema violations dead-letter or quarantine on the first attempt; only transient
  errors (network, throttling, 5xx) go back to Service Bus for redelivery. See "Failure
  classification policy" below.
- **Verification runs use one site / one file**, not the full historical backlog.

### Failure classification policy

- **Message-deterministic failures dead-letter on the first delivery attempt.** A body that
  won't parse, an envelope or payload that fails schema validation, or a source object that
  cannot be read (missing, wrong type, malformed) will fail identically on every redelivery —
  retrying it only burns the redelivery budget that exists for transient conditions and delays
  the operator signal. Route these straight to a dead-letter destination without looping through
  Service Bus first.
- **Environmental, transient or unknown failures redeliver, bounded by an explicit
  `maxDeliveryCount`, and alert.** Network errors, throttling, 5xx responses and anything not
  positively classified as message-deterministic get the broker's normal retry path — but every
  queue must declare its own `maxDeliveryCount` explicitly (never rely on an implicit or
  emulator default; `scripts/verify-queue-topology.py` asserts this), and exhausting it must
  page/alert, not silently pile up in a DLQ nobody watches. Do not assume the emulator enforces
  this correctly on its own — a schema-load bug on `pvdaq-historical-work` once produced
  ~8,000 redeliveries of a single message despite the queue declaring
  `MaxDeliveryCount: 10` at the time; the code-level classification above is the real backstop,
  the broker setting is a second line of defense, not the only one.
- **Never complete a message whose handling failed without a durable record of the failure.**
  A message is either fully processed, dead-lettered/quarantined with a reason, or left
  uncompleted for redelivery — there is no fourth path where a failure is swallowed and the
  message is acknowledged anyway. (See ADR 0001 "Failure routing"; this is the same
  no-silent-drops identity `docs/agent-fleet.md` §5 requires at the row level.)
- **Exception classifiers must be tested against the SDK's real class hierarchy, not a
  hand-written list.** A `catch`/`except` that names a fixed set of exception types drifts
  silently when the SDK adds, renames or reclassifies one — the safety property the classifier
  exists for erodes without any test failing. Assert the classification against the actual SDK
  types (`isinstance`/`is`-a checks against the real exception hierarchy, or a test that walks
  it), not a parallel hand-maintained list of type names.
