#!/usr/bin/env python3
"""Delete project resources from a list-project-resources JSON report.

The inventory command deliberately discovers resources through the service
catalog.  Deletion must still be explicit because OpenStack services have
different dependency rules and command-line interfaces.  This command keeps
those rules in one place, refuses incomplete inventories by default, and
never deletes the Keystone project itself.

Examples::

    scripts/openstack/list-project-resources.py \
        --project demo --format json > /tmp/demo-resources.json
    scripts/openstack/delete-project-resources.py \
        /tmp/demo-resources.json --yes

For the expiry worker, ``--project`` refreshes the inventory before deletion
and verifies it again afterwards::

    scripts/openstack/delete-project-resources.py \
        --project demo --yes --verify
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_POLL_SECONDS = 2.0
DEFAULT_WAIT_SECONDS = 600.0


class DeletePlanError(RuntimeError):
    """Raised when an inventory cannot be safely converted into a plan."""


@dataclass(frozen=True)
class Resource:
    service: str
    resource: str
    identifier: str
    raw: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str, str]:
        return self.service, self.resource, self.identifier


@dataclass(frozen=True)
class Operation:
    phase: int
    resource: Resource
    argv: tuple[str, ...]


SERVICE_ALIASES = {
    "alarming": "alarm",
    "application_container": "application-container",
    "backup": "backup",
    "container": "application-container",
    "zun": "application-container",
    "block_storage": "block-storage",
    "cinder": "block-storage",
    "clustering": "clustering",
    "compute": "compute",
    "database": "database",
    "designate": "dns",
    "dns": "dns",
    "heat": "orchestration",
    "image": "image",
    "key_manager": "key-manager",
    "load_balancer": "load-balancer",
    "loadbalancer": "load-balancer",
    "magnum": "container-infrastructure-management",
    "manila": "shared-file-system",
    "mistral": "workflow",
    "network": "network",
    "neutron": "network",
    "nova": "compute",
    "object_store": "object-store",
    "orchestration": "orchestration",
    "octavia": "load-balancer",
    "shared_file_system": "shared-file-system",
    "swift": "object-store",
    "workflow_engine": "workflow",
}


RESOURCE_ALIASES = {
    "action_executions": "action_execution",
    "actions": "action",
    "alarms": "alarm",
    "application_containers": "container",
    "backups": "backup",
    "capsules": "capsule",
    "cluster_templates": "cluster_template",
    "code_sources": "code_source",
    "containers": "container",
    "cron_triggers": "cron_trigger",
    "database_instances": "instance",
    "event_triggers": "event_trigger",
    "events": "event",
    "floating_ips": "floating_ip",
    "group_snapshots": "group_snapshot",
    "keypairs": "keypair",
    "load_balancers": "load_balancer",
    "loadbalancers": "load_balancer",
    "networks": "network",
    "network_ip_availabilities": "network_ip_availability",
    "objects": "object",
    "ports": "port",
    "rbac_policies": "rbac_policy",
    "record_sets": "recordset",
    "recordsets": "recordset",
    "security_group_rules": "security_group_rule",
    "security_groups": "security_group",
    "server_groups": "server_group",
    "servers": "server",
    "share_groups": "share_group",
    "share_snapshots": "share_snapshot",
    "share_networks": "share_network",
    "shares": "share",
    "snapshots": "snapshot",
    "software_configs": "software_config",
    "software_deployments": "software_deployment",
    "stacks": "stack",
    "subnets": "subnet",
    "trusts": "trust",
    "user_messages": "user_message",
    "volume_transfers": "volume_transfer",
    "volumes": "volume",
    "workbooks": "workbook",
    "workflow_executions": "execution",
    "workflows": "workflow",
    "zone_shares": "zone_share",
    "zones": "zone",
}


# A project deletion follows the dependency direction used by the legacy
# shell cleanup.  Service-managed parents (Heat stacks and Mistral
# executions) are removed before their child resources, and networking is
# removed last.
RESOURCE_PHASES = {
    # Heat stacks use Keystone trusts for deferred cleanup, so trusts must
    # remain until every service resource (especially Heat) is gone.
    ("identity", "trust"): 200,
    ("workflow", "event_trigger"): 10,
    ("workflow", "cron_trigger"): 10,
    ("workflow", "action_execution"): 15,
    ("workflow", "execution"): 20,
    ("workflow", "workflow"): 30,
    ("workflow", "action"): 31,
    ("workflow", "code_source"): 31,
    ("workflow", "dynamic_action"): 31,
    ("workflow", "environment"): 31,
    ("workflow", "workbook"): 31,
    ("dns", "zone_share"): 35,
    ("dns", "recordset"): 36,
    ("dns", "zone"): 37,
    ("alarm", "alarm"): 34,
    ("clustering", "cluster"): 40,
    ("clustering", "policy"): 41,
    ("clustering", "profile"): 41,
    ("orchestration", "stack"): 40,
    ("orchestration", "software_deployment"): 41,
    ("orchestration", "software_config"): 42,
    ("application-container", "capsule"): 50,
    ("application-container", "container"): 50,
    ("load-balancer", "load_balancer"): 60,
    ("container-infrastructure-management", "cluster"): 61,
    ("container-infrastructure-management", "cluster_template"): 62,
    ("database", "instance"): 63,
    ("shared-file-system", "share_snapshot"): 64,
    ("shared-file-system", "share"): 65,
    ("shared-file-system", "share_group"): 66,
    ("shared-file-system", "share_network"): 67,
    ("shared-file-system", "user_message"): 68,
    ("object-store", "object"): 70,
    ("object-store", "container"): 71,
    ("compute", "server"): 80,
    ("compute", "keypair"): 81,
    ("compute", "server_group"): 82,
    ("image", "image"): 83,
    ("key-manager", "container"): 84,
    ("key-manager", "order"): 84,
    ("key-manager", "secret"): 85,
    ("block-storage", "group_snapshot"): 90,
    ("block-storage", "snapshot"): 91,
    ("block-storage", "backup"): 92,
    ("block-storage", "group"): 93,
    ("block-storage", "volume_transfer"): 94,
    ("block-storage", "volume"): 95,
    ("network", "floating_ip"): 100,
    ("network", "router"): 110,
    ("network", "port"): 120,
    ("network", "subnet"): 130,
    ("network", "network"): 140,
    ("network", "rbac_policy"): 139,
    ("network", "security_group_rule"): 150,
    ("network", "security_group"): 151,
}


# These are read-only child records.  Their owning object is deleted by the
# parent operation, so attempting to delete them individually would either
# fail or make the cleanup order less safe.
COVERED_CHILD_RESOURCES = {
    ("application-container", "action"),
    ("application-container", "container_action"),
    ("application-container", "log"),
    ("application-container", "stats"),
    ("workflow", "action_execution_result"),
    ("workflow", "task"),
    ("workflow", "task_execution"),
    ("load-balancer", "health_monitor"),
    ("load-balancer", "listener"),
    ("load-balancer", "member"),
    ("load-balancer", "pool"),
    ("orchestration", "resource"),
    ("compute", "server_action"),
    ("compute", "server_interface"),
    ("compute", "server_ip"),
}


def normalize_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def canonical_service(value: object) -> str:
    normalized = normalize_name(value)
    return SERVICE_ALIASES.get(normalized, normalized.replace("_", "-"))


def canonical_resource(value: object) -> str:
    normalized = normalize_name(value)
    return RESOURCE_ALIASES.get(normalized, normalized)


def _scalar_text(value: object) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _identifier(raw: Mapping[str, Any], service: str, resource: str) -> str:
    keys = (
        ("name", "id", "uuid")
        if service == "workflow" and resource == "cron_trigger"
        else ("id", "uuid", "name", "display_name", "stack_name")
    )
    for key in keys:
        value = _scalar_text(raw.get(key))
        if value:
            return value
    return ""


def _object_operation(resource: Resource) -> tuple[str, ...]:
    container = _scalar_text(
        resource.raw.get("container") or resource.raw.get("container_name")
    )
    object_name = _scalar_text(
        resource.raw.get("name") or resource.raw.get("id") or resource.raw.get("object")
    )
    if not container or not object_name:
        raise DeletePlanError(
            "object-store object is missing its container or object name: "
            f"{resource.raw!r}"
        )
    return "object", "delete", container, object_name


def _recordset_operation(resource: Resource) -> tuple[str, ...]:
    zone = _scalar_text(resource.raw.get("zone_id") or resource.raw.get("zone"))
    if not zone:
        raise DeletePlanError(
            "dns recordset is missing its zone ID: " f"{resource.raw!r}"
        )
    return "recordset", "delete", zone, resource.identifier


def _zone_share_operation(resource: Resource) -> tuple[str, ...]:
    zone = _scalar_text(resource.raw.get("zone_id") or resource.raw.get("zone"))
    if not zone:
        raise DeletePlanError(
            "dns zone share is missing its zone ID: " f"{resource.raw!r}"
        )
    return "zone", "share", "delete", zone, resource.identifier


def command_for(resource: Resource) -> tuple[str, ...]:
    service = resource.service
    kind = resource.resource
    identifier = resource.identifier

    if service == "identity" and kind == "trust":
        return "trust", "delete", identifier
    if service == "dns":
        if kind == "recordset":
            return _recordset_operation(resource)
        if kind == "zone_share":
            return _zone_share_operation(resource)
        if kind == "zone":
            return "zone", "delete", "--hard-delete", identifier
    if service == "alarm" and kind == "alarm":
        return "alarm", "delete", identifier
    if service == "clustering":
        commands = {
            "cluster": ("cluster", "delete", "--force"),
            "policy": ("cluster", "policy", "delete", "--force"),
            "profile": ("cluster", "profile", "delete", "--force"),
        }
        if kind in commands:
            return (*commands[kind], identifier)

    if service == "workflow":
        commands = {
            "action": ("action", "definition", "delete"),
            "action_execution": ("action", "execution", "delete"),
            "code_source": ("code", "source", "delete"),
            "cron_trigger": ("cron", "trigger", "delete"),
            "dynamic_action": ("dynamic", "action", "delete"),
            "environment": ("workflow", "env", "delete"),
            "event_trigger": ("event", "trigger", "delete"),
            "execution": ("workflow", "execution", "delete", "--force"),
            "workbook": ("workbook", "delete"),
            "workflow": ("workflow", "delete"),
        }
        if kind in commands:
            return (*commands[kind], identifier)

    if service == "orchestration":
        commands = {
            "software_config": ("software", "config", "delete"),
            "software_deployment": ("software", "deployment", "delete"),
            "stack": ("stack", "delete", "--yes"),
        }
        if kind in commands:
            return (*commands[kind], identifier)

    if service == "application-container":
        commands = {
            "capsule": ("capsule", "delete"),
            "container": (
                "appcontainer",
                "delete",
                "--force",
                "--stop",
                "--wait",
            ),
        }
        if kind in commands:
            return (*commands[kind], identifier)

    if service == "load-balancer" and kind == "load_balancer":
        return "loadbalancer", "delete", "--cascade", "--wait", identifier
    if service == "container-infrastructure-management" and kind == "cluster":
        return "coe", "cluster", "delete", identifier
    if service == "container-infrastructure-management" and kind == "cluster_template":
        return "coe", "cluster", "template", "delete", identifier
    if service == "database" and kind == "instance":
        return "database", "instance", "delete", "--force", identifier
    if service == "shared-file-system":
        commands = {
            "share": ("share", "delete", "--wait"),
            "share_network": ("share", "network", "delete"),
            "share_group": ("share", "group", "delete"),
            "share_snapshot": ("share", "snapshot", "delete"),
            "user_message": ("share", "message", "delete"),
        }
        if kind in commands:
            return (*commands[kind], identifier)
    if service == "object-store":
        if kind == "object":
            return _object_operation(resource)
        if kind == "container":
            return "container", "delete", "-r", identifier
    if service == "compute":
        commands = {
            "keypair": ("keypair", "delete"),
            "server": ("server", "delete", "--wait"),
            "server_group": ("server", "group", "delete"),
        }
        if kind in commands:
            return (*commands[kind], identifier)
    if service == "image" and kind == "image":
        return "image", "delete", identifier
    if service == "key-manager":
        commands = {
            "container": ("secret", "container", "delete"),
            "order": ("secret", "order", "delete"),
            "secret": ("secret", "delete"),
        }
        if kind in commands:
            return (*commands[kind], identifier)
    if service == "block-storage":
        commands = {
            "backup": ("volume", "backup", "delete"),
            "group": ("volume", "group", "delete"),
            "group_snapshot": ("volume", "group", "snapshot", "delete"),
            "snapshot": ("volume", "snapshot", "delete"),
            "volume": ("volume", "delete", "--force"),
            "volume_transfer": ("volume", "transfer", "request", "delete"),
        }
        if kind in commands:
            return (*commands[kind], identifier)
    if service == "network":
        commands = {
            "floating_ip": ("floating", "ip", "delete"),
            "network": ("network", "delete"),
            "port": ("port", "delete"),
            "rbac_policy": ("network", "rbac", "delete"),
            "security_group": ("security", "group", "delete"),
            "security_group_rule": ("security", "group", "rule", "delete"),
            "subnet": ("subnet", "delete"),
        }
        if kind == "router":
            return "router", "delete", identifier
        if kind in commands:
            return (*commands[kind], identifier)

    raise DeletePlanError(f"unsupported project resource: {service}/{kind}")


def _resource_entries(report: Mapping[str, Any]) -> tuple[list[Resource], list[str]]:
    resources = report.get("resources")
    if not isinstance(resources, Mapping):
        raise DeletePlanError("inventory report has no resources object")

    entries: list[Resource] = []
    issues: list[str] = []
    for raw_service, service_resources in resources.items():
        service = canonical_service(raw_service)
        if not isinstance(service_resources, Mapping):
            issues.append(f"{raw_service}: resource collection is not an object")
            continue
        for raw_resource, objects in service_resources.items():
            resource = canonical_resource(raw_resource)
            if not objects:
                continue
            if not isinstance(objects, list):
                issues.append(f"{service}/{resource}: resource collection is not a list")
                continue
            if service == "identity" and resource == "project":
                continue
            if (service, resource) in COVERED_CHILD_RESOURCES:
                continue
            if (service, resource) not in RESOURCE_PHASES:
                issues.append(f"unsupported project resource: {service}/{resource}")
                continue
            for item in objects:
                if not isinstance(item, Mapping):
                    issues.append(f"{service}/{resource}: resource is not an object")
                    continue
                identifier = _identifier(item, service, resource)
                if not identifier:
                    issues.append(f"{service}/{resource}: resource has no id or name: {item!r}")
                    continue
                entries.append(Resource(service, resource, identifier, item))
    deduplicated: dict[tuple[str, str, str], Resource] = {}
    for entry in entries:
        deduplicated.setdefault(entry.key, entry)
    return list(deduplicated.values()), issues


def plan_deletions(report: Mapping[str, Any]) -> tuple[list[Operation], list[str]]:
    """Build an ordered deletion plan and return validation issues."""

    resources, issues = _resource_entries(report)
    operations: list[Operation] = []
    for resource in resources:
        try:
            argv = command_for(resource)
        except DeletePlanError as exc:
            issues.append(str(exc))
            continue
        operations.append(Operation(RESOURCE_PHASES[(resource.service, resource.resource)], resource, argv))
    operations.sort(key=lambda operation: (operation.phase, operation.resource.key))
    return operations, sorted(set(issues))


def load_report(path: str) -> dict[str, Any]:
    if path == "-":
        payload = sys.stdin.read()
    else:
        payload = Path(path).read_text(encoding="utf-8")
    try:
        report = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DeletePlanError(
            "inventory input is not JSON; run list-project-resources.py with --format json"
        ) from exc
    if not isinstance(report, dict):
        raise DeletePlanError("inventory JSON must contain an object at the top level")
    return report


def _missing_output(output: str) -> bool:
    text = output.casefold()
    return bool(
        re.search(
            r"not found|no .* found|404 not found|does not exist|could not be found|"
            r"does not have an interface",
            text,
        )
    )


def _run_openstack(
    openstack_bin: str,
    argv: Sequence[str],
    *,
    allow_missing: bool = True,
) -> tuple[bool, str]:
    completed = subprocess.run(
        [openstack_bin, *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(value for value in (completed.stdout, completed.stderr) if value).strip()
    if completed.returncode == 0:
        return True, output
    if allow_missing and _missing_output(output):
        return True, output
    return False, output or f"exit code {completed.returncode}"


def _router_ports(openstack_bin: str, router_id: str) -> tuple[list[str], str | None]:
    completed = subprocess.run(
        [openstack_bin, "port", "list", "--router", router_id, "-f", "value", "-c", "ID"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(value for value in (completed.stdout, completed.stderr) if value).strip()
    if completed.returncode != 0 and not _missing_output(output):
        return [], output or f"exit code {completed.returncode}"
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()], None


def delete_router(openstack_bin: str, resource: Resource) -> list[str]:
    errors: list[str] = []
    ports, error = _router_ports(openstack_bin, resource.identifier)
    if error:
        errors.append(f"{resource.service}/{resource.resource}/{resource.identifier}: {error}")
        return errors
    for port in ports:
        ok, detail = _run_openstack(
            openstack_bin,
            ("router", "remove", "port", resource.identifier, port),
        )
        if not ok:
            errors.append(f"router {resource.identifier}: remove port {port}: {detail}")
    for argv in (
        ("router", "unset", "--external-gateway", resource.identifier),
        ("router", "delete", resource.identifier),
    ):
        ok, detail = _run_openstack(openstack_bin, argv)
        if not ok:
            errors.append(f"router {resource.identifier}: {' '.join(argv)}: {detail}")
    return errors


def wait_for_stack_delete(
    openstack_bin: str,
    stack_id: str,
    timeout: float,
    poll_seconds: float,
) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [
                openstack_bin,
                "stack",
                "show",
                stack_id,
                "-f",
                "value",
                "-c",
                "stack_status",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        output = "\n".join(value for value in (completed.stdout, completed.stderr) if value).strip()
        if completed.returncode != 0 and _missing_output(output):
            return None
        if completed.returncode != 0:
            return output or f"exit code {completed.returncode}"
        status = completed.stdout.strip().casefold()
        if status == "delete_complete":
            return None
        if status == "delete_failed":
            return output or "stack deletion failed"
        time.sleep(poll_seconds)
    return f"stack remained visible after {timeout:.0f} seconds"


def execute_plan(
    operations: Iterable[Operation],
    *,
    openstack_bin: str,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> list[str]:
    errors: list[str] = []
    for operation in operations:
        resource = operation.resource
        print(
            "Deleting "
            f"{resource.service}/{resource.resource} {resource.identifier}: "
            f"{' '.join(operation.argv)}"
        )
        if resource.resource == "router":
            errors.extend(delete_router(openstack_bin, resource))
            continue
        ok, detail = _run_openstack(openstack_bin, operation.argv)
        if not ok:
            errors.append(
                f"{resource.service}/{resource.resource}/{resource.identifier}: {detail}"
            )
            continue
        if resource.service == "orchestration" and resource.resource == "stack":
            error = wait_for_stack_delete(
                openstack_bin,
                resource.identifier,
                wait_seconds,
                poll_seconds,
            )
            if error:
                errors.append(f"stack {resource.identifier}: {error}")
    return errors


def run_inventory(
    script: str,
    project: str,
    *,
    openstack_bin: str,
    strict: bool,
    interface: str | None,
    region: str | None,
) -> dict[str, Any]:
    argv = [sys.executable, script, "--project", project, "--format", "json"]
    if strict:
        argv.append("--strict")
    argv.extend(("--openstack-bin", openstack_bin))
    if interface:
        argv.extend(("--interface", interface))
    if region:
        argv.extend(("--region", region))
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DeletePlanError(f"project inventory failed: {detail}")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeletePlanError("project inventory returned invalid JSON") from exc
    if not isinstance(report, dict):
        raise DeletePlanError("project inventory did not return a JSON object")
    return report


def _report_errors(report: Mapping[str, Any]) -> list[str]:
    errors = report.get("errors", [])
    if not isinstance(errors, list):
        return ["inventory report errors field is not a list"]
    return [str(error) for error in errors if error]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete project resources from list-project-resources JSON output."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("report", nargs="?", help="JSON report path, or - for stdin.")
    source.add_argument(
        "--project",
        help="Refresh the JSON inventory for this project before deleting it.",
    )
    parser.add_argument(
        "--inventory-script",
        default=str(Path(__file__).with_name("list-project-resources.py")),
        help="Path to list-project-resources.py when --project is used.",
    )
    parser.add_argument(
        "--openstack-bin",
        default="openstack",
        help="OpenStack CLI executable (default: openstack).",
    )
    parser.add_argument(
        "--interface",
        default=None,
        help="Endpoint interface passed to the inventory command.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Region passed to the inventory command.",
    )
    parser.add_argument(
        "--allow-inventory-errors",
        action="store_true",
        help="Allow a report with inventory errors; unsupported resources still stop deletion.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete resources. Without this flag, print the ordered plan only.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Refresh the inventory after deletion and fail if any resources remain.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help=f"Maximum wait for each Heat stack deletion (default: {DEFAULT_WAIT_SECONDS:g}).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help=f"Heat stack status polling interval (default: {DEFAULT_POLL_SECONDS:g}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.project:
            report = run_inventory(
                args.inventory_script,
                args.project,
                openstack_bin=args.openstack_bin,
                strict=not args.allow_inventory_errors,
                interface=args.interface,
                region=args.region,
            )
        else:
            report = load_report(args.report)

        inventory_errors = _report_errors(report)
        if inventory_errors and not args.allow_inventory_errors:
            raise DeletePlanError(
                "refusing to delete from an incomplete inventory; rerun the inventory "
                "with --strict and resolve these errors: "
                + "; ".join(inventory_errors)
            )

        operations, issues = plan_deletions(report)
        if issues:
            raise DeletePlanError("refusing to delete: " + "; ".join(issues))

        if not operations:
            print("No deletable project resources found.")
            return 0

        if not args.yes:
            print("Dry run; pass --yes to execute the ordered deletion plan.")
            for operation in operations:
                print(
                    f"  {operation.resource.service}/{operation.resource.resource} "
                    f"{operation.resource.identifier}: {' '.join(operation.argv)}"
                )
            return 0

        errors = execute_plan(
            operations,
            openstack_bin=args.openstack_bin,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 3

        if args.verify:
            if not args.project:
                raise DeletePlanError("--verify requires --project so the inventory can be refreshed")
            verified = run_inventory(
                args.inventory_script,
                args.project,
                openstack_bin=args.openstack_bin,
                strict=not args.allow_inventory_errors,
                interface=args.interface,
                region=args.region,
            )
            verify_errors = _report_errors(verified)
            verify_operations, verify_issues = plan_deletions(verified)
            if verify_errors or verify_issues or verify_operations:
                details = verify_errors + verify_issues
                if verify_operations:
                    details.append(
                        "remaining resources: "
                        + ", ".join(
                            f"{item.resource.service}/{item.resource.resource}/"
                            f"{item.resource.identifier}"
                            for item in verify_operations
                        )
                    )
                raise DeletePlanError("post-delete verification failed: " + "; ".join(details))
            print("Verified that no project resources remain.")
        return 0
    except (DeletePlanError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
