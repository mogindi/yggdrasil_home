#!/usr/bin/env python3
"""Expose aggregate CPU stats for a Zun Capsule's CRI containers."""

from pathlib import Path
import glob


MARKER = "# yggdrasil-zun-capsule-stats-v2"
PREVIOUS_MARKER = "# yggdrasil-zun-capsule-stats-v1"
ANCHOR = "    def _do_get_one(self, capsule_ident, legacy_api_version=False):"
CUSTOM_ACTIONS_ANCHOR = "    _custom_actions = {\n"
CUSTOM_ACTION = "        'stats': ['GET'],\n"
METHOD = '''    # yggdrasil-zun-capsule-stats-v1
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def stats(self, capsule_ident, **kwargs):
        """Return mean CPU utilization across the running Capsule containers."""
        context = pecan.request.context
        if utils.is_all_projects(kwargs):
            policy.enforce(
                context,
                "capsule:get_one_all_projects",
                action="capsule:get_one_all_projects",
            )
            context.all_projects = True

        capsule = api_utils.get_resource('Capsule', capsule_ident)
        check_policy_on_capsule(capsule.as_dict(), "capsule:get")
        compute_api = pecan.request.compute_api
        weighted_cpu = 0.0
        total_cpu = 0.0

        for container in capsule.containers or []:
            if container.status != consts.RUNNING:
                continue
            if not container.host:
                container.host = capsule.host
            if not container.host or not container.container_id:
                continue

            try:
                stats = compute_api.container_stats(context, container)
            except Exception as error:
                LOG.warning(
                    "Could not poll CPU for Capsule container %s: %s",
                    container.uuid,
                    error,
                )
                continue

            try:
                cpu_percent = float(str(stats.get('CPU %')).strip().rstrip('%'))
            except (AttributeError, TypeError, ValueError):
                continue
            cpu_request = float(container.cpu or 0)
            if cpu_request <= 0:
                cpu_request = 1.0
            weighted_cpu += cpu_percent * cpu_request
            total_cpu += cpu_request

        if total_cpu <= 0:
            return {}
        return {"CPU %": weighted_cpu / total_cpu}

'''


def candidate_paths() -> list[Path]:
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/api/controllers/v1/capsules.py"
        )
    })


def patch(path: Path) -> None:
    source = path.read_text()
    if MARKER in source and CUSTOM_ACTION in source:
        return

    if CUSTOM_ACTION not in source:
        if source.count(CUSTOM_ACTIONS_ANCHOR) != 1:
            raise RuntimeError(
                f"expected one CapsuleController custom action map in {path}; "
                f"found {source.count(CUSTOM_ACTIONS_ANCHOR)}"
            )
        source = source.replace(
            CUSTOM_ACTIONS_ANCHOR,
            CUSTOM_ACTIONS_ANCHOR + CUSTOM_ACTION,
            1,
        )

    if PREVIOUS_MARKER in source:
        source = source.replace(PREVIOUS_MARKER, MARKER, 1)
    elif MARKER not in source:
        if source.count(ANCHOR) != 1:
            raise RuntimeError(
                f"expected one CapsuleController insertion point in {path}; "
                f"found {source.count(ANCHOR)}"
            )
        source = source.replace(ANCHOR, METHOD + ANCHOR, 1)

    path.write_text(source)


def main() -> None:
    paths = candidate_paths()
    if len(paths) != 1:
        raise RuntimeError(f"expected one Zun capsules controller, found {paths}")
    patch(paths[0])


if __name__ == "__main__":
    main()
