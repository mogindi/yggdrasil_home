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
RESOURCE_TYPE_OPERATION_MARKER = (
    "create yggdrasil_zun_capsule for Zun Capsule autoscaling"
)
LEGACY_RESOURCE_TYPE_OPERATION = '''    {"desc": "add original_resource_id to generic for Zun Capsule autoscaling",
     "type": "update_attribute_type",
     "resource_type": "generic",
     "data": [{
         "op": "add",
         "path": "/attributes/original_resource_id",
         "value": {"type": "string", "min_length": 0,
                   "max_length": 255, "required": False}
     }]},
'''
PREVIOUS_RESOURCE_TYPE_OPERATION = '''    {"desc": "create yggdrasil_zun_capsule for Zun Capsule autoscaling",
     "type": "create_resource_type",
     "resource_type": "yggdrasil_zun_capsule",
     "data": [{
         "attributes": {
             "original_resource_id": {
                 "type": "string", "min_length": 0,
                 "max_length": 255, "required": False
             }
         }
     }]},
'''
RESOURCE_TYPE_OPERATION = '''    {"desc": "create yggdrasil_zun_capsule for Zun Capsule autoscaling",
     "type": "create_resource_type",
     "resource_type": "yggdrasil_zun_capsule",
     "data": [{"attributes": {}}]},
'''
RESOURCE_ATTRIBUTE_OPERATION_MARKER = (
    "add yggdrasil_senlin_resource_id to Zun Capsule autoscaling"
)
RESOURCE_ATTRIBUTE_REMOVAL_OPERATION_MARKER = (
    "remove original_resource_id from Zun Capsule autoscaling"
)
RESOURCE_ATTRIBUTE_REMOVAL_OPERATION = '''    {"desc": "remove original_resource_id from Zun Capsule autoscaling",
     "type": "update_attribute_type",
     "resource_type": "yggdrasil_zun_capsule",
     "data": [{
         "op": "remove",
         "path": "/attributes/original_resource_id"
     }]},
'''
RESOURCE_ATTRIBUTE_OPERATION = '''    {"desc": "add yggdrasil_senlin_resource_id to Zun Capsule autoscaling",
     "type": "update_attribute_type",
     "resource_type": "yggdrasil_zun_capsule",
     "data": [{
         "op": "add",
         "path": "/attributes/yggdrasil_senlin_resource_id",
         "value": {"type": "string", "min_length": 0,
                   "max_length": 255, "required": False}
     }]},
'''
RESOURCE_TYPE_VALUE_COPY = "            value = first_op['value'].copy()\n"
RESOURCE_TYPE_VALUE_COPY_PATCH = (
    "            value = first_op.get('value', {}).copy()\n"
)


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


def patch_gnocchi_resource_type():
    candidates = sorted(
        {
            path.resolve()
            for path in Path("/var/lib/kolla/venv/lib").glob(
                "python*/site-packages/ceilometer/gnocchi_client.py"
            )
        }
    )
    if len(candidates) != 1:
        raise SystemExit(f"expected one Ceilometer Gnocchi client, found {candidates}")

    target = candidates[0]
    source = target.read_text()
    if RESOURCE_TYPE_VALUE_COPY in source:
        source = source.replace(
            RESOURCE_TYPE_VALUE_COPY, RESOURCE_TYPE_VALUE_COPY_PATCH, 1
        )
    elif RESOURCE_TYPE_VALUE_COPY_PATCH not in source:
        raise SystemExit("unexpected Ceilometer Gnocchi update operation layout")

    if LEGACY_RESOURCE_TYPE_OPERATION in source:
        source = source.replace(LEGACY_RESOURCE_TYPE_OPERATION, "", 1)

    if PREVIOUS_RESOURCE_TYPE_OPERATION in source:
        source = source.replace(
            PREVIOUS_RESOURCE_TYPE_OPERATION, RESOURCE_TYPE_OPERATION, 1
        )
    if RESOURCE_TYPE_OPERATION_MARKER not in source:
        needle = "resources_update_operations = [\n"
        if source.count(needle) != 1:
            raise SystemExit(
                "unexpected Ceilometer Gnocchi resource operation layout"
            )

        source = source.replace(
            needle,
            needle + RESOURCE_TYPE_OPERATION,
            1,
        )
    if RESOURCE_ATTRIBUTE_REMOVAL_OPERATION_MARKER not in source:
        if source.count(RESOURCE_TYPE_OPERATION) != 1:
            raise SystemExit(
                "unexpected Zun Capsule Gnocchi resource type operation"
            )
        source = source.replace(
            RESOURCE_TYPE_OPERATION,
            RESOURCE_TYPE_OPERATION + RESOURCE_ATTRIBUTE_REMOVAL_OPERATION,
            1,
        )
    if RESOURCE_ATTRIBUTE_OPERATION_MARKER not in source:
        if source.count(RESOURCE_ATTRIBUTE_REMOVAL_OPERATION) != 1:
            raise SystemExit(
                "unexpected Zun Capsule Gnocchi attribute removal operation"
            )
        source = source.replace(
            RESOURCE_ATTRIBUTE_REMOVAL_OPERATION,
            RESOURCE_ATTRIBUTE_REMOVAL_OPERATION + RESOURCE_ATTRIBUTE_OPERATION,
            1,
        )
    compile(source, str(target), "exec")
    target.write_text(source)
    print(f"added Zun Capsule Gnocchi resource type operation to {target}")


def main():
    patch_gnocchi()
    patch_rgw_client()
    patch_gnocchi_resource_type()


if __name__ == "__main__":
    main()
