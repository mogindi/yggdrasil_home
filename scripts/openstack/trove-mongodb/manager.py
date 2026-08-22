"""Minimal current-style MongoDB guest-agent manager."""

from trove.guestagent.datastore import manager
from trove.guestagent.datastore import service as base_service
from trove.guestagent.datastore.experimental.mongodb import service


class Manager(manager.Manager):
    """Lifecycle-only MongoDB manager for the injected guest image."""

    def __init__(self):
        super(Manager, self).__init__("mongodb")
        self.status = base_service.BaseDbStatus(self.docker_client)
        self.app = service.MongoDbApp(self.status, self.docker_client)

    @property
    def configuration_manager(self):
        return None

    def do_prepare(self, context, packages, databases, memory_mb, users,
                   device_path, mount_point, backup_info,
                   config_contents, root_password, overrides,
                   cluster_config, snapshot, ds_version=None):
        if any((packages, databases, users, backup_info, cluster_config,
                snapshot, root_password)):
            raise RuntimeError(
                "The injected MongoDB adapter supports lifecycle operations "
                "only; databases, users, backups, and clusters are not "
                "implemented yet.")
        self.app.start_db(ds_version=ds_version)
