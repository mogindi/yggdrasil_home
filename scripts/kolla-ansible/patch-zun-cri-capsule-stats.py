#!/usr/bin/env python3
"""Read CPU utilization for Zun Capsule containers from the CRI runtime."""

from pathlib import Path
import glob


MARKER = "# yggdrasil-zun-cri-capsule-stats-v1"
IMPORT_ANCHOR = "import tenacity\n"
INIT_ANCHOR = "        self.image_stub = api_pb2_grpc.ImageServiceStub(compat_channel)\n"
METHOD_ANCHOR = "    def create_capsule(self, context, capsule, image, requested_networks,\n"
METHOD = '''    # yggdrasil-zun-cri-capsule-stats-v1
    def stats(self, context, container):
        """Return CPU utilization normalized to the container CPU request."""
        container_id = container.container_id
        if not container_id:
            return {}

        response = self.runtime_stub.ContainerStats(
            api_pb2.ContainerStatsRequest(container_id=container_id),
            timeout=30,
        )
        cpu_stats = response.stats.cpu
        usage_cores = None

        try:
            if cpu_stats.HasField("usage_nano_cores"):
                nano_cores = int(cpu_stats.usage_nano_cores.value)
                if nano_cores > 0:
                    usage_cores = nano_cores / 1_000_000_000.0
        except ValueError:
            pass

        try:
            has_cpu_time = cpu_stats.HasField("usage_core_nano_seconds")
        except ValueError:
            has_cpu_time = False
        if usage_cores is None and has_cpu_time and cpu_stats.timestamp > 0:
            cpu_time = int(cpu_stats.usage_core_nano_seconds.value)
            sample_time = int(cpu_stats.timestamp)
            with self._cpu_stats_lock:
                previous = self._cpu_stats_cache.get(container.uuid)
                self._cpu_stats_cache[container.uuid] = (cpu_time, sample_time)
                if len(self._cpu_stats_cache) > 10000:
                    self._cpu_stats_cache.clear()
                    self._cpu_stats_cache[container.uuid] = (cpu_time, sample_time)
            if previous:
                previous_cpu_time, previous_sample_time = previous
                elapsed = sample_time - previous_sample_time
                consumed = cpu_time - previous_cpu_time
                if elapsed > 0 and consumed >= 0:
                    usage_cores = consumed / float(elapsed)
            else:
                usage_cores = 0.0

        if usage_cores is None:
            return {}

        cpu_request = float(container.cpu or 0)
        if cpu_request <= 0:
            cpu_request = 1.0
        cpu_percent = usage_cores * 100.0 / cpu_request
        return {"CPU %": max(0.0, min(100.0, cpu_percent))}

'''
MANAGER_MARKER = "# yggdrasil-zun-capsule-stats-v1"
MANAGER_OLD = '''        try:
            # NOTE(hongbin): capsule shouldn't reach here
            return self.driver.stats(context, container)
'''
MANAGER_NEW = '''        try:
            # yggdrasil-zun-capsule-stats-v1
            return self._get_driver(container).stats(context, container)
'''


def candidate_paths() -> list[Path]:
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/container/cri/driver.py"
        )
    })


def manager_paths() -> list[Path]:
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/compute/manager.py"
        )
    })


def patch(path: Path) -> None:
    source = path.read_text()
    if MARKER in source:
        return
    if source.count(IMPORT_ANCHOR) != 1:
        raise RuntimeError(f"expected one CRI driver import insertion point in {path}")
    if source.count(INIT_ANCHOR) != 1:
        raise RuntimeError(f"expected one CRI driver initialization point in {path}")
    if source.count(METHOD_ANCHOR) != 1:
        raise RuntimeError(f"expected one Capsule creation method in {path}")

    source = source.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "import threading\n", 1)
    source = source.replace(
        INIT_ANCHOR,
        INIT_ANCHOR
        + "        self._cpu_stats_cache = {}\n"
        + "        self._cpu_stats_lock = threading.Lock()\n",
        1,
    )
    source = source.replace(METHOD_ANCHOR, METHOD + METHOD_ANCHOR, 1)
    path.write_text(source)


def patch_manager(path: Path) -> None:
    source = path.read_text()
    if MANAGER_MARKER in source:
        return
    if source.count(MANAGER_OLD) != 1:
        raise RuntimeError(
            f"expected one Zun container stats driver dispatch in {path}; "
            f"found {source.count(MANAGER_OLD)}"
        )
    path.write_text(source.replace(MANAGER_OLD, MANAGER_NEW, 1))


def main() -> None:
    paths = candidate_paths()
    if len(paths) != 1:
        raise RuntimeError(f"expected one Zun CRI driver, found {paths}")
    patch(paths[0])
    paths = manager_paths()
    if len(paths) != 1:
        raise RuntimeError(f"expected one Zun compute manager, found {paths}")
    patch_manager(paths[0])


if __name__ == "__main__":
    main()
