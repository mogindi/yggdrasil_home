#!/usr/bin/env python3
"""Show or delete Heat stacks through the local Kolla Heat Engine RPC.

Run this command as root on a controller that runs ``heat_engine``::

    scripts/openstack/heat-admin-stack.py show STACK_UUID
    scripts/openstack/heat-admin-stack.py delete STACK_UUID

The command runs its Heat RPC portion inside the Heat Engine container so it
uses the installed Heat version, service configuration, and message bus. Stack
deletion needs system-scoped admin credentials in the caller's ``OS_*``
environment, including ``OS_USERNAME`` and ``OS_PASSWORD``. For an existing
project, the command obtains a project-scoped admin token because Heat must
refresh the stack's stored trust before cleanup. If needed, it temporarily
grants the system admin the project admin role and removes that grant after
successful deletion in the same run. If deletion fails or times out, the grant
stays in place for retry and the command reports how to remove it after
cleanup. Missing-project stacks use Heat's normal RPC path and report any
cleanup failure. This is an operator-only interface to Heat's internal RPC
API, not a replacement for the public OpenStack API.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO


RPC_RUNTIME_ENV = "HEAT_ADMIN_STACK_RPC_RUNTIME"
CONTAINER_ENV = "HEAT_ENGINE_CONTAINER"
CONTAINER_CLI_ENV = "HEAT_ADMIN_STACK_CONTAINER_CLI"
DEFAULT_CONTAINER = "heat_engine"
DEFAULT_TIMEOUT = 600.0
POLL_INTERVAL = 2.0
AUTH_ENV_NAMES = (
    "OS_AUTH_URL",
    "OS_USERNAME",
    "OS_PASSWORD",
    "OS_USER_DOMAIN_ID",
    "OS_USER_DOMAIN_NAME",
    "OS_TOKEN",
    "OS_SYSTEM_SCOPE",
    "OS_CACERT",
    "OS_INSECURE",
)


@dataclass
class SystemAdminAuth:
    context: Any
    identity: Any
    auth_url: str
    username: str | None
    password: str | None
    user_domain_id: str | None
    user_domain_name: str
    verify: bool | str
    user_id: str


@dataclass
class DeleteAuth:
    context: Any
    cleanup: Callable[[], None] | None = None
    retained_grant_notice: str | None = None


def _stack_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise argparse.ArgumentTypeError("stack ID must be a UUID") from exc


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
            "Inspect or normally delete a Heat stack from the local Kolla "
            "Heat Engine runtime. Run as root on a Heat controller."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    show_parser = commands.add_parser("show", help="show a stack by UUID")
    show_parser.add_argument("stack_id", type=_stack_uuid)

    delete_parser = commands.add_parser(
        "delete", help="delete a stack by UUID using Heat resource cleanup"
    )
    delete_parser.add_argument("stack_id", type=_stack_uuid)
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive UUID confirmation",
    )
    delete_parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"wait this long for deletion to finish (default: {DEFAULT_TIMEOUT:g})",
    )
    return parser


def _status(stack: Mapping[str, Any]) -> str:
    action = str(stack.get("stack_action") or "").upper()
    status = str(stack.get("stack_status") or "UNKNOWN").upper()
    if action and not status.startswith(f"{action}_"):
        return f"{action}_{status}"
    return status


def _exception_summary(exc: Exception) -> str:
    exc_type = getattr(exc, "exc_type", None)
    value = getattr(exc, "value", None)
    if value:
        return f"{exc_type}: {value}" if exc_type else str(value)
    if exc_type:
        return str(exc_type)
    return str(exc).splitlines()[0]


def _stack_details(
    rpc: Any,
    context: Any,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    result = rpc.show_stack(context, identity, resolve_outputs=False)
    if not result:
        raise RuntimeError("Heat returned no stack details")

    details = dict(result[0])
    details["id"] = identity.get("stack_id")
    details["project_id"] = identity.get("tenant")
    details["stack_status"] = _status(details)
    return details


def _show_stack(
    rpc: Any,
    context: Any,
    identity: Mapping[str, Any],
    stdout: TextIO,
) -> int:
    details = _stack_details(rpc, context, identity)
    json.dump(details, stdout, indent=2, sort_keys=True, default=str)
    stdout.write("\n")
    return 0


def _confirm_delete(
    stack_id: str,
    stack_name: str,
    project_id: str,
    *,
    yes: bool,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> bool:
    if yes:
        return True
    if not stdin.isatty():
        print("Refusing non-interactive deletion; pass --yes to confirm.", file=stderr)
        return False

    print(
        f"Delete Heat stack {stack_name!r} ({stack_id}) "
        f"from project {project_id}?",
        file=stdout,
    )
    print(f"Type the stack UUID {stack_id} to confirm:", end=" ", file=stdout, flush=True)
    return stdin.readline().strip() == stack_id


def _delete_stack(
    rpc: Any,
    read_context: Any,
    identity: Mapping[str, Any],
    *,
    delete_context: Any | None,
    prepare_delete_context: Callable[[], DeleteAuth] | None = None,
    yes: bool,
    timeout: float,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    stack_id = str(identity["stack_id"])
    stack_name = str(identity["stack_name"])
    project_id = str(identity["tenant"])

    try:
        current = _stack_details(rpc, read_context, identity)
    except Exception as exc:
        print(
            f"Unable to inspect stack before deletion: {_exception_summary(exc)}",
            file=stderr,
        )
        return 1

    print(
        f"Stack {stack_name!r} ({stack_id}) in project {project_id}: "
        f"{current['stack_status']}",
        file=stdout,
    )
    if not _confirm_delete(
        stack_id,
        stack_name,
        project_id,
        yes=yes,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    ):
        if stdin.isatty() and not yes:
            print("Confirmation did not match; stack was not deleted.", file=stderr)
        return 1

    delete_auth = DeleteAuth(delete_context) if delete_context is not None else None
    start_error: Exception | None = None
    if delete_auth is None and prepare_delete_context is not None:
        try:
            delete_auth = prepare_delete_context()
            if delete_auth.retained_grant_notice:
                print(delete_auth.retained_grant_notice, file=stdout)
        except Exception as exc:
            start_error = exc

    if delete_auth and delete_auth.context is not None and start_error is None:
        try:
            rpc.delete_stack(delete_auth.context, identity)
        except Exception as exc:
            start_error = exc
            if prepare_delete_context is not None and delete_context is not None:
                try:
                    after_initial_attempt = _stack_details(
                        rpc, read_context, identity
                    )
                except Exception as status_exc:
                    start_error = RuntimeError(
                        f"{_exception_summary(exc)}; unable to check whether "
                        f"Heat started deletion: {_exception_summary(status_exc)}"
                    )
                else:
                    after_status = after_initial_attempt["stack_status"]
                    if (
                        after_status.startswith("DELETE_")
                        and after_status != current["stack_status"]
                    ):
                        current = after_initial_attempt
                        start_error = None
                    elif after_status == current["stack_status"]:
                        try:
                            fallback_auth = prepare_delete_context()
                        except Exception as fallback_exc:
                            start_error = fallback_exc
                        else:
                            if fallback_auth.context is not delete_context:
                                delete_auth = fallback_auth
                                if delete_auth.retained_grant_notice:
                                    print(
                                        delete_auth.retained_grant_notice,
                                        file=stdout,
                                    )
                                try:
                                    rpc.delete_stack(delete_auth.context, identity)
                                except Exception as fallback_exc:
                                    start_error = fallback_exc
                                else:
                                    start_error = None

    if start_error is not None or delete_auth is None:
        exc = start_error or RuntimeError("unable to prepare Heat deletion context")
        reason = current.get("stack_status_reason") or "No status reason."
        print(
            f"Heat could not start stack deletion; current status is "
            f"{current['stack_status']} ({reason}): {_exception_summary(exc)}",
            file=stderr,
        )
        if delete_auth and delete_auth.retained_grant_notice:
            print(delete_auth.retained_grant_notice, file=stderr)
        return 1

    deadline = monotonic() + timeout
    last_status: str | None = None
    latest: dict[str, Any] = current
    while True:
        try:
            latest = _stack_details(rpc, read_context, identity)
        except Exception as exc:
            print(
                "Unable to read stack status after requesting deletion: "
                f"{_exception_summary(exc)}",
                file=stderr,
            )
            if delete_auth and delete_auth.retained_grant_notice:
                print(delete_auth.retained_grant_notice, file=stderr)
            return 1

        status = str(latest["stack_status"])
        if status != last_status:
            print(f"{stack_id}: {status}", file=stdout)
            last_status = status

        if status == "DELETE_COMPLETE":
            if delete_auth and delete_auth.cleanup:
                try:
                    delete_auth.cleanup()
                except Exception as exc:
                    print(
                        "Stack deletion completed, but the temporary project "
                        "admin role could not be removed: "
                        f"{_exception_summary(exc)}",
                        file=stderr,
                    )
                    return 1
                print("Temporary project admin role removed.", file=stdout)
            print(f"Stack {stack_id} deletion completed.", file=stdout)
            return 0
        if status in {"DELETE_FAILED", "DELETE_CANCELLED"}:
            reason = latest.get("stack_status_reason") or "Heat reported no reason."
            print(f"Stack {stack_id} deletion failed: {reason}", file=stderr)
            if delete_auth and delete_auth.retained_grant_notice:
                print(delete_auth.retained_grant_notice, file=stderr)
            return 1
        if monotonic() >= deadline:
            print(
                f"Timed out waiting for stack {stack_id}; latest status is {status}.",
                file=stderr,
            )
            if delete_auth and delete_auth.retained_grant_notice:
                print(delete_auth.retained_grant_notice, file=stderr)
            return 1
        sleep(POLL_INTERVAL)


def _initialize_heat_rpc() -> tuple[Any, Any]:
    from heat import version
    from heat.common import context as heat_context
    from heat.common import messaging
    from heat.rpc import client as rpc_client
    from oslo_config import cfg
    from oslo_log import log

    log.register_options(cfg.CONF)
    cfg.CONF(
        [],
        project="heat",
        prog="heat-admin-stack",
        version=version.version_info.version_string(),
    )
    log.setup(cfg.CONF, cfg.CONF.prog)
    messaging.setup()
    return rpc_client.EngineClient(), heat_context.get_admin_context(show_deleted=True)


def _context_from_access_info(
    access_info: Any,
    auth_url: str,
    context_class: Any,
    *,
    require_system_scope: bool = True,
    expected_project_id: str | None = None,
) -> Any:
    roles = list(access_info.role_names or [])
    if require_system_scope and not access_info.system_scoped:
        raise RuntimeError("stack deletion requires a system-scoped Keystone token")
    if expected_project_id and access_info.project_id != expected_project_id:
        raise RuntimeError("Keystone returned a token scoped to the wrong project")
    if "admin" not in roles:
        scope = "system-scoped" if access_info.system_scoped else "project-scoped"
        raise RuntimeError(f"stack deletion requires the {scope} admin role")

    # Heat's RequestContext serializes this token response into RPC messages.
    # keystoneauth exposes the response body as _data rather than a public
    # serialization method, so this is intentionally kept in this adapter.
    auth_token_info = getattr(access_info, "_data", None)
    if not isinstance(auth_token_info, Mapping) or not access_info.auth_token:
        raise RuntimeError("Keystone did not return a serializable auth context")

    return context_class(
        auth_token=access_info.auth_token,
        auth_token_info=auth_token_info,
        auth_url=auth_url,
        username=access_info.username,
        user_id=access_info.user_id,
        project_id=access_info.project_id,
        roles=roles,
        user_domain_id=access_info.user_domain_id,
        project_domain_id=access_info.project_domain_id,
        is_admin=True,
        show_deleted=True,
    )


def _initialize_system_admin_context(
    environ: Mapping[str, str] | None = None,
) -> SystemAdminAuth:
    from heat.common import context as heat_context
    from keystoneclient.v3 import client as keystone_client
    from keystoneauth1 import session as keystone_session
    from keystoneauth1.identity import v3

    environ = os.environ if environ is None else environ
    auth_url = environ.get("OS_AUTH_URL")
    if not auth_url:
        raise RuntimeError("set OS_AUTH_URL and system-admin credentials before deleting")

    system_scope = environ.get("OS_SYSTEM_SCOPE") or "all"
    token = environ.get("OS_TOKEN")
    username = environ.get("OS_USERNAME")
    password = environ.get("OS_PASSWORD")
    user_domain_id = environ.get("OS_USER_DOMAIN_ID")
    user_domain_name = environ.get("OS_USER_DOMAIN_NAME") or "Default"

    if token:
        auth = v3.Token(
            auth_url=auth_url,
            token=token,
            system_scope=system_scope,
        )
    else:
        if not username or not password:
            raise RuntimeError(
                "set OS_TOKEN or OS_USERNAME and OS_PASSWORD before deleting"
            )
        auth_options: dict[str, str] = {
            "auth_url": auth_url,
            "username": username,
            "password": password,
            "system_scope": system_scope,
        }
        if user_domain_id:
            auth_options["user_domain_id"] = user_domain_id
        else:
            auth_options["user_domain_name"] = user_domain_name or "Default"
        auth = v3.Password(**auth_options)

    if environ.get("OS_INSECURE", "").lower() in {"1", "true", "yes"}:
        verify: bool | str = False
    else:
        verify = environ.get("OS_CACERT") or True
    session = keystone_session.Session(auth=auth, verify=verify)
    access_info = auth.get_access(session)
    context = _context_from_access_info(
        access_info, auth_url, heat_context.RequestContext
    )
    identity = keystone_client.Client(session=session)
    return SystemAdminAuth(
        context=context,
        identity=identity,
        auth_url=auth_url,
        username=username or access_info.username,
        password=password,
        user_domain_id=user_domain_id or access_info.user_domain_id,
        user_domain_name=user_domain_name,
        verify=verify,
        user_id=access_info.user_id,
    )


def _project_scoped_admin_context(
    system_admin: SystemAdminAuth,
    project_id: str,
    *,
    context_class: Any | None = None,
) -> DeleteAuth:
    """Get Heat a project-scoped admin token to refresh its stored trust.

    Heat refreshes a stack's deferred credentials when the RPC caller has a
    different identity from the original stack owner. A system-scoped token
    is not accepted for Keystone trust creation, so a system admin needs an
    admin role in the owning project while Heat replaces the trust. Any role
    grant created here is recorded and removed after successful deletion.
    """
    from heat.common import context as heat_context
    from keystoneclient import exceptions as keystone_exceptions
    from keystoneauth1 import session as keystone_session
    from keystoneauth1.identity import v3

    try:
        system_admin.identity.projects.get(project_id)
    except keystone_exceptions.NotFound:
        # A missing project cannot produce a project-scoped token. The caller
        # can still try Heat's normal RPC delete using its system context; Heat
        # will report any stored-trust or resource-cleanup failure.
        print(
            f"Keystone project {project_id} no longer exists; attempting "
            "Heat's normal deletion path with the system-scoped context.",
            file=sys.stderr,
        )
        return DeleteAuth(system_admin.context)

    if not system_admin.username or not system_admin.password:
        raise RuntimeError(
            "cross-project deletion requires OS_USERNAME and OS_PASSWORD so "
            "Heat can receive a project-scoped token"
        )

    def authenticate_project_scope() -> Any:
        auth_options: dict[str, str] = {
            "auth_url": system_admin.auth_url,
            "username": system_admin.username or "",
            "password": system_admin.password or "",
            "project_id": project_id,
        }
        if system_admin.user_domain_id:
            auth_options["user_domain_id"] = system_admin.user_domain_id
        else:
            auth_options["user_domain_name"] = system_admin.user_domain_name
        auth = v3.Password(**auth_options)
        session = keystone_session.Session(auth=auth, verify=system_admin.verify)
        return auth.get_access(session)

    try:
        access_info = authenticate_project_scope()
    except Exception as exc:
        from keystoneauth1 import exceptions as keystone_exceptions

        if not isinstance(
            exc,
            (keystone_exceptions.Unauthorized, keystone_exceptions.Forbidden),
        ):
            raise
        access_info = None

    granted_admin_role = False
    role = None
    if access_info is None or "admin" not in list(access_info.role_names or []):
        admin_roles = list(system_admin.identity.roles.list(name="admin"))
        role = next((item for item in admin_roles if item.name == "admin"), None)
        if role is None:
            raise RuntimeError("Keystone has no project admin role to grant")
        system_admin.identity.roles.grant(
            role,
            user=system_admin.user_id,
            project=project_id,
        )
        granted_admin_role = True
        try:
            access_info = authenticate_project_scope()
        except Exception:
            # Do not leave an unnecessary grant when Keystone could not issue
            # the scoped token needed by Heat.
            system_admin.identity.roles.revoke(
                role,
                user=system_admin.user_id,
                project=project_id,
            )
            raise

    if access_info.project_id != project_id:
        if granted_admin_role and role is not None:
            system_admin.identity.roles.revoke(
                role,
                user=system_admin.user_id,
                project=project_id,
            )
        raise RuntimeError("Keystone returned a token scoped to the wrong project")
    if "admin" not in list(access_info.role_names or []):
        if granted_admin_role and role is not None:
            system_admin.identity.roles.revoke(
                role,
                user=system_admin.user_id,
                project=project_id,
            )
        raise RuntimeError("Keystone did not include the project admin role")

    if context_class is None:
        context_class = heat_context.RequestContext
    delete_context = _context_from_access_info(
        access_info,
        system_admin.auth_url,
        context_class,
        require_system_scope=False,
        expected_project_id=project_id,
    )
    cleanup = None
    notice = None
    if granted_admin_role and role is not None:
        cleanup = lambda: system_admin.identity.roles.revoke(
            role,
            user=system_admin.user_id,
            project=project_id,
        )
        notice = (
            f"Temporary project admin role granted to Keystone user "
            f"{system_admin.user_id} in project {project_id}; it will be "
            "removed after successful deletion. If this command fails or "
            "times out, remove it after cleanup with: openstack role remove "
            f"--user {system_admin.user_id} --project {project_id} admin"
        )
    else:
        notice = (
            f"Project admin role for Keystone user {system_admin.user_id} "
            f"in project {project_id} was already assigned and is unchanged. "
            "If it was left by an earlier failed attempt, remove it after "
            "cleanup with: openstack role remove --user "
            f"{system_admin.user_id} --project {project_id} admin"
        )
    return DeleteAuth(
        context=delete_context,
        cleanup=cleanup,
        retained_grant_notice=notice,
    )


def _run_rpc_command(
    argv: Sequence[str],
    *,
    rpc: Any | None = None,
    context: Any | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(list(argv))
    injected_rpc_context = rpc is not None and context is not None

    try:
        if rpc is None or context is None:
            rpc, context = _initialize_heat_rpc()
        identity = rpc.identify_stack(context, args.stack_id)
    except Exception as exc:
        print(
            f"Unable to resolve Heat stack {args.stack_id}: "
            f"{_exception_summary(exc)}",
            file=stderr,
        )
        return 1

    if args.command == "show":
        try:
            return _show_stack(rpc, context, identity, stdout)
        except Exception as exc:
            print(
                f"Unable to show Heat stack {args.stack_id}: "
                f"{_exception_summary(exc)}",
                file=stderr,
            )
            return 1

    delete_context = context if injected_rpc_context else None
    prepare_delete_context = None
    if not injected_rpc_context:
        try:
            system_admin = _initialize_system_admin_context()
        except Exception as exc:
            try:
                current = _stack_details(rpc, context, identity)
                print(
                    f"Heat status remains {current['stack_status']} "
                    f"({current.get('stack_status_reason') or 'No status reason.'}): "
                    f"{_exception_summary(exc)}",
                    file=stderr,
                )
            except Exception:
                print(
                    "Unable to create a system-admin Heat context: "
                    f"{_exception_summary(exc)}",
                    file=stderr,
                )
            return 1
        delete_context = system_admin.context
        prepare_delete_context = lambda: _project_scoped_admin_context(
            system_admin, str(identity["tenant"])
        )

    return _delete_stack(
        rpc,
        context,
        identity,
        delete_context=delete_context,
        prepare_delete_context=prepare_delete_context,
        yes=args.yes,
        timeout=args.timeout,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        sleep=sleep,
        monotonic=monotonic,
    )


def _run_in_container(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
    script_path: Path,
    stdin_isatty: bool,
) -> int:
    container_cli = environ.get(CONTAINER_CLI_ENV)
    if not container_cli:
        container_cli = shutil.which("docker") or shutil.which("podman")
    if not container_cli:
        print(
            "Docker or Podman is required to enter the Kolla Heat Engine container.",
            file=sys.stderr,
        )
        return 1

    container = environ.get(CONTAINER_ENV, DEFAULT_CONTAINER)
    command = [container_cli, "exec"]
    if stdin_isatty:
        command.append("--tty")
    command.extend(
        [
            "--interactive",
            "--env",
            f"{RPC_RUNTIME_ENV}=1",
        ]
    )
    for name in AUTH_ENV_NAMES:
        if name in environ:
            command.extend(["--env", name])
    command.extend(
        [
            container,
            "python",
            "-c",
            script_path.read_text(encoding="utf-8"),
            *argv,
        ]
    )
    try:
        result = runner(command, check=False)
    except FileNotFoundError as exc:
        print(f"Unable to start the container runtime: {exc}", file=sys.stderr)
        return 1
    return int(result.returncode)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    getuid: Callable[[], int] = os.geteuid,
    script_path: Path | None = None,
    stdin_isatty: bool | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ

    rpc_runtime = environ.get(RPC_RUNTIME_ENV) == "1"
    if not rpc_runtime and getuid() != 0:
        print("This operator command must be run as root.", file=sys.stderr)
        return 1

    if rpc_runtime:
        return _run_rpc_command(argv)

    if script_path is None:
        script_path = Path(__file__).resolve()
    if stdin_isatty is None:
        stdin_isatty = sys.stdin.isatty()
    return _run_in_container(
        argv,
        environ=environ,
        runner=runner,
        script_path=script_path,
        stdin_isatty=stdin_isatty,
    )


if __name__ == "__main__":
    raise SystemExit(main())
