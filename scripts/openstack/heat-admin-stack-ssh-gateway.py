#!/usr/bin/env python3
"""Restricted SSH forced-command gateway for scheduled Heat stack deletion.

Install this as the ``command=`` for a dedicated SSH key. It accepts only
``delete STACK_UUID`` through ``SSH_ORIGINAL_COMMAND`` and always enables the
Heat helper's guarded stale-trust recovery path.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Mapping


def main(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    if os.geteuid() != 0:
        print("The Heat cleanup SSH gateway must run as root.", file=sys.stderr)
        return 1

    try:
        command = shlex.split(environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError:
        command = []
    if len(command) != 2 or command[0] != "delete":
        print("Only 'delete STACK_UUID' is allowed.", file=sys.stderr)
        return 2

    try:
        stack_id = str(uuid.UUID(command[1]))
    except (ValueError, AttributeError, TypeError):
        print("Stack ID must be a UUID.", file=sys.stderr)
        return 2

    repository = Path(__file__).resolve().parents[2]
    openrc = repository / "config" / "admin-openrc.sh"
    helper = Path(__file__).with_name("heat-admin-stack.py")
    if not openrc.is_file() or not helper.is_file():
        print(
            "The Heat cleanup gateway is missing its OpenStack configuration "
            "or stack helper.",
            file=sys.stderr,
        )
        return 1

    command_line = (
        "set -e; set -a; "
        f"source {shlex.quote(str(openrc))}; "
        "set +a; "
        "exec "
        + shlex.join(
            [
                sys.executable,
                str(helper),
                "delete",
                stack_id,
                "--yes",
                "--recover-stale-credentials",
            ]
        )
    )
    result = subprocess.run(["bash", "-c", command_line], check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
