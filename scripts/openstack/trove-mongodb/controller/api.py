"""Trove API strategy for MongoDB replica sets and sharded clusters.

The default topology remains a replica set for compatibility with the
Yggdrasil functional test.  Passing ``topology=sharded`` as an extended
cluster property creates config servers, one shard replica set, and query
routers.  Additional shards and routers can then be added with the normal
cluster grow operation.
"""

import time

from oslo_log import log as logging

from trove.cluster import models
from trove.cluster.models import Cluster
from trove.cluster.tasks import ClusterTasks
from trove.cluster.views import ClusterView
from trove.common import cfg
from trove.common import clients
from trove.common import exception
from trove.common import glance as common_glance
from trove.common import server_group as srv_grp
from trove.common import utils
from trove.common.notification import DBaaSClusterGrow
from trove.common.notification import StartNotification
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
CONFIG_SERVER_REPLICA_SET_NAME = "configRS"
MIN_CONFIG_SERVER_MEMBERS = 2
MIN_QUERY_ROUTERS = 1
SHARDED_TOPOLOGY = "sharded"


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
    """Trove cluster model backed by a MongoDB topology."""

    @staticmethod
    def _topology(extended_properties):
        topology = str((extended_properties or {}).get(
            "topology", "replica_set")).lower().replace("-", "_")
        if topology not in ("replica_set", SHARDED_TOPOLOGY):
            raise exception.TroveError(
                "MongoDB topology must be replica_set or sharded")
        return topology

    @staticmethod
    def _integer_property(properties, name, default, minimum):
        try:
            value = int((properties or {}).get(name, default))
        except (TypeError, ValueError):
            raise exception.TroveError(
                "MongoDB extended property %s must be an integer" % name)
        if value < minimum:
            raise exception.TroveError(
                "MongoDB extended property %s must be at least %d" %
                (name, minimum))
        return value

    @property
    def is_sharded(self):
        return any(instance.type in ("config_server", "query_router")
                   for instance in self.db_instances)

    @staticmethod
    def _validate_shard_members(context, members, mongo_conf):
        if len(members) < MIN_REPLICA_SET_MEMBERS:
            raise exception.ClusterNumInstancesNotSupported(
                num_instances=MIN_REPLICA_SET_MEMBERS)
        models.validate_instance_flavors(
            context, members, mongo_conf.volume_support,
            mongo_conf.device_path)
        models.assert_homogeneous_cluster(members)
        models.validate_instance_nics(context, members)

    @staticmethod
    def _shard_member_config(cluster_id, shard_id, replica_set_name):
        return {
            "id": cluster_id,
            "topology": SHARDED_TOPOLOGY,
            "instance_type": "member",
            "shard_id": shard_id,
            "replica_set_name": replica_set_name,
            "config_server_replica_set_name":
                CONFIG_SERVER_REPLICA_SET_NAME,
        }

    @staticmethod
    def _config_server_config(cluster_id):
        return {
            "id": cluster_id,
            "topology": SHARDED_TOPOLOGY,
            "instance_type": "config_server",
            "replica_set_name": CONFIG_SERVER_REPLICA_SET_NAME,
            "config_server_replica_set_name":
                CONFIG_SERVER_REPLICA_SET_NAME,
        }

    @staticmethod
    def _query_router_config(cluster_id):
        return {
            "id": cluster_id,
            "topology": SHARDED_TOPOLOGY,
            "instance_type": "query_router",
            "config_server_replica_set_name":
                CONFIG_SERVER_REPLICA_SET_NAME,
        }

    @classmethod
    def _create_sharded(cls, context, name, datastore, datastore_version,
                        instances, extended_properties, locality,
                        configuration, image_id=None):
        mongo_conf = CONF.get(datastore_version.manager)
        if mongo_conf.cluster_secure:
            raise exception.TroveError(
                "The MongoDB sharded controller currently requires "
                "cluster_secure=false")

        cls._validate_shard_members(context, instances, mongo_conf)
        num_config_servers = cls._integer_property(
            extended_properties, "num_configsvr",
            getattr(mongo_conf, "num_config_servers_per_cluster", 3),
            MIN_CONFIG_SERVER_MEMBERS)
        num_query_routers = cls._integer_property(
            extended_properties, "num_mongos",
            getattr(mongo_conf, "num_query_routers_per_cluster", 1),
            MIN_QUERY_ROUTERS)

        shard_volume_size = instances[0].get("volume_size")
        shard_volume_type = instances[0].get("volume_type")
        config_volume_size = cls._integer_property(
            extended_properties, "configsvr_volume_size",
            getattr(mongo_conf, "config_servers_volume_size", 10), 1)
        config_volume_type = extended_properties.get(
            "configsvr_volume_type", shard_volume_type)
        router_volume_size = cls._integer_property(
            extended_properties, "mongos_volume_size",
            getattr(mongo_conf, "query_routers_volume_size", 10), 1)
        router_volume_type = extended_properties.get(
            "mongos_volume_type", shard_volume_type)

        extra_instances = ([{"volume_size": config_volume_size}]
                           * num_config_servers +
                           [{"volume_size": router_volume_size}]
                           * num_query_routers)
        total_volume_size = models.get_required_volume_size(
            list(instances) + extra_instances, mongo_conf.volume_support)
        check_quotas(
            context.project_id,
            {"instances": len(instances) + num_config_servers +
             num_query_routers, "volumes": total_volume_size})

        configuration_id = configuration or None
        if configuration_id:
            config_models.Configuration.find(
                context, configuration_id, datastore_version.id)

        resolved_image_id = image_id or datastore_version.image_id

        db_info = models.DBCluster.create(
            name=name,
            tenant_id=context.project_id,
            datastore_version_id=datastore_version.id,
            configuration_id=configuration_id,
            task_status=ClusterTasks.BUILDING_INITIAL)

        shard_id = utils.generate_uuid()
        member_config = cls._shard_member_config(
            db_info.id, shard_id, "rs1")
        config_config = cls._config_server_config(db_info.id)
        router_config = cls._query_router_config(db_info.id)
        nics = instances[0].get("nics")
        regions = [instance.get("region_name") for instance in instances]

        for index, instance in enumerate(instances, start=1):
            instance_name = instance.get(
                "name", "%s-rs1-%d" % (name, index))
            inst_models.Instance.create(
                context, instance_name, instance["flavor_id"],
                resolved_image_id, [], [], datastore,
                datastore_version, shard_volume_size, None,
                availability_zone=instance.get("availability_zone"),
                nics=instance.get("nics", nics),
                configuration_id=configuration_id,
                cluster_config=member_config,
                volume_type=shard_volume_type,
                modules=instance.get("modules"),
                locality=locality,
                region_name=instance.get("region_name"))

        flavor_id = instances[0]["flavor_id"]
        for index in range(1, num_config_servers + 1):
            inst_models.Instance.create(
                context, "%s-configsvr-%d" % (name, index), flavor_id,
                resolved_image_id, [], [], datastore,
                datastore_version, config_volume_size, None,
                availability_zone=None, nics=nics,
                configuration_id=configuration_id,
                cluster_config=config_config,
                volume_type=config_volume_type,
                locality=locality,
                region_name=regions[(index - 1) % len(regions)])

        for index in range(1, num_query_routers + 1):
            inst_models.Instance.create(
                context, "%s-mongos-%d" % (name, index), flavor_id,
                resolved_image_id, [], [], datastore,
                datastore_version, router_volume_size, None,
                availability_zone=None, nics=nics,
                configuration_id=configuration_id,
                cluster_config=router_config,
                volume_type=router_volume_type,
                locality=locality,
                region_name=regions[(index - 1) % len(regions)])

        task_api.load(context, datastore_version.manager).create_cluster(
            db_info.id)
        return cls(context, db_info, datastore, datastore_version)

    @staticmethod
    def _create_instances(context, db_info, datastore, datastore_version,
                          instances, locality, configuration_id=None,
                          enforce_minimum=True, image_id=None):
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
        resolved_image_id = image_id or datastore_version.image_id
        for index, instance in enumerate(instances, start=1):
            name = instance.get("name") or "%s-member-%s" % (
                db_info.name, index)
            member_instances.append(
                inst_models.Instance.create(
                    context,
                    name,
                    instance["flavor_id"],
                    resolved_image_id,
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
                        "topology": "replica_set",
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
        topology = cls._topology(extended_properties)
        if topology == SHARDED_TOPOLOGY:
            LOG.info("Creating MongoDB sharded cluster %s", name)
            return cls._create_sharded(
                context, name, datastore, datastore_version, instances,
                extended_properties, locality, configuration, image_id)

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
            locality, configuration_id=configuration_id, image_id=image_id)
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

    def action(self, context, req, action, param):
        """Preserve MongoDB-specific grow fields before dispatching.

        The base cluster action parser drops ``related_to``.  That field is
        required to group named grow members into one new MongoDB shard, so
        retain it here while keeping all other cluster actions on Trove's
        standard implementation.
        """
        if action != "grow":
            return super(MongoDbCluster, self).action(
                context, req, action, param)

        context.notification = DBaaSClusterGrow(context, request=req)
        with StartNotification(context, cluster_id=self.id):
            instances = []
            for node in param:
                instance = {
                    "flavor_id": utils.get_id_from_href(node["flavorRef"])
                }
                if "name" in node:
                    instance["name"] = node["name"]
                if "volume" in node:
                    instance["volume_size"] = int(node["volume"]["size"])
                if "modules" in node:
                    instance["modules"] = node["modules"]
                if "nics" in node:
                    instance["nics"] = node["nics"]
                if "availability_zone" in node:
                    instance["availability_zone"] = node[
                        "availability_zone"]
                if "type" in node:
                    instance_type = node["type"]
                    if isinstance(instance_type, str):
                        instance_type = instance_type.split(",")
                    instance["instance_type"] = instance_type
                if "related_to" in node:
                    instance["related_to"] = node["related_to"]
                instances.append(instance)

            image_id = self.ds_version.image_id
            if not image_id:
                glance_client = clients.create_glance_client(context)
                image_id = common_glance.get_image_id(
                    glance_client, self.ds_version.image_id,
                    self.ds_version.image_tags)
            return self.grow(instances, image_id)

    @staticmethod
    def _group_shard_instances(instances):
        """Group ``type=replica`` grow items into new shards.

        ``related_to`` follows Trove's stock MongoDB convention: all members
        referring to the same name belong to one replica set.  For a compact
        request with no names, all replica items form one new shard.
        """
        if not instances:
            return []
        if all(not item.get("name") and not item.get("related_to")
               for item in instances):
            return [list(instances)]

        groups = []
        names = {}
        unnamed = []
        for item in instances:
            name = item.get("name")
            related_to = item.get("related_to")
            if not name and not related_to:
                unnamed.append(item)
                continue
            key = related_to or name
            group = names.get(key)
            if group is None:
                group = []
                names[key] = group
                groups.append(group)
            group.append(item)
            if name:
                previous = names.get(name)
                if previous is not None and previous is not group:
                    raise exception.TroveError(
                        "Replica member names cannot belong to multiple "
                        "MongoDB shards")
                names[name] = group
        if unnamed:
            raise exception.TroveError(
                "Named MongoDB replica members are required when "
                "related_to is used")
        return groups

    @staticmethod
    def _instance_type(instance):
        value = instance.get("instance_type")
        if isinstance(value, (list, tuple)):
            if len(value) != 1:
                raise exception.TroveError(
                    "Each MongoDB grow node must have exactly one type")
            return value[0]
        return value

    def _create_sharded_instances(self, instances, locality, image_id=None):
        mongo_conf = CONF.get(self.ds_version.manager)
        replica_items = [instance for instance in instances
                         if self._instance_type(instance) in
                         ("replica", "member", None)]
        router_items = [instance for instance in instances
                        if self._instance_type(instance) == "query_router"]
        unsupported = [self._instance_type(instance) for instance in
                       instances if self._instance_type(instance) not in
                       ("replica", "member", "query_router", None)]
        if unsupported:
            raise exception.TroveError(
                "MongoDB sharded grow does not support node type(s): %s" %
                sorted(set(unsupported)))

        groups = self._group_shard_instances(replica_items)
        for group in groups:
            self._validate_shard_members(self.context, group, mongo_conf)
        for router in router_items:
            models.validate_instance_flavors(
                self.context, [router], mongo_conf.volume_support,
                mongo_conf.device_path)
            models.validate_instance_nics(self.context, [router])
        if not groups and not router_items:
            raise exception.TroveError("No MongoDB nodes specified for grow")

        new_items = list(replica_items) + list(router_items)
        check_quotas(
            self.context.project_id,
            {"instances": len(new_items),
             "volumes": models.get_required_volume_size(
                 new_items, mongo_conf.volume_support)})

        existing_shards = {
            member.shard_id for member in self.db_instances
            if member.type == "member" and member.shard_id}
        next_shard_number = len(existing_shards) + 1
        new_ids = []
        resolved_image_id = image_id or self.ds_version.image_id
        for group in groups:
            shard_id = utils.generate_uuid()
            replica_set_name = "rs%d" % next_shard_number
            next_shard_number += 1
            cluster_config = self._shard_member_config(
                self.id, shard_id, replica_set_name)
            for index, item in enumerate(group, start=1):
                name = item.get(
                    "name", "%s-%s-%d" %
                    (self.name, replica_set_name, index))
                created = inst_models.Instance.create(
                    self.context, name, item["flavor_id"],
                    resolved_image_id, [], [], self.ds,
                    self.ds_version, item.get("volume_size"), None,
                    availability_zone=item.get("availability_zone"),
                    nics=item.get("nics"),
                    configuration_id=self.configuration_id,
                    cluster_config=cluster_config,
                    volume_type=item.get("volume_type"),
                    modules=item.get("modules"),
                    locality=locality,
                    region_name=item.get("region_name"))
                new_ids.append(created.id)

        router_config = self._query_router_config(self.id)
        for index, item in enumerate(router_items, start=1):
            name = item.get("name", "%s-mongos-%d" % (self.name, index))
            created = inst_models.Instance.create(
                self.context, name, item["flavor_id"],
                resolved_image_id, [], [], self.ds,
                self.ds_version, item.get("volume_size"), None,
                availability_zone=item.get("availability_zone"),
                nics=item.get("nics"),
                configuration_id=self.configuration_id,
                cluster_config=router_config,
                volume_type=item.get("volume_type"),
                modules=item.get("modules"),
                locality=locality,
                region_name=item.get("region_name"))
            new_ids.append(created.id)
        return new_ids

    def _grow_sharded(self, instances, image_id=None):
        LOG.info("Growing MongoDB sharded cluster %s", self.id)
        self.validate_cluster_available()
        locality = srv_grp.ServerGroup.convert_to_hint(self.server_group)
        try:
            new_ids = self._create_sharded_instances(
                instances, locality, image_id=image_id)
        except Exception:
            self.update_db(task_status=ClusterTasks.NONE)
            raise
        self.update_db(task_status=ClusterTasks.GROWING_CLUSTER)
        task_api.load(self.context, self.ds_version.manager).grow_cluster(
            self.id, new_ids)
        return self.__class__(self.context, self.db_info,
                              self.ds, self.ds_version)

    def grow(self, instances, image_id=None):
        if self.is_sharded:
            return self._grow_sharded(instances, image_id=image_id)

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
            enforce_minimum=False,
            image_id=image_id)
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

    def _sharded_replica_set_name(self, instance):
        loaded = inst_models.Instance.load(self.context, instance.id)
        name = self.get_guest(loaded).get_replica_set_name()
        if not name:
            raise exception.TroveError(
                "MongoDB shard member %s has no replica-set name" %
                instance.id)
        return name

    def _remove_shard_from_router(self, router, replica_set_name):
        guest = self.get_guest(inst_models.Instance.load(
            self.context, router.id))
        result = guest.remove_shard(replica_set_name)
        if isinstance(result, dict) and result.get("ok") not in (None, 1):
            raise exception.TroveError(
                "MongoDB refused to remove shard %s: %s" %
                (replica_set_name, result))
        if isinstance(result, dict) and result.get("state") == "completed":
            return

        deadline = time.time() + CONF.mongodb.add_members_timeout
        while time.time() < deadline:
            # MongoDB's removeShard command is intentionally progressive:
            # callers repeat it until the response reaches ``completed``.
            # listShards alone can remain in the draining state indefinitely.
            result = guest.remove_shard(replica_set_name)
            if isinstance(result, dict) and result.get("state") == \
                    "completed":
                return
            time.sleep(2)
        raise exception.TroveError(
            "MongoDB shard %s did not finish draining" % replica_set_name)

    def _shrink_sharded(self, removal_ids):
        LOG.info("Shrinking MongoDB sharded cluster %s", self.id)
        self.validate_cluster_available()
        db_instances = inst_models.DBInstance.find_all(
            cluster_id=self.id, deleted=False).all()
        all_by_id = {instance.id: instance for instance in db_instances}
        requested_ids = set(removal_ids)
        if not requested_ids:
            raise exception.TroveError("No MongoDB nodes specified for shrink")
        if not requested_ids.issubset(all_by_id):
            raise exception.NotFound(uuid=next(iter(
                requested_ids.difference(all_by_id))))

        config_server_ids = {
            instance.id for instance in db_instances
            if instance.type == "config_server"
        }
        if requested_ids.intersection(config_server_ids):
            raise exception.ClusterShrinkInstanceInUse(
                id=list(requested_ids.intersection(config_server_ids)),
                reason="MongoDB config servers cannot be removed")

        routers = [instance for instance in db_instances
                   if instance.type == "query_router"]
        requested_router_ids = requested_ids.intersection(
            {instance.id for instance in routers})
        if len(routers) - len(requested_router_ids) < MIN_QUERY_ROUTERS:
            raise exception.ClusterShrinkInstanceInUse(
                id=list(requested_router_ids),
                reason="At least one MongoDB query router must remain")

        members = [instance for instance in db_instances
                   if instance.type == "member"]
        requested_member_ids = requested_ids.intersection(
            {instance.id for instance in members})
        target_shards = {}
        for member in members:
            if member.id in requested_member_ids:
                target_shards.setdefault(member.shard_id, []).append(member)

        existing_shards = {member.shard_id for member in members}
        if len(existing_shards - set(target_shards)) < 1:
            raise exception.TroveError(
                "A MongoDB sharded cluster must retain at least one shard")
        for shard_id, target_members in target_shards.items():
            all_shard_members = [member for member in members
                                 if member.shard_id == shard_id]
            if {member.id for member in target_members} != {
                    member.id for member in all_shard_members}:
                raise exception.TroveError(
                    "MongoDB sharded shrink removes complete shards only; "
                    "shard %s still has members" % shard_id)

        remaining_routers = [router for router in routers
                             if router.id not in requested_router_ids]
        self.update_db(task_status=ClusterTasks.SHRINKING_CLUSTER)
        try:
            for shard_id, target_members in target_shards.items():
                replica_set_name = self._sharded_replica_set_name(
                    target_members[0])
                self._remove_shard_from_router(
                    remaining_routers[0], replica_set_name)

            for instance_id in requested_ids:
                instance = inst_models.Instance.load(
                    self.context, instance_id)
                instance.update_db(cluster_id=None)
                inst_models.Instance.delete(instance)
        finally:
            self.update_db(task_status=ClusterTasks.NONE)

        return self.__class__(self.context, self.db_info,
                              self.ds, self.ds_version)

    def shrink(self, removal_ids):
        if self.is_sharded:
            return self._shrink_sharded(removal_ids)

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
        return self._build_instances(
            ["query_router"], ["member", "query_router"])


class MongoDbMgmtClusterView(MgmtClusterView):

    def build_instances(self):
        return self._build_instances(
            ["query_router"], ["config_server", "member", "query_router"])
