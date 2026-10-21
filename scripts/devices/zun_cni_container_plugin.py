#!/usr/bin/python3
"""Run the Zun CNI plugin inside the local Kolla zun_compute container."""

import os
import json
import subprocess
import sys


DOCKER = "/usr/bin/docker"
CONTAINER = "zun_compute"
PLUGIN = "/var/lib/kolla/venv/bin/zun-cni"


def main():
    if os.environ.get("CNI_COMMAND") == "VERSION":
        json.dump(
            {
                "cniVersion": "0.3.1",
                "supportedVersions": ["0.3.1"],
            },
            sys.stdout,
            separators=(",", ":"),
        )
        return 0

    env_args = []
    for name, value in sorted(os.environ.items()):
        if name.startswith("CNI_"):
            env_args.extend(("--env", f"{name}={value}"))

    command = [DOCKER, "exec", "--interactive", *env_args, CONTAINER, PLUGIN]
    try:
        result = subprocess.run(
            command,
            input=sys.stdin.buffer.read(),
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            check=False,
        )
    except OSError as error:
        print(f"unable to invoke the Zun CNI plugin: {error}", file=sys.stderr)
        return 127
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
