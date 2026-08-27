#!/usr/bin/env python3
"""Freezer agent entry point with optional Keystone application credentials."""

import os

import swiftclient

from freezer.common import client_manager
from freezer.common import config as freezer_config
from freezer.main import main
from freezer.openstack import osclients


_upstream_get_client_manager = client_manager.get_client_manager
_upstream_get_backup_args = freezer_config.get_backup_args

_STORAGE_ENVIRONMENT = {
    "access_key": ("FREEZER_ACCESS_KEY", "ACCESS_KEY"),
    "secret_key": ("FREEZER_SECRET_KEY", "SECRET_KEY"),
    "endpoint": ("FREEZER_ENDPOINT", "ENDPOINT"),
    "ssh_key": ("FREEZER_SSH_KEY",),
    "ssh_password": ("FREEZER_SSH_PASSWORD",),
    "ssh_username": ("FREEZER_SSH_USERNAME",),
    "ssh_host": ("FREEZER_SSH_HOST",),
    "ssh_port": ("FREEZER_SSH_PORT",),
    "ftp_password": ("FREEZER_FTP_PASSWORD",),
    "ftp_username": ("FREEZER_FTP_USERNAME",),
    "ftp_host": ("FREEZER_FTP_HOST",),
    "ftp_port": ("FREEZER_FTP_PORT",),
    "ftp_keyfile": ("FREEZER_FTP_KEYFILE",),
    "ftp_certfile": ("FREEZER_FTP_CERTFILE",),
}


def _application_credentials_present():
    return bool(
        os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
        and os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    )


def _endpoint_interface(manager):
    interface = manager.client_kwargs.get("interface")
    if interface:
        return interface
    endpoint_type = manager.swift_args.get("endpoint_type", "publicURL")
    return endpoint_type[:-3] if endpoint_type.endswith("URL") else endpoint_type


def get_backup_args():
    backup_args = _upstream_get_backup_args()
    for name, environment_names in _STORAGE_ENVIRONMENT.items():
        for environment_name in environment_names:
            value = os.environ.get(environment_name)
            if value:
                setattr(backup_args, name, value)
                break
    return backup_args


class ApplicationCredentialClientManager(osclients.OSClientManager):
    """Use a Keystone session for Swift because Swiftclient has no username."""

    def create_swift(self):
        region_name = self.swift_args.get("region_name")
        endpoint = self.sess.get_endpoint(
            service_type="object-store",
            interface=_endpoint_interface(self),
            region_name=region_name,
        )
        token = self.sess.get_token()
        os_options = {
            "project_id": self.swift_args.get("project_id"),
            "region_name": region_name,
        }
        self.swift = swiftclient.client.Connection(
            preauthurl=endpoint,
            preauthtoken=token,
            insecure=self.swift_args.get("insecure", False),
            cacert=self.swift_args.get("cacert"),
            os_options=os_options,
        )
        if self.dry_run:
            self.swift = osclients.DryRunSwiftclientConnectionWrapper(self.swift)
        return self.swift


def get_client_manager(backup_args):
    if not _application_credentials_present():
        return _upstream_get_client_manager(backup_args)

    options = {
        "application_credential_id": os.environ["OS_APPLICATION_CREDENTIAL_ID"],
        "application_credential_secret": os.environ[
            "OS_APPLICATION_CREDENTIAL_SECRET"
        ],
        "project_id": os.environ.get("OS_PROJECT_ID")
        or backup_args.get("project_id"),
        "region_name": os.environ.get("OS_REGION_NAME"),
        "endpoint_type": os.environ.get("OS_ENDPOINT_TYPE", "internalURL"),
        "interface": os.environ.get("OS_INTERFACE"),
    }
    if os.environ.get("OS_CACERT"):
        options["cacert"] = os.environ["OS_CACERT"]
    else:
        options["insecure"] = True

    return ApplicationCredentialClientManager(
        auth_url=os.environ.get("OS_AUTH_URL"),
        auth_method="v3applicationcredential",
        dry_run=backup_args.get("dry_run"),
        **options,
    )


client_manager.get_client_manager = get_client_manager
freezer_config.get_backup_args = get_backup_args


if __name__ == "__main__":
    raise SystemExit(main() or 0)
