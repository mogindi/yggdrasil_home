#!/usr/bin/env python3
"""Normalize Ceilometer event/sample attributes for Gnocchi resource types."""

from pathlib import Path


MARKER = "_normalize_resource_attributes"
METHOD = '''    @staticmethod
    def _normalize_resource_attributes(resource):
        """Normalize values whose Gnocchi resource fields are strings."""
        if resource.get("flavor_id") is not None:
            resource["flavor_id"] = str(resource["flavor_id"])
        return resource

'''


def main():
    candidates = sorted(
        {
            path.resolve()
            for path in Path("/var/lib/kolla/venv/lib").glob(
                "python*/site-packages/ceilometer/publisher/gnocchi.py"
            )
        }
    )
    if len(candidates) != 1:
        raise SystemExit(f"expected one Ceilometer publisher, found {candidates}")

    target = candidates[0]
    source = target.read_text()
    if MARKER in source:
        return

    needle = "    def _create_resource(self, resource_type, resource):\n"
    if source.count(needle) != 1:
        raise SystemExit("unexpected Ceilometer publisher layout")
    if source.count("resource = rd.event_attributes(event)") != 3:
        raise SystemExit("unexpected event attribute layout")
    if source.count("rd.sample_attributes(sample)") != 1:
        raise SystemExit("unexpected sample attribute layout")

    source = source.replace(needle, METHOD + needle, 1)
    source = source.replace(
        "resource = rd.event_attributes(event)",
        "resource = self._normalize_resource_attributes("
        "rd.event_attributes(event))",
    )
    source = source.replace(
        "rd.sample_attributes(sample)",
        "self._normalize_resource_attributes(rd.sample_attributes(sample))",
    )

    compile(source, str(target), "exec")
    target.write_text(source)
    print(f"patched {target}")


if __name__ == "__main__":
    main()
