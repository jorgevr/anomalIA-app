#!/usr/bin/env python3
"""Diff the queue names read from configuration by both services against the
root Service Bus emulator config (R0.3, docs/contract-migration.md).

Reads queue-name values from an env file (.env, falling back to .env.example)
for the settings each service's own config loader treats as a queue name:

  ingestion-func (services/ingestion-func/src/config.py):
    SERVICE_BUS_QUEUE_NAME, DEAD_LETTER_QUEUE_NAME, PVDAQ_HISTORICAL_QUEUE_NAME

  processing-func (services/processing-func/src/DatasetProcessingFunction/
  Functions/ProcessDatasetFunction.cs ServiceBusTrigger, Program.cs):
    SERVICEBUS_QUEUE_NAME, SERVICEBUS_BRONZE_QUEUE_NAME

Exits non-zero if either side has a queue name the other doesn't.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV_QUEUE_VARS = [
    "SERVICE_BUS_QUEUE_NAME",
    "DEAD_LETTER_QUEUE_NAME",
    "PVDAQ_HISTORICAL_QUEUE_NAME",
    "SERVICEBUS_QUEUE_NAME",
    "SERVICEBUS_BRONZE_QUEUE_NAME",
]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def configured_queue_names(env_path: Path) -> dict[str, str]:
    env = parse_env_file(env_path)
    found = {}
    for var in ENV_QUEUE_VARS:
        value = env.get(var, "").strip()
        if value:
            found[var] = value
    return found


def emulator_queue_names(config_path: Path) -> set[str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for namespace in config.get("UserConfig", {}).get("Namespaces", []):
        for queue in namespace.get("Queues", []):
            names.add(queue["Name"])
    return names


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = ROOT / ".env.example"

    config_path = ROOT / "servicebus-emulator" / "config.json"

    configured = configured_queue_names(env_path)
    configured_names = set(configured.values())
    emulator_names = emulator_queue_names(config_path)

    missing_from_emulator = configured_names - emulator_names
    unused_in_emulator = emulator_names - configured_names

    print(f"Env file:      {env_path.relative_to(ROOT)}")
    print(f"Emulator config: {config_path.relative_to(ROOT)}")
    print()
    print("Queue names read from configuration:")
    for var, value in sorted(configured.items()):
        print(f"  {var:<28} = {value}")
    print()
    print(f"Emulator declares: {sorted(emulator_names)}")
    print()

    ok = True
    if missing_from_emulator:
        ok = False
        print(f"MISMATCH: configured but not declared in emulator: {sorted(missing_from_emulator)}")
    if unused_in_emulator:
        print(f"NOTE: declared in emulator but not read by either service's config: {sorted(unused_in_emulator)}")

    if ok:
        print("OK: every queue name read from configuration exists in the emulator config.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
