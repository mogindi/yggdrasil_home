"""MongoDB guest-agent manager injected into the Trove guest image."""

from oslo_service import periodic_task

from trove.guestagent.datastore import manager
from trove.guestagent.datastore import service as base_service
from trove.guestagent.datastore.experimental.mongodb import service
from trove.instance import service_status


class Manager(manager.Manager):
    """Lifecycle, configuration, and MongoDB topology operations."""

    def __init__(self):
        super(Manager, self).__init__("mongodb")
        self.status = base_service.BaseDbStatus(self.docker_client)
        self.app = service.MongoDbApp(self.status, self.docker_client)

    @periodic_task.periodic_task(spacing=30)
    def update_status(self, context):
        """Keep a clustered guest alive while Trove assembles its topology.

        Trove marks clustered guests ``INSTANCE_READY`` after the local
        database container is healthy, but intentionally delays the normal
        prepare marker until the task manager has configured the complete
        topology.  BaseDbStatus therefore suppresses periodic heartbeats in
        that interval.  A sharded build on small guests can legitimately
        take longer than Trove's 90-second heartbeat expiry, so renew the
        existing transitional status without advertising the database as
        fully installed before ``cluster_complete`` runs.
        """
        if not self.status.is_installed:
            if (self.app.cluster_config and
                    self.status.status ==
                    service_status.ServiceStatuses.INSTANCE_READY):
                self.status.set_status(
                    service_status.ServiceStatuses.INSTANCE_READY,
                    force=True)
            return

        super(Manager, self).update_status(context)

    @property
    def configuration_manager(self):
        return self.app.configuration_manager

    def do_prepare(self, context, packages, databases, memory_mb, users,
                   device_path, mount_point, backup_info,
                   config_contents, root_password, overrides,
                   cluster_config, snapshot, ds_version=None):
        if any((packages, databases, users, backup_info, snapshot,
                root_password)):
            raise RuntimeError(
                "The injected MongoDB adapter does not yet support packages, "
                "database/user provisioning, backups, or restore snapshots.")
        self.app.prepare_configuration(config_contents, memory_mb=memory_mb)
        self.app.configure_cluster(cluster_config)
        self.app.update_overrides(overrides)
        self.app.start_db(ds_version=ds_version)

    def apply_overrides(self, context, overrides):
        self.app.apply_overrides(overrides)

    def initialize_replica_set(self, context, members):
        return self.app.initialize_replica_set(members)

    def prep_primary(self, context):
        return self.app.prep_primary()

    def add_members(self, context, members):
        return self.app.add_members(members)

    def add_replica_members(self, context, members):
        return self.app.add_replica_members(members)

    def remove_replica_members(self, context, members):
        return self.app.remove_replica_members(members)

    def replica_set_status(self, context):
        return self.app.replica_set_status()

    def add_config_servers(self, context, config_servers):
        return self.app.add_config_servers(config_servers)

    def add_shard(self, context, replica_set_name, replica_set_member):
        return self.app.add_shard(replica_set_name, replica_set_member)

    def remove_shard(self, context, replica_set_name):
        return self.app.remove_shard(replica_set_name)

    def is_shard_active(self, context, replica_set_name):
        return self.app.is_shard_active(replica_set_name)

    def get_replica_set_name(self, context):
        return self.app.get_replica_set_name()

    def get_key(self, context):
        return self.app.get_key()

    def cluster_complete(self, context):
        return self.app.cluster_complete()
