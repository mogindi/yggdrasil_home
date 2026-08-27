#!/usr/bin/env python3
"""Run the Freezer scheduler while restricting it to one registered client.

Freezer's scheduler API query returns all active jobs visible to the project.
The upstream scheduler relies on the agent's capability list, but does not
filter by client_id. This entry point keeps each HA scheduler instance bound
to its configured client.
"""

import os
import sys

from keystoneauth1 import identity
from keystoneauth1 import session
from oslo_config import cfg

from freezerclient import utils as client_utils
from freezerclient.v2 import client as freezer_client_v2
from freezer.scheduler import freezer_scheduler
from freezer.scheduler import utils


# The scheduler launches freezer-agent as a child process. Keep the same
# virtualenv/bin directory discoverable for self-managed installations.
_scheduler_bin = os.path.dirname(sys.executable)
os.environ["PATH"] = os.pathsep.join(
    path for path in (_scheduler_bin, os.environ.get("PATH", "")) if path
)


_upstream_get_active_jobs = utils.get_active_jobs_from_api
_upstream_get_client_instance = client_utils.get_client_instance


def _application_credentials_present():
    return bool(
        os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
        and os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    )


def _option(opts, name, default=None):
    value = getattr(opts, name, None)
    if value is not None:
        return value
    config = getattr(opts, "opts", None)
    if config is not None:
        value = getattr(config, name, None)
        if value is not None:
            return value
    return default


def get_client_instance(opts=None, api_version=None):
    if not _application_credentials_present():
        return _upstream_get_client_instance(opts=opts, api_version=api_version)
    if api_version == "1":
        return _upstream_get_client_instance(opts=opts, api_version=api_version)

    auth_url = _option(opts, "os_auth_url")
    insecure = bool(_option(opts, "insecure", False))
    auth = identity.V3ApplicationCredential(
        auth_url=auth_url,
        application_credential_id=os.environ["OS_APPLICATION_CREDENTIAL_ID"],
        application_credential_secret=os.environ[
            "OS_APPLICATION_CREDENTIAL_SECRET"
        ],
    )
    auth_session = session.Session(
        auth=auth,
        verify=_option(opts, "os_cacert") if not insecure else False,
        cert=_option(opts, "os_cert"),
    )
    client_opts = client_utils.Namespace(
        {
            name: _option(opts, name)
            for name in (
                "os_auth_url",
                "os_backup_url",
                "os_endpoint_type",
                "os_project_id",
                "os_project_name",
                "os_project_domain_id",
                "os_project_domain_name",
                "os_user_domain_id",
                "os_user_domain_name",
                "os_cacert",
                "os_cert",
            )
        }
    )
    client_opts.insecure = insecure
    return freezer_client_v2.Client(opts=client_opts, session=auth_session)


client_utils.get_client_instance = get_client_instance


def _configured_client_id():
    client_id = os.environ.get("FREEZER_SCHEDULER_CLIENT_ID")
    if client_id:
        return client_id

    # Kolla's invocation has parsed freezer.conf by the time the scheduler
    # asks for jobs. The guard also supports older Freezer argument sets.
    try:
        return cfg.CONF.client_id
    except Exception:
        return ""


def get_active_jobs_from_api(api_client):
    jobs = _upstream_get_active_jobs(api_client)
    client_id = _configured_client_id()
    if not client_id:
        return jobs
    return [job for job in jobs if job.get("client_id") == client_id]


utils.get_active_jobs_from_api = get_active_jobs_from_api


def _ensure_safe_scheduler_defaults():
    arguments = sys.argv[1:]
    if not any(
        argument == "--disable-exec" or argument.startswith("--disable-exec=")
        for argument in arguments
    ) and "--nodisable-exec" not in arguments:
        sys.argv.insert(1, "--disable-exec")
    if not any(
        argument == "--capabilities-supported-actions"
        or argument.startswith("--capabilities-supported-actions=")
        for argument in arguments
    ):
        sys.argv.insert(
            1,
            "--capabilities-supported-actions=backup,restore,info,admin",
        )
    for option, value in (
        ("modes", "fs,mongo,mysql,sqlserver"),
        ("storages", "local,swift,ssh,s3,ftp,ftps"),
        ("engines", "tar,rsync,rsyncv2"),
    ):
        option_name = f"--capabilities-supported-{option}"
        if not any(
            argument == option_name or argument.startswith(f"{option_name}=")
            for argument in sys.argv[1:]
        ):
            sys.argv.insert(1, f"{option_name}={value}")


if __name__ == "__main__":
    _ensure_safe_scheduler_defaults()
    raise SystemExit(freezer_scheduler.main() or 0)
