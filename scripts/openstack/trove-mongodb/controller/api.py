"""A small Trove API strategy for MongoDB replica sets.

The MongoDB strategy shipped by Trove is aimed at a sharded deployment and
creates config servers and a query router.  The injected guest image only
needs a replica set for the common HA use case, so this strategy keeps the
cluster to the requested MongoDB members and delegates membership changes to
the guest agent.
"""

import time

from oslo_log import log as logging

from trove.cluster import models
from trove.cluster.models import Cluster
from trove.cluster.tasks import ClusterTasks
from trove.cluster.views import ClusterView
from trove.common import cfg
from trove.common import exception
from trove.common import server_group as srv_grp
from trove.common.strategies.cluster import base
from trove.configuration import models as config_models
from trove.extensions.mgmt.clusters.views import MgmtClusterView
from trove.instance import models as inst_models
from trove.quota.quota import check_quotas
from trove.taskmanager import api as task_api


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

MIN_REPLICA_SET_MEMBERS = 2
REPLICA_SET_NAME = "rs0"


class MongoDbAPIStrategy(base.BaseAPIStrategy):

    @property
    def cluster_class(self):
        return MongoDbCluster

    @property
    def cluster_view_class(self):
        return MongoDbClusterView

    @property
    def mgmt_cluster_view_class(self):
        return MongoDbMgmtClusterView


