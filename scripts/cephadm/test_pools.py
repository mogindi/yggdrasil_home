"""Tests for the idempotent Ceph pool bootstrap script."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("pools.sh")


class GnocchiPoolTests(unittest.TestCase):
    def test_pool_script_contains_gnocchi_configuration(self):
        script = SCRIPT.read_text()

        self.assertIn('gnocchi_pool="${CEPH_GNOCCHI_POOL_NAME:-gnocchi}"', script)
        self.assertIn('ensure_pool "$gnocchi_pool"', script)
        self.assertIn('CEPH_GNOCCHI_POOL_SIZE', script)
        self.assertIn('CEPH_GNOCCHI_POOL_MIN_SIZE', script)
        self.assertIn('cephadm_pools_v2.done', script)

    def test_pool_script_creates_and_replica_configures_gnocchi_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "ceph-state.json"
            marker = root / "pools.done"
            fake_cephadm = root / "cephadm"
            fake_cephadm.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_CEPH_STATE"]
try:
    with open(state_path) as stream:
        state = json.load(stream)
except FileNotFoundError:
    state = {}

command = sys.argv[5:]
if command[:4] == ["ceph", "osd", "pool", "get"]:
    pool = command[4]
    setting = command[5]
    if pool not in state:
        raise SystemExit(1)
    print(f"{setting}: {state[pool].get(setting, 1)}")
elif command[:4] == ["ceph", "osd", "pool", "create"]:
    pool = command[4]
    state.setdefault(pool, {"size": 1, "min_size": 1})
elif command[:4] == ["ceph", "osd", "pool", "set"]:
    pool = command[4]
    state.setdefault(pool, {})[command[5]] = int(command[6])

with open(state_path, "w") as stream:
    json.dump(state, stream)
"""
            )
            fake_cephadm.chmod(
                fake_cephadm.stat().st_mode | stat.S_IXUSR
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{root}:{environment['PATH']}",
                    "CEPHADM_IMAGE": "test-image",
                    "CEPHADM_POOLS_MARKER": str(marker),
                    "CEPH_GNOCCHI_POOL_NAME": "gnocchi",
                    "CEPH_GNOCCHI_POOL_SIZE": "2",
                    "CEPH_GNOCCHI_POOL_MIN_SIZE": "1",
                    "FAKE_CEPH_STATE": str(state_file),
                }
            )

            subprocess.run(
                ["bash", str(SCRIPT)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            state = json.loads(state_file.read_text())
            self.assertEqual(state["gnocchi"]["size"], 2)
            self.assertEqual(state["gnocchi"]["min_size"], 1)
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
