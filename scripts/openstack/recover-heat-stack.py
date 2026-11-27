#!/usr/bin/env python3
"""Restore a deleted Heat owner project and resume Heat-managed cleanup.

This operator command deliberately leaves any restored Keystone project in
place. Project deletion is a separate, explicit command that requires a
complete empty resource inventory.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_HEAT_ADMIN_STACK = SCRIPT_DIR / "heat-admin-stack.py"
DEFAULT_PROJECT_INVENTORY = SCRIPT_DIR / "list-project-resources.py"
DEFAULT_TIMEOUT = 600.0


class RecoveryError(RuntimeError):
    """An operator action could not be completed safely."""


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise argparse.ArgumentTypeError("ID must be a UUID") from exc


def _project_uuid(value: str) -> str:
    try:
        uuid.UUID(value)
        return value.lower()
    except (ValueError, AttributeError, TypeError) as exc:
        raise argparse.ArgumentTypeError("project ID must be a UUID") from exc


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover Heat stack cleanup after its Keystone owner project was "
            "deleted. The owner project is never removed automatically."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    recover = commands.add_parser(
        "recover", help="restore the owner project if needed, then delete the stack"
    )
    recover.add_argument("stack_id", type=_uuid, metavar="STACK_UUID")
    recover.add_argument(
        "--project-name",
        help="name to use only if the missing owner project must be restored",
    )
    recover.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"wait this long for Heat cleanup (default: {DEFAULT_TIMEOUT:g})",
    )

    restore = commands.add_parser(
        "restore-only", help="ensure the owner project exists, without deleting the stack"
    )
    restore.add_argument("stack_id", type=_uuid, metavar="STACK_UUID")
    restore.add_argument(
        "--project-name",
        help="name to use only if the missing owner project must be restored",
    )

    delete = commands.add_parser(
        "delete-only", help="delete a stack only when its owner project already exists"
    )
    delete.add_argument("stack_id", type=_uuid, metavar="STACK_UUID")
    delete.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"wait this long for Heat cleanup (default: {DEFAULT_TIMEOUT:g})",
    )

    cleanup = commands.add_parser(
        "cleanup-project",
        help="explicitly delete a project only after strict empty-inventory checks",
    )
    cleanup.add_argument("project_id", type=_project_uuid, metavar="PROJECT_UUID")
    cleanup.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion after the empty inventory has been verified",
    )
    return parser


def _field(mapping: Mapping[str, Any], name: str) -> Any:
    wanted = name.casefold().replace("_", " ")
    for key, value in mapping.items():
        if str(key).casefold().replace("_", " ") == wanted:
            return value
    return None


def _failure_text(result: subprocess.CompletedProcess[str]) -> str:
    detail = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
    if detail:
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        for line in lines:
            if line.casefold().startswith(("error:", "error ", "failed:")):
                return line
        useful_lines = [
            line
            for line in lines
            if not line.startswith(("Traceback", "File \"", "(Background on this error"))
        ]
        if useful_lines:
            return useful_lines[-1]
        return lines[-1]
    return f"command exited with status {result.returncode}"


class RecoveryOperator:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        runner: Any = subprocess.run,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self.openstack = self._openstack_binary()
        self.python = self.environ.get("OPENSTACK_PYTHON") or sys.executable
        self.heat_admin_stack = Path(
            self.environ.get("HEAT_ADMIN_STACK_BIN", str(DEFAULT_HEAT_ADMIN_STACK))
        )
        self.inventory = Path(
            self.environ.get("LIST_PROJECT_RESOURCES_BIN", str(DEFAULT_PROJECT_INVENTORY))
        )
        self.container_cli = self.environ.get("KEYSTONE_CONTAINER_CLI") or shutil.which(
            "docker"
        ) or shutil.which("podman")
        self.keystone_container = self.environ.get("KEYSTONE_CONTAINER", "keystone")

    def _openstack_binary(self) -> str:
        configured = self.environ.get("OPENSTACK_BIN")
        if configured:
            return configured
        found = shutil.which("openstack")
        if found:
            return found
        raise RecoveryError(
            "OpenStack client not found; set OPENSTACK_BIN or add openstack to PATH"
        )

    def _run(
        self,
        argv: Sequence[str],
        environ: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                list(argv),
                text=True,
                capture_output=True,
                check=False,
                env=self.environ if environ is None else dict(environ),
            )
        except OSError as exc:
            raise RecoveryError(f"unable to run {argv[0]}: {exc}") from exc

    def _checked(
        self,
        argv: Sequence[str],
        description: str,
        environ: Mapping[str, str] | None = None,
    ) -> str:
        result = self._run(argv, environ=environ)
        if result.returncode != 0:
            raise RecoveryError(f"{description}: {_failure_text(result)}")
        return result.stdout

    def inspect_stack(self, stack_id: str) -> dict[str, Any]:
        output = self._checked(
            [str(self.heat_admin_stack), "show", stack_id],
            f"unable to inspect Heat stack {stack_id}",
        )
        try:
            details = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RecoveryError("Heat stack inspection returned invalid JSON") from exc
        if not isinstance(details, dict):
            raise RecoveryError("Heat stack inspection returned an unexpected response")
        returned_id = str(details.get("id") or "")
        project_id = details.get("project_id")
        if returned_id and returned_id != stack_id:
            raise RecoveryError(
                f"Heat returned stack {returned_id}, expected requested stack {stack_id}"
            )
        if not project_id:
            raise RecoveryError("Heat stack inspection did not return its owner project ID")
        try:
            uuid.UUID(str(project_id))
        except ValueError as exc:
            raise RecoveryError("Heat returned an invalid owner project ID") from exc
        details["project_id"] = str(project_id).lower()
        details["stack_status"] = str(details.get("stack_status") or "UNKNOWN").upper()
        return details

    def project_details(self, project_id: str) -> dict[str, Any] | None:
        result = self._run([self.openstack, "project", "show", project_id, "-f", "json"])
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").casefold()
            is_missing = "project" in error and any(
                marker in error
                for marker in ("not found", "no project", "does not exist", "404")
            )
            if is_missing:
                return None
            raise RecoveryError(
                f"unable to check owner project {project_id}: {_failure_text(result)}"
            )
        try:
            details = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RecoveryError("OpenStack project show returned invalid JSON") from exc
        if not isinstance(details, dict):
            raise RecoveryError("OpenStack project show returned an unexpected response")
        returned_id = _field(details, "id")
        try:
            ids_match = uuid.UUID(str(returned_id)) == uuid.UUID(project_id)
        except (ValueError, AttributeError, TypeError):
            ids_match = False
        if not ids_match:
            raise RecoveryError(
                f"OpenStack returned project {returned_id!r} while looking up {project_id}"
            )
        return details

    def project_exists(self, project_id: str) -> bool:
        return self.project_details(project_id) is not None

    def restore_project(
        self, project_id: str, stack_id: str, project_name: str | None = None
    ) -> bool:
        """Ensure the project ID exists; return True only when this run created it."""
        if self.project_exists(project_id):
            print(f"Owner project {project_id} already exists.")
            return False
        if not self.container_cli:
            raise RecoveryError("Docker or Podman is required to restore a Keystone project")
        name = project_name or f"heat-recovery-{stack_id[:8]}"
        if not name.strip():
            raise RecoveryError("project name cannot be empty")
        print(f"Restoring owner project {project_id} as {name!r}.")
        argv = [
            self.container_cli,
            "exec",
            self.keystone_container,
            "keystone-manage",
            "project_setup",
            "--project-name",
            name,
            "--project-id",
            project_id,
        ]
        result = self._run(argv)
        if result.returncode != 0:
            # A concurrent operator may have restored the project. Continue
            # only when Keystone now confirms the exact UUID exists.
            if self.project_exists(project_id):
                print(f"Owner project {project_id} now exists; continuing.")
                return False
            raise RecoveryError(
                f"unable to restore owner project {project_id}: {_failure_text(result)}"
            )
        if not self.project_exists(project_id):
            raise RecoveryError(
                f"Keystone project setup completed, but project {project_id} is still missing"
            )
        print(f"Restored owner project {project_id}; it will be left in place.")
        return True

    def delete_stack(self, stack_id: str, timeout: float) -> int:
        command = [
            str(self.heat_admin_stack),
            "delete",
            stack_id,
            "--recover-stale-credentials",
            "--yes",
            "--timeout",
            f"{timeout:g}",
        ]
        result = self._run(command)
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        return int(result.returncode)

    def cleanup_project(self, project_id: str, confirmed: bool) -> int:
        if not confirmed:
            raise RecoveryError(
                "project cleanup requires --yes after reviewing this explicit request"
            )
        project = self.project_details(project_id)
        if project is None:
            raise RecoveryError(f"project {project_id} does not exist")
        project_id = str(_field(project, "id"))
        domain_id = _field(project, "domain_id")
        if not domain_id:
            raise RecoveryError(
                f"project {project_id} has no domain ID; cannot safely scope its inventory"
            )

        inventory_text = self._strict_project_inventory(project_id, str(domain_id))
        try:
            inventory = json.loads(inventory_text)
        except json.JSONDecodeError as exc:
            raise RecoveryError("project inventory returned invalid JSON") from exc
        if not isinstance(inventory, dict) or not isinstance(inventory.get("resources"), dict):
            raise RecoveryError("project inventory response is missing its resources map")
        if inventory.get("errors"):
            raise RecoveryError("project inventory reported errors despite strict mode")
        inventory_project = inventory.get("project")
        inventory_id = inventory_project.get("id") if isinstance(inventory_project, dict) else None
        if str(inventory_id or "") != project_id:
            raise RecoveryError(
                f"inventory returned project {inventory_id!r}, expected {project_id}"
            )
        resource_count = _count_resource_records(inventory["resources"])
        if resource_count:
            raise RecoveryError(
                f"project {project_id} still has {resource_count} inventoried resource(s); "
                "refusing deletion"
            )

        stacks_text = self._checked(
            [str(self.heat_admin_stack), "list-project", project_id],
            f"unable to verify Heat stacks for project {project_id}",
        )
        try:
            stacks = json.loads(stacks_text)
        except json.JSONDecodeError as exc:
            raise RecoveryError("Heat Engine stack inventory returned invalid JSON") from exc
        if not isinstance(stacks, list):
            raise RecoveryError("Heat Engine stack inventory returned an unexpected response")
        if stacks:
            raise RecoveryError(
                f"project {project_id} still has {len(stacks)} Heat stack(s); refusing deletion"
            )

        print(f"Verified project {project_id} has no inventoried resources or Heat stacks.")
        self._checked(
            [self.openstack, "project", "delete", project_id],
            f"unable to delete empty project {project_id}",
        )
        print(f"Deleted project {project_id}.")
        return 0

    def _strict_project_inventory(self, project_id: str, domain_id: str) -> str:
        """Run inventory with a temporary project-scoped admin grant if needed."""
        username = self.environ.get("OS_USERNAME")
        password = self.environ.get("OS_PASSWORD")
        if not username or not password:
            raise RecoveryError(
                "strict project inventory requires OS_USERNAME and OS_PASSWORD "
                "to obtain a project-scoped admin token"
            )
        user_domain = self.environ.get("OS_USER_DOMAIN_ID") or self.environ.get(
            "OS_USER_DOMAIN_NAME"
        ) or "Default"
        user_text = self._checked(
            [
                self.openstack,
                "user",
                "show",
                username,
                "--domain",
                user_domain,
                "-f",
                "json",
            ],
            f"unable to resolve admin user {username!r}",
        )
        try:
            user = json.loads(user_text)
        except json.JSONDecodeError as exc:
            raise RecoveryError("OpenStack user show returned invalid JSON") from exc
        user_id = _field(user, "id") if isinstance(user, dict) else None
        if not user_id:
            raise RecoveryError("OpenStack user show did not return an admin user ID")

        assignments_text = self._checked(
            [
                self.openstack,
                "role",
                "assignment",
                "list",
                "--user",
                str(user_id),
                "--project",
                project_id,
                "--role",
                "admin",
                "-f",
                "json",
            ],
            f"unable to check existing admin role assignments for project {project_id}",
        )
        try:
            assignments = json.loads(assignments_text)
        except json.JSONDecodeError as exc:
            raise RecoveryError("OpenStack role assignment list returned invalid JSON") from exc
        if not isinstance(assignments, list):
            raise RecoveryError("OpenStack role assignment list returned an unexpected response")

        added_role = not assignments
        if added_role:
            self._checked(
                [
                    self.openstack,
                    "role",
                    "add",
                    "--user",
                    str(user_id),
                    "--project",
                    project_id,
                    "admin",
                ],
                f"unable to grant temporary project admin access to {project_id}",
            )
            print(f"Temporarily granted project admin access for inventory of {project_id}.")

        scoped_environ = dict(self.environ)
        for name in (
            "OS_SYSTEM_SCOPE",
            "OS_TOKEN",
            "OS_PROJECT_NAME",
            "OS_PROJECT_DOMAIN_NAME",
            "OS_PROJECT_DOMAIN_ID",
            "OS_TENANT_NAME",
            "OS_TENANT_ID",
            "OS_DOMAIN_NAME",
            "OS_DOMAIN_ID",
        ):
            scoped_environ.pop(name, None)
        scoped_environ["OS_AUTH_TYPE"] = "password"
        scoped_environ["OS_PROJECT_ID"] = project_id
        scoped_environ["OS_PROJECT_DOMAIN_ID"] = domain_id
        inventory_command = [
            self.python,
            str(self.inventory),
            "--project",
            project_id,
            "--format",
            "json",
            "--strict",
        ]

        inventory_result: subprocess.CompletedProcess[str] | None = None
        removal_error: RecoveryError | None = None
        try:
            inventory_result = self._run(inventory_command, environ=scoped_environ)
        finally:
            if added_role:
                try:
                    self._checked(
                        [
                            self.openstack,
                            "role",
                            "remove",
                            "--user",
                            str(user_id),
                            "--project",
                            project_id,
                            "admin",
                        ],
                        f"unable to remove temporary project admin access from {project_id}",
                    )
                    print(f"Removed temporary project admin access from {project_id}.")
                except RecoveryError as exc:
                    removal_error = exc
        if removal_error:
            raise removal_error
        if inventory_result is None:
            raise RecoveryError(f"inventory did not run for project {project_id}")
        if inventory_result.returncode != 0:
            raise RecoveryError(
                f"unable to obtain a complete inventory for project {project_id}: "
                f"{_failure_text(inventory_result)}"
            )
        return inventory_result.stdout


def _count_resource_records(resources: Mapping[str, Any]) -> int:
    count = 0
    for service, resource_map in resources.items():
        if not isinstance(resource_map, Mapping):
            raise RecoveryError(
                f"project inventory has an unexpected resource map for service {service!r}"
            )
        for resource_name, records in resource_map.items():
            if not isinstance(records, list):
                raise RecoveryError(
                    "project inventory has an unexpected record list for "
                    f"{service!r}/{resource_name!r}"
                )
            count += len(records)
    return count


def run(args: argparse.Namespace, operator: RecoveryOperator) -> int:
    if args.command == "cleanup-project":
        return operator.cleanup_project(args.project_id, args.yes)

    details = operator.inspect_stack(args.stack_id)
    project_id = details["project_id"]
    stack_status = details["stack_status"]
    stack_name = str(details.get("stack_name") or args.stack_id)
    print(
        f"Heat stack {stack_name!r} ({args.stack_id}) belongs to project {project_id} "
        f"and is {stack_status}."
    )

    if args.command == "restore-only":
        operator.restore_project(project_id, args.stack_id, args.project_name)
        print("Restore-only complete; the Heat stack was not changed.")
        return 0

    if args.command == "recover":
        if stack_status == "DELETE_COMPLETE":
            print("Stack is already DELETE_COMPLETE; nothing to recover.")
            return 0
        operator.restore_project(project_id, args.stack_id, args.project_name)
        print(
            f"Requesting Heat resource cleanup for project {project_id}. "
            "Any restored project will be left in place."
        )
        return operator.delete_stack(args.stack_id, args.timeout)

    if args.command == "delete-only":
        if stack_status == "DELETE_COMPLETE":
            print("Stack is already DELETE_COMPLETE; nothing to delete.")
            return 0
        if not operator.project_exists(project_id):
            raise RecoveryError(
                f"owner project {project_id} is missing; run restore-only first"
            )
        print(f"Requesting Heat resource cleanup for existing project {project_id}.")
        return operator.delete_stack(args.stack_id, args.timeout)

    raise RecoveryError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        operator = RecoveryOperator()
        return run(args, operator)
    except RecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
