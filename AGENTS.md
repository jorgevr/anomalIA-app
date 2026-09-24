# AGENTS.md — anomalia-platform (workspace root)

Fleet design/rationale: see [docs/agent-fleet.md](docs/agent-fleet.md). This file is
the operational summary agents actually load.

## 1. Repo map

- `services/ingestion-func/` (submodule) — Python 3.11, Azure Functions v4. Pulls PVDAQ
  CSVs from the OEDI S3 data lake and lands them in ADLS Gen2 `bronze`.
- `services/processing-func/` (submodule) — .NET 10 Isolated Worker (`DatasetProcessingFunction.slnx`).
  Consumes dataset-available events and processes bronze datasets.
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
- Shared contracts — `services/*/schemas/`, message/CloudEvents envelope shapes,
  `.env.example`, root `docker-compose.yml` — change only via the Contract Owner role.
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
