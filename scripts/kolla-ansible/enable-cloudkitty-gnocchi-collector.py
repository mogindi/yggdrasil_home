#!/usr/bin/env python3
"""Idempotently enable CloudKitty's Gnocchi collector after Kolla deploys."""

from __future__ import annotations

import sys
import time

COLLECTOR_NAME = "gnocchi"


def collector_state(connection, state_url: str) -> bool:
    response = connection.session.get(
        state_url,
        params={"name": COLLECTOR_NAME},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("name") != COLLECTOR_NAME:
        raise RuntimeError("CloudKitty returned an invalid Gnocchi collector state")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise RuntimeError("CloudKitty returned no boolean collector state")
    return enabled


def reconcile(connection=None, sleep=time.sleep) -> bool:
    if connection is None:
        import openstack

        connection = openstack.connect()
    endpoint = connection.endpoint_for("rating")
    if not endpoint:
        raise RuntimeError("CloudKitty rating endpoint was not found")
    base_url = endpoint.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    state_url = f"{base_url}/v1/collector/states/"

    for attempt in range(30):
        try:
            if collector_state(connection, state_url):
                print("CloudKitty Gnocchi collector is already enabled")
                return False
            response = connection.session.put(
                state_url,
                json={"name": COLLECTOR_NAME, "enabled": True},
                timeout=30,
            )
            response.raise_for_status()
            if not collector_state(connection, state_url):
                raise RuntimeError("CloudKitty did not enable the Gnocchi collector")
            print("CloudKitty Gnocchi collector enabled and verified")
            return True
        except Exception:
            if attempt == 29:
                raise
            sleep(2)
    raise RuntimeError("CloudKitty rating API did not become ready")


def main() -> int:
    try:
        reconcile()
    except Exception as exc:
        print(f"ERROR: Could not reconcile CloudKitty Gnocchi collector: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
