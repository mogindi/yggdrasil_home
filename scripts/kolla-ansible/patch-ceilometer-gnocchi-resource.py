#!/usr/bin/env python3
"""Apply the Ceilometer telemetry compatibility patches used by Yggdrasil."""

from pathlib import Path


GNOCCHI_MARKER = "_normalize_resource_attributes"
GNOCCHI_METHOD = '''    @staticmethod
    def _normalize_resource_attributes(resource):
        """Normalize values whose Gnocchi resource fields are strings."""
        if resource.get("flavor_id") is not None:
            resource["flavor_id"] = str(resource["flavor_id"])
        return resource

'''
RGW_MARKER = "_treat_empty_admin_response_as_empty_account"
RGW_METHOD = '''        # Some RGW tenants do not have an account record yet. RGW
        # returns an empty 200 response for those tenants, which is a valid
        # zero-usage result rather than malformed JSON.
        # _treat_empty_admin_response_as_empty_account
        if not r.content.strip():
            return []

'''


def patch_gnocchi():
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
    if GNOCCHI_MARKER in source:
        return

    needle = "    def _create_resource(self, resource_type, resource):\n"
    if source.count(needle) != 1:
        raise SystemExit("unexpected Ceilometer publisher layout")
    if source.count("resource = rd.event_attributes(event)") != 3:
        raise SystemExit("unexpected event attribute layout")
    if source.count("rd.sample_attributes(sample)") != 1:
        raise SystemExit("unexpected sample attribute layout")

    source = source.replace(needle, GNOCCHI_METHOD + needle, 1)
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


def patch_rgw_client():
    candidates = sorted(
        {
            path.resolve()
            for path in Path("/var/lib/kolla/venv/lib").glob(
                "python*/site-packages/ceilometer/objectstore/rgw_client.py"
            )
        }
    )
    if len(candidates) != 1:
        raise SystemExit(f"expected one Ceilometer RGW client, found {candidates}")

    target = candidates[0]
    source = target.read_text()
    if RGW_MARKER in source:
        return

    needle = "        return r.json()\n"
    if source.count(needle) != 1:
        raise SystemExit("unexpected Ceilometer RGW client layout")

    source = source.replace(needle, RGW_METHOD + needle, 1)
    compile(source, str(target), "exec")
    target.write_text(source)
    print(f"patched {target}")


def main():
    patch_gnocchi()
    patch_rgw_client()


if __name__ == "__main__":
    main()