class MongoDbCluster(models.Cluster):
    """Trove cluster model backed by one MongoDB replica set."""

    @staticmethod
    def _create_instances(context, db_info, datastore, datastore_version,
                          instances, locality, configuration_id=None,
                          enforce_minimum=True):
        mongo_conf = CONF.get(datastore_version.manager)
        if mongo_conf.cluster_secure:
            raise exception.TroveError(
                "The MongoDB replica-set controller currently requires "
                "cluster_secure=false")
        volume_enabled = mongo_conf.volume_support
        ephemeral_enabled = mongo_conf.device_path

        if enforce_minimum and len(instances) < MIN_REPLICA_SET_MEMBERS:
            raise exception.ClusterNumInstancesNotSupported(
                num_instances=MIN_REPLICA_SET_MEMBERS)

        models.validate_instance_flavors(
            context, instances, volume_enabled, ephemeral_enabled)
        models.assert_homogeneous_cluster(instances)
        models.validate_instance_nics(context, instances)

        total_volume_size = models.get_required_volume_size(
            instances, volume_enabled)
        check_quotas(
            context.project_id,
            {"instances": len(instances), "volumes": total_volume_size})

        member_instances = []
        for index, instance in enumerate(instances, start=1):
            name = instance.get("name") or "%s-member-%s" % (
                db_info.name, index)
            member_instances.append(
                inst_models.Instance.create(
                    context,
                    name,
                    instance["flavor_id"],
                    datastore_version.image_id,
                    [],
                    [],
                    datastore,
                    datastore_version,
                    instance.get("volume_size"),
                    None,
                    availability_zone=instance.get("availability_zone"),
                    nics=instance.get("nics"),
                    configuration_id=configuration_id,
                    cluster_config={
                        "id": db_info.id,
                        "instance_type": "member",
                        "replica_set_name": REPLICA_SET_NAME,
                    },
                    volume_type=instance.get("volume_type"),
                    modules=instance.get("modules"),
                    locality=locality,
                    region_name=instance.get("region_name"),
                ))
        return member_instances

    @classmethod
    def create(cls, context, name, datastore, datastore_version,
               instances, extended_properties, locality, configuration,
               image_id=None):
        LOG.info("Creating MongoDB replica-set cluster %s", name)

        configuration_id = configuration or None
        if configuration_id:
            config_models.Configuration.find(
                context, configuration_id, datastore_version.id)

        db_info = models.DBCluster.create(
            name=name,
            tenant_id=context.project_id,
            datastore_version_id=datastore_version.id,
            configuration_id=configuration_id,
            task_status=ClusterTasks.BUILDING_INITIAL)

        cls._create_instances(
            context, db_info, datastore, datastore_version, instances,
            locality, configuration_id=configuration_id)
        task_api.load(context, datastore_version.manager).create_cluster(
            db_info.id)

        return cls(context, db_info, datastore, datastore_version)

    def _validate_growth(self, instances):
        if not instances:
            raise exception.TroveError("No instances specified for grow")

        existing = self.db_instances
        if not existing:
            raise exception.TroveError("MongoDB cluster has no members")
        reference = existing[0]
        for instance in instances:
            if instance["flavor_id"] != reference.flavor_id:
                raise exception.ClusterFlavorsNotEqual()
            if instance.get("volume_size") != reference.volume_size:
                raise exception.ClusterVolumeSizesNotEqual()

    def grow(self, instances, image_id=None):
        LOG.info("Growing MongoDB replica-set cluster %s", self.id)
        self.validate_cluster_available()
        self._validate_growth(instances)

        self.update_db(task_status=ClusterTasks.GROWING_CLUSTER)
        locality = srv_grp.ServerGroup.convert_to_hint(self.server_group)
        new_instances = self._create_instances(
            self.context,
            self.db_info,
            self.ds,
            self.ds_version,
            instances,
            locality,
            configuration_id=self.configuration_id,
            enforce_minimum=False)
        task_api.load(self.context, self.ds_version.manager).grow_cluster(
            self.id, [instance.id for instance in new_instances])
        return self.__class__(self.context, self.db_info,
                              self.ds, self.ds_version)

    def _member_address(self, instance):
        addresses = instance.get_visible_ip_addresses()
        if not addresses:
            raise exception.TroveError(
                "MongoDB cluster member %s has no visible address" %
                instance.id)
        return "%s:%s" % (addresses[0]["address"],
                           CONF.mongodb.mongodb_port)

    def shrink(self, removal_ids):
        LOG.info("Shrinking MongoDB replica-set cluster %s", self.id)
        self.validate_cluster_available()

        db_instances = inst_models.DBInstance.find_all(
            cluster_id=self.id, deleted=False).all()
        all_ids = {instance.id for instance in db_instances}
        requested_ids = set(removal_ids)
        if not requested_ids.issubset(all_ids):
            raise exception.NotFound(uuid=next(iter(
                requested_ids.difference(all_ids))))
        if len(all_ids) - len(requested_ids) < MIN_REPLICA_SET_MEMBERS:
            raise exception.TroveError(
                "A MongoDB replica set must retain at least %d members" %
                MIN_REPLICA_SET_MEMBERS)

        self.update_db(task_status=ClusterTasks.SHRINKING_CLUSTER)
        try:
            remaining = [inst_models.Instance.load(self.context, instance.id)
                         for instance in db_instances
                         if instance.id not in requested_ids]
            removal = [inst_models.Instance.load(self.context, instance.id)
                       for instance in db_instances
                       if instance.id in requested_ids]
            removal_addresses = [self._member_address(instance)
                                 for instance in removal]

            removed = False
            deadline = time.time() + CONF.mongodb.add_members_timeout
            # The primary may itself be one of the requested removals.  Try
            # the surviving members first; if the primary is being removed,
            # its guest steps down and a surviving member completes the
            # removal after the election.
            while not removed and time.time() < deadline:
                for candidate in remaining + removal:
                    try:
                        result = Cluster.get_guest(
                            candidate).remove_replica_members(
                                removal_addresses)
                        if not (isinstance(result, dict) and
                                result.get("stepped_down")):
                            removed = True
                            break
                    except Exception:
                        LOG.debug(
                            "Member %s was not the replica-set primary",
                            candidate.id, exc_info=True)
                if not removed:
                    time.sleep(2)
            if not removed:
                raise exception.TroveError(
                    "Unable to find a MongoDB replica-set primary")

            for instance in removal:
                instance.update_db(cluster_id=None)
            for instance in removal:
                inst_models.Instance.delete(instance)
        finally:
            self.update_db(task_status=ClusterTasks.NONE)

        return self.__class__(self.context, self.db_info,
                              self.ds, self.ds_version)

    def _configuration_requires_restart(self, configuration_id):
        configuration = config_models.Configuration.find(
            self.context, configuration_id, self.ds_version.id)
        return configuration.does_configuration_need_restart()

    def rolling_configuration_update(self, configuration_id,
                                     apply_on_all=True):
        requires_restart = self._configuration_requires_restart(
            configuration_id)
        result = super(MongoDbCluster, self).rolling_configuration_update(
            configuration_id, apply_on_all=apply_on_all)
        if requires_restart:
            LOG.info("Rolling MongoDB restart after configuration update")
            return result.rolling_restart()
        return result

    def configuration_attach(self, configuration_id):
        return self.rolling_configuration_update(configuration_id)

    def configuration_detach(self):
        if not self.configuration_id:
            return self
        requires_restart = self._configuration_requires_restart(
            self.configuration_id)
        result = super(MongoDbCluster, self).rolling_configuration_remove()
        if requires_restart:
            return result.rolling_restart()
        return result


class MongoDbClusterView(ClusterView):

    def build_instances(self):
        return self._build_instances(["member"], ["member"])


class MongoDbMgmtClusterView(MgmtClusterView):

    def build_instances(self):
        return self._build_instances(["member"], ["member"])
