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

Also asserts every queue declared in the emulator config sets an explicit
``MaxDeliveryCount`` no greater than 10. The emulator's enforcement of this
property is not trustworthy on its own — a schema-load bug on
`pvdaq-historical-work` once produced ~8,000 redeliveries of a single
message despite the queue declaring MaxDeliveryCount: 10 at the time (see
AGENTS.md §6 "Failure classification policy") — so the real backstop is
code-level dead-lettering of deterministic failures on first delivery; this
check only guards against a queue silently reverting to the emulator's
default (which does not bound redelivery at all) or an unreasonably large
value that would let a poison message loop for a long time before the
broker-level safety net engages.
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


MAX_DELIVERY_COUNT_CEILING = 10


def emulator_queue_max_delivery_counts(config_path: Path) -> dict[str, int | None]:
    """Map queue name -> MaxDeliveryCount, or None if the property is absent."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    counts: dict[str, int | None] = {}
    for namespace in config.get("UserConfig", {}).get("Namespaces", []):
        for queue in namespace.get("Queues", []):
            counts[queue["Name"]] = queue.get("Properties", {}).get("MaxDeliveryCount")
    return counts


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = ROOT / ".env.example"

    config_path = ROOT / "servicebus-emulator" / "config.json"

    configured = configured_queue_names(env_path)
    configured_names = set(configured.values())
    emulator_names = emulator_queue_names(config_path)
    max_delivery_counts = emulator_queue_max_delivery_counts(config_path)

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
    print("MaxDeliveryCount per queue:")
    for name in sorted(max_delivery_counts):
        print(f"  {name:<28} = {max_delivery_counts[name]}")
    print()

    ok = True
    if missing_from_emulator:
        ok = False
        print(f"MISMATCH: configured but not declared in emulator: {sorted(missing_from_emulator)}")
    if unused_in_emulator:
        print(f"NOTE: declared in emulator but not read by either service's config: {sorted(unused_in_emulator)}")

    unset_max_delivery = sorted(
        name for name, count in max_delivery_counts.items() if count is None
    )
    if unset_max_delivery:
        ok = False
        print(f"MISMATCH: MaxDeliveryCount not set (relies on emulator default): {unset_max_delivery}")

    too_high = sorted(
        name
        for name, count in max_delivery_counts.items()
        if count is not None and count > MAX_DELIVERY_COUNT_CEILING
    )
    if too_high:
        ok = False
        print(
            f"MISMATCH: MaxDeliveryCount exceeds the {MAX_DELIVERY_COUNT_CEILING} ceiling: "
            f"{[(n, max_delivery_counts[n]) for n in too_high]}"
        )

    if ok:
        print(
            "OK: every queue name read from configuration exists in the emulator config, "
            f"and every queue declares MaxDeliveryCount <= {MAX_DELIVERY_COUNT_CEILING}."
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
