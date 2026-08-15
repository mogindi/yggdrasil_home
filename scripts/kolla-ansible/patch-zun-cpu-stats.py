#!/usr/bin/env python3
"""Make Zun report interval CPU usage from Docker's stats counters.

Docker exposes cumulative container and host CPU counters in ``cpu_stats``.
The previous Zun implementation divided those counters directly, which made
the reported percentage shrink as a container's uptime increased. Docker's
own stats client calculates a percentage from the ``precpu_stats`` interval
and the number of online CPUs; use the same calculation here.
"""

from pathlib import Path
import glob
import sys


OLD = """            cpu_usage = res['cpu_stats']['cpu_usage']['total_usage']
            system_cpu_usage = res['cpu_stats']['system_cpu_usage']
            cpu_percent = float(cpu_usage) / float(system_cpu_usage) * 100
"""

NEW = """            cpu_stats = res['cpu_stats']
            precpu_stats = res.get('precpu_stats') or {}
            cpu_usage = cpu_stats['cpu_usage']['total_usage']
            previous_cpu_usage = precpu_stats.get(
                'cpu_usage', {}).get('total_usage', 0)
            system_cpu_usage = cpu_stats.get('system_cpu_usage', 0)
            previous_system_cpu_usage = precpu_stats.get(
                'system_cpu_usage', 0)
            cpu_delta = float(cpu_usage) - float(previous_cpu_usage)
            system_delta = (
                float(system_cpu_usage) - float(previous_system_cpu_usage))
            online_cpus = cpu_stats.get('online_cpus')
            if not online_cpus:
                online_cpus = len(
                    cpu_stats['cpu_usage'].get('percpu_usage') or [])
            if system_delta > 0 and cpu_delta >= 0 and online_cpus:
                cpu_percent = (
                    cpu_delta / system_delta * float(online_cpus) * 100)
            else:
                # The first Docker sample has no previous interval.
                cpu_percent = 0.0
"""


def candidate_paths() -> list[Path]:
    if len(sys.argv) > 1:
        return [Path(argument) for argument in sys.argv[1:]]
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/container/docker/driver.py"
        )
    })


def patch(path: Path) -> None:
    source = path.read_text()
    if NEW in source and OLD not in source:
        return
    if source.count(OLD) != 1:
        raise RuntimeError(
            f"expected exactly one unpatched Zun CPU calculation in {path}, "
            f"found {source.count(OLD)}"
        )
    path.write_text(source.replace(OLD, NEW))


def main() -> None:
    paths = candidate_paths()
    if len(paths) != 1:
        raise RuntimeError(
            f"expected one Zun Docker driver module, found {len(paths)}: "
            f"{paths}"
        )
    patch(paths[0])


if __name__ == "__main__":
    main()
