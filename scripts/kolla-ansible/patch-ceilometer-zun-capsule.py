#!/usr/bin/env python3
"""Add the custom Zun capsule CPU meter to Ceilometer's Gnocchi mapping."""

from pathlib import Path


MARKER = "# yggdrasil-zun-capsule-cpu-v3"
PREVIOUS_MARKER = "# yggdrasil-zun-capsule-cpu-v2"
LEGACY_MARKER = "# yggdrasil-zun-capsule-cpu-v1"
DEFINITION = '''
  - resource_type: yggdrasil_zun_capsule
    metrics:
      yggdrasil.zun.capsule.cpu_util:
        archive_policy_name: ceilometer-high
    attributes:
      yggdrasil_senlin_resource_id:
        fields: resource_metadata.yggdrasil_senlin_resource_id
'''
PREVIOUS_DEFINITION = '''
  - resource_type: yggdrasil_zun_capsule
    metrics:
      yggdrasil.zun.capsule.cpu_util:
        archive_policy_name: ceilometer-high
    attributes:
      original_resource_id:
        fields: resource_metadata.original_resource_id
'''
LEGACY_DEFINITION = '''
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
    previous_definition = PREVIOUS_MARKER + PREVIOUS_DEFINITION
    if PREVIOUS_MARKER in source:
        if source.count(previous_definition) != 1:
            raise SystemExit("unexpected previous Zun capsule meter mapping")
        source = source.replace(previous_definition, MARKER + DEFINITION, 1)
        target.write_text(source)
        print(f"updated Zun capsule CPU meter in {target}")
        return
    if LEGACY_MARKER in source:
        legacy_definition = LEGACY_MARKER + LEGACY_DEFINITION
        if source.count(legacy_definition) != 1:
            raise SystemExit("unexpected legacy Zun capsule meter mapping")
        source = source.replace(legacy_definition, MARKER + DEFINITION, 1)
        target.write_text(source)
        print(f"updated Zun capsule CPU meter in {target}")
        return
    if "yggdrasil.zun.capsule.cpu_util" in source:
        raise SystemExit("Zun capsule CPU meter already exists without its marker")
    if "resources:" not in source:
        raise SystemExit("unexpected Ceilometer Gnocchi resource definition layout")

    target.write_text(source.rstrip() + "\n" + MARKER + DEFINITION)
    print(f"added Zun capsule CPU meter to {target}")


if __name__ == "__main__":
    patch_resource_definitions()
