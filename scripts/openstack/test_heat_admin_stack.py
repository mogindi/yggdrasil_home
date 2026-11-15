"""Unit tests for the root-only Heat stack operator command."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("heat-admin-stack.py")
SPEC = importlib.util.spec_from_file_location("heat_admin_stack", SCRIPT)
assert SPEC and SPEC.loader
admin_stack = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin_stack
SPEC.loader.exec_module(admin_stack)


STACK_ID = "aad0e9d2-ac8d-4094-a484-6c09409e4b8f"
PROJECT_ID = "88a8b0838f534961a703aa0dd42e7536"
STACK_NAME = "cicd-real-1791996511868"


def stack_details(action: str, status: str, reason: str = ""):
    return {
        "stack_action": action,
        "stack_status": status,
        "stack_status_reason": reason,
        "stack_name": STACK_NAME,
        "parameters": {},
    }


class FakeRPC:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.identified = []
        self.deleted = []

    def identify_stack(self, context, stack_id):
        self.identified.append((context, stack_id))
        return {
            "tenant": PROJECT_ID,
            "stack_name": STACK_NAME,
            "stack_id": stack_id,
            "path": "",
        }

    def show_stack(self, context, identity, resolve_outputs=True):
        if len(self.statuses) > 1:
            item = self.statuses.pop(0)
        else:
            item = self.statuses[0]
        return [item]

    def delete_stack(self, context, identity):
        self.deleted.append((context, identity))


class TTYInput(io.StringIO):
    def isatty(self):
        return True


class RemoteFailure(Exception):
    exc_type = "Unauthorized"
    value = "stored stack credentials received HTTP 401"
    traceback = "internal traceback must not be shown"


class ShowTests(unittest.TestCase):
    def test_show_includes_global_stack_identity_and_combined_status(self):
        rpc = FakeRPC([stack_details("CREATE", "FAILED", "Timed out")])
        output = io.StringIO()
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["show", STACK_ID],
            rpc=rpc,
            context=object(),
            stdout=output,
            stderr=errors,
        )

        self.assertEqual(result, 0)
        self.assertEqual(errors.getvalue(), "")
        details = json.loads(output.getvalue())
        self.assertEqual(details["id"], STACK_ID)
        self.assertEqual(details["project_id"], PROJECT_ID)
        self.assertEqual(details["stack_name"], STACK_NAME)
        self.assertEqual(details["stack_status"], "CREATE_FAILED")
        self.assertEqual(details["stack_status_reason"], "Timed out")


class DeleteTests(unittest.TestCase):
    def test_delete_requires_typed_uuid_when_interactive(self):
        rpc = FakeRPC([stack_details("CREATE", "FAILED")])
        output = io.StringIO()
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID],
            rpc=rpc,
            context=object(),
            stdin=TTYInput("wrong-id\n"),
            stdout=output,
            stderr=errors,
        )

        self.assertEqual(result, 1)
        self.assertEqual(rpc.deleted, [])
        self.assertIn("Confirmation did not match", errors.getvalue())

    def test_delete_refuses_noninteractive_without_yes(self):
        rpc = FakeRPC([stack_details("CREATE", "FAILED")])
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID],
            rpc=rpc,
            context=object(),
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=errors,
        )

        self.assertEqual(result, 1)
        self.assertEqual(rpc.deleted, [])
        self.assertIn("pass --yes", errors.getvalue())

    def test_delete_uses_normal_heat_delete_and_waits_for_complete(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED", "Timed out"),
                stack_details("DELETE", "IN_PROGRESS"),
                stack_details("DELETE", "COMPLETE"),
            ]
        )
        output = io.StringIO()
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID, "--yes"],
            rpc=rpc,
            context="admin-context",
            stdout=output,
            stderr=errors,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(rpc.deleted), 1)
        self.assertIs(rpc.deleted[0][0], "admin-context")
        self.assertEqual(rpc.deleted[0][1]["stack_id"], STACK_ID)
        self.assertIn("DELETE_COMPLETE", output.getvalue())
        self.assertIn("deletion completed", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_delete_reports_heat_cleanup_failure_and_preserves_stack(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "FAILED", "Trust no longer exists"),
            ]
        )
        output = io.StringIO()
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID, "--yes"],
            rpc=rpc,
            context=object(),
            stdout=output,
            stderr=errors,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(rpc.deleted), 1)
        self.assertIn("Trust no longer exists", errors.getvalue())

    def test_delete_rpc_error_is_reported_without_remote_traceback(self):
        rpc = FakeRPC([stack_details("CREATE", "FAILED", "Timed out")])
        rpc.delete_stack = mock.Mock(side_effect=RemoteFailure())
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID, "--yes"],
            rpc=rpc,
            context=object(),
            stdout=io.StringIO(),
            stderr=errors,
        )

        self.assertEqual(result, 1)
        self.assertIn("CREATE_FAILED (Timed out)", errors.getvalue())
        self.assertIn(
            "Unauthorized: stored stack credentials received HTTP 401",
            errors.getvalue(),
        )
        self.assertNotIn("internal traceback", errors.getvalue())

    def test_delete_reports_timeout_with_latest_status(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "IN_PROGRESS"),
            ]
        )
        clock = iter([0.0, 2.0])
        errors = io.StringIO()

        result = admin_stack._run_rpc_command(
            ["delete", STACK_ID, "--yes", "--timeout", "1"],
            rpc=rpc,
            context=object(),
            stdout=io.StringIO(),
            stderr=errors,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(clock),
        )

        self.assertEqual(result, 1)
        self.assertIn("latest status is DELETE_IN_PROGRESS", errors.getvalue())

    def test_temporary_project_admin_role_is_removed_after_complete(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "COMPLETE"),
            ]
        )
        cleanup = mock.Mock()
        output = io.StringIO()
        errors = io.StringIO()

        result = admin_stack._delete_stack(
            rpc,
            object(),
            {
                "stack_id": STACK_ID,
                "stack_name": STACK_NAME,
                "tenant": PROJECT_ID,
            },
            delete_context=None,
            prepare_delete_context=lambda: admin_stack.DeleteAuth(
                context="project-admin",
                cleanup=cleanup,
                retained_grant_notice="Temporary role remains until deletion completes.",
            ),
            yes=True,
            timeout=10,
            stdin=io.StringIO(),
            stdout=output,
            stderr=errors,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 0)
        cleanup.assert_called_once_with()
        self.assertEqual(rpc.deleted[0][0], "project-admin")
        self.assertIn("Temporary project admin role removed", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_project_scope_fallback_runs_after_system_scope_delete_fails(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "COMPLETE"),
            ]
        )
        rpc.delete_stack = mock.Mock(side_effect=[RemoteFailure(), None])
        prepare = mock.Mock(
            return_value=admin_stack.DeleteAuth(context="project-admin")
        )

        result = admin_stack._delete_stack(
            rpc,
            object(),
            {
                "stack_id": STACK_ID,
                "stack_name": STACK_NAME,
                "tenant": PROJECT_ID,
            },
            delete_context="system-admin",
            prepare_delete_context=prepare,
            yes=True,
            timeout=10,
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 0)
        prepare.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in rpc.delete_stack.call_args_list],
            ["system-admin", "project-admin"],
        )

    def test_project_scope_fallback_is_not_used_when_system_delete_works(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "COMPLETE"),
            ]
        )
        prepare = mock.Mock()

        result = admin_stack._delete_stack(
            rpc,
            object(),
            {
                "stack_id": STACK_ID,
                "stack_name": STACK_NAME,
                "tenant": PROJECT_ID,
            },
            delete_context="system-admin",
            prepare_delete_context=prepare,
            yes=True,
            timeout=10,
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 0)
        prepare.assert_not_called()
        self.assertEqual(rpc.deleted[0][0], "system-admin")

    def test_temporary_project_admin_role_is_retained_on_cleanup_failure(self):
        rpc = FakeRPC(
            [
                stack_details("CREATE", "FAILED"),
                stack_details("DELETE", "FAILED", "Resource cleanup failed"),
            ]
        )
        cleanup = mock.Mock()
        errors = io.StringIO()

        result = admin_stack._delete_stack(
            rpc,
            object(),
            {
                "stack_id": STACK_ID,
                "stack_name": STACK_NAME,
                "tenant": PROJECT_ID,
            },
            delete_context=None,
            prepare_delete_context=lambda: admin_stack.DeleteAuth(
                context="project-admin",
                cleanup=cleanup,
                retained_grant_notice="Temporary role remains for retry.",
            ),
            yes=True,
            timeout=10,
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=errors,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )

        self.assertEqual(result, 1)
        cleanup.assert_not_called()
        self.assertIn("Resource cleanup failed", errors.getvalue())
        self.assertIn("Temporary role remains for retry", errors.getvalue())

    def test_cancelled_confirmation_does_not_prepare_project_role(self):
        rpc = FakeRPC([stack_details("CREATE", "FAILED")])
        prepare = mock.Mock()

        result = admin_stack._delete_stack(
            rpc,
            object(),
            {
                "stack_id": STACK_ID,
                "stack_name": STACK_NAME,
                "tenant": PROJECT_ID,
            },
            delete_context=None,
            prepare_delete_context=prepare,
            yes=False,
            timeout=10,
            stdin=TTYInput("wrong-id\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        self.assertEqual(result, 1)
        prepare.assert_not_called()
        self.assertEqual(rpc.deleted, [])


class LauncherTests(unittest.TestCase):
    def test_launcher_requires_root_before_invoking_container_runtime(self):
        runner = mock.Mock()
        errors = io.StringIO()

        with mock.patch("sys.stderr", errors):
            result = admin_stack.main(
                ["show", STACK_ID],
                environ={},
                runner=runner,
                getuid=lambda: 1000,
                script_path=SCRIPT,
                stdin_isatty=False,
            )

        self.assertEqual(result, 1)
        runner.assert_not_called()
        self.assertIn("must be run as root", errors.getvalue())

    def test_launcher_runs_rpc_code_as_the_heat_container_user(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))

        result = admin_stack.main(
            ["show", STACK_ID],
            environ={admin_stack.CONTAINER_CLI_ENV: "docker"},
            runner=runner,
            getuid=lambda: 0,
            script_path=SCRIPT,
            stdin_isatty=False,
        )

        self.assertEqual(result, 0)
        command = runner.call_args.args[0]
        self.assertNotIn("--user", command)
        self.assertIn("heat_engine", command)
        self.assertEqual(command[-2], "show")
        self.assertEqual(command[-1], STACK_ID)

    def test_launcher_forwards_auth_environment_names_not_secret_values(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        password = "temporary-secret-value"

        result = admin_stack.main(
            ["delete", STACK_ID, "--yes"],
            environ={
                admin_stack.CONTAINER_CLI_ENV: "docker",
                "OS_AUTH_URL": "https://identity.example/v3",
                "OS_PASSWORD": password,
                "OS_SYSTEM_SCOPE": "all",
            },
            runner=runner,
            getuid=lambda: 0,
            script_path=SCRIPT,
            stdin_isatty=False,
        )

        self.assertEqual(result, 0)
        command = runner.call_args.args[0]
        self.assertIn("OS_PASSWORD", command)
        self.assertIn("OS_SYSTEM_SCOPE", command)
        self.assertNotIn(password, command)


class AuthContextTests(unittest.TestCase):
    def test_context_requires_system_scoped_admin_and_carries_token_data(self):
        access = SimpleNamespace(
            system_scoped=True,
            role_names=["admin", "reader"],
            _data={"token": {"system": {"all": True}}},
            auth_token="test-token",
            username="admin",
            user_id="user-id",
            project_id=None,
            user_domain_id="domain-id",
            project_domain_id=None,
        )

        context = admin_stack._context_from_access_info(
            access,
            "https://identity.example/v3",
            lambda **values: values,
        )

        self.assertEqual(context["auth_token"], "test-token")
        self.assertEqual(context["auth_token_info"], access._data)
        self.assertTrue(context["is_admin"])
        self.assertEqual(context["project_id"], None)

    def test_context_accepts_admin_token_scoped_to_target_project(self):
        access = SimpleNamespace(
            system_scoped=False,
            role_names=["admin", "member"],
            _data={"token": {"project": {"id": PROJECT_ID}}},
            auth_token="test-token",
            username="admin",
            user_id="user-id",
            project_id=PROJECT_ID,
            user_domain_id="domain-id",
            project_domain_id="project-domain-id",
        )

        context = admin_stack._context_from_access_info(
            access,
            "https://identity.example/v3",
            lambda **values: values,
            require_system_scope=False,
            expected_project_id=PROJECT_ID,
        )

        self.assertEqual(context["project_id"], PROJECT_ID)
        self.assertTrue(context["is_admin"])

    def test_context_rejects_non_system_scope_and_missing_admin_role(self):
        access = SimpleNamespace(
            system_scoped=False,
            role_names=["admin"],
            _data={"token": {}},
            auth_token="test-token",
        )
        with self.assertRaisesRegex(RuntimeError, "system-scoped"):
            admin_stack._context_from_access_info(access, "url", dict)

        access.system_scoped = True
        access.role_names = ["reader"]
        with self.assertRaisesRegex(RuntimeError, "admin role"):
            admin_stack._context_from_access_info(access, "url", dict)


if __name__ == "__main__":
    unittest.main()
