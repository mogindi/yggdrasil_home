#!/usr/bin/env python3
"""Add the custom Zun capsule CPU meter to Ceilometer's Gnocchi mapping."""

from pathlib import Path


MARKER = "# yggdrasil-zun-capsule-cpu-v1"
DEFINITION = '''
  - resource_type: generic
    metrics:
      yggdrasil.zun.capsule.cpu_util:
        archive_policy_name: ceilometer-high
    attributes:
      original_resource_id:
        fields: resource_metadata.original_resource_id
'''


def patch_resource_definitions():
    candidates = sorted(
        {
            path.resolve()
            for path in Path("/var/lib/kolla/venv/lib").glob(
                "python*/site-packages/ceilometer/publisher/data/gnocchi_resources.yaml"
            )
        }
    )
    if len(candidates) != 1:
        raise SystemExit(f"expected one Gnocchi resource definition, found {candidates}")

    target = candidates[0]
    source = target.read_text()
    if MARKER in source:
        return
    if "yggdrasil.zun.capsule.cpu_util" in source:
        raise SystemExit("Zun capsule CPU meter already exists without its marker")
    if "resources:" not in source:
        raise SystemExit("unexpected Ceilometer Gnocchi resource definition layout")

    target.write_text(source.rstrip() + "\n" + MARKER + DEFINITION)
    print(f"added Zun capsule CPU meter to {target}")


if __name__ == "__main__":
    patch_resource_definitions()
