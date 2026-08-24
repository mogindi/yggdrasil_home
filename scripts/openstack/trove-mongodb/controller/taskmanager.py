"""Trove task-manager strategy for MongoDB topologies."""

import time

from eventlet.timeout import Timeout
from oslo_log import log as logging

from trove.common import cfg
from trove.common.strategies.cluster import base
from trove.instance import tasks as inst_tasks
from trove.instance.models import DBInstance
from trove.instance.models import Instance
from trove.taskmanager import api as task_api
from trove.taskmanager import models as task_models


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class MongoDbTaskManagerStrategy(base.BaseTaskManagerStrategy):

    @property
    def task_manager_api_class(self):
        return MongoDbTaskManagerAPI

    @property
    def task_manager_cluster_tasks_class(self):
        return MongoDbClusterTasks

    @property
    def task_manager_manager_actions(self):
        return {"add_shard_cluster": self._manager_add_shard}

    def _manager_add_shard(self, context, cluster_id, shard_id,
                           replica_set_name):
        cluster_tasks = MongoDbClusterTasks.load(
            context, cluster_id, MongoDbClusterTasks)
        cluster_tasks.add_shard_cluster(context, cluster_id, shard_id,
                                        replica_set_name)


class MongoDbClusterTasks(task_models.ClusterTasks):

    @staticmethod
    def _address(instance):
        addresses = instance.get_visible_ip_addresses()
        if not addresses:
            raise RuntimeError(
                "MongoDB member %s has no visible address" % instance.id)
        return "%s:%s" % (addresses[0]["address"],
                           CONF.mongodb.mongodb_port)

    def _run_with_timeout(self, operation, cluster_id, failure_status=None):
        timeout = Timeout(CONF.cluster_usage_timeout)
        try:
            operation()
            self.reset_task()
        except Timeout as exc:
            if exc is not timeout:
                raise
            LOG.exception("Timed out while changing MongoDB cluster %s",
                          cluster_id)
            self.update_statuses_on_failure(
                cluster_id, status=failure_status)
        except Exception:
            LOG.exception("Failed to change MongoDB cluster %s", cluster_id)
            self.update_statuses_on_failure(
                cluster_id, status=failure_status)
        finally:
            timeout.cancel()

    def _wait_for_replica_members(self, guest, addresses):
        deadline = time.time() + CONF.mongodb.add_members_timeout
        expected = set(addresses)
        while time.time() < deadline:
            try:
                status = guest.replica_set_status()
                members = status.get("members", [])
                ready = {
                    member.get("name") for member in members
                    if member.get("state") in (1, 2)
                }
                if expected.issubset(ready):
                    return
            except Exception:
                LOG.debug("MongoDB replica-set membership is not ready",
                          exc_info=True)
            time.sleep(2)
        raise RuntimeError(
            "MongoDB replica-set members did not become ready in time")

    @staticmethod
    def _by_type(instances, instance_type):
        return [instance for instance in instances
                if instance.type == instance_type]

    def _is_sharded(self, instances):
        return bool(self._by_type(instances, "config_server") or
                    self._by_type(instances, "query_router"))

    def _initialize_replica_set(self, members):
        if not members:
            raise RuntimeError("MongoDB replica set has no members")
        addresses = [self._address(member) for member in members]
        guest = self.get_guest(members[0])
        guest.initialize_replica_set(addresses)
        self._wait_for_replica_members(guest, addresses)
        return addresses

    def _configure_query_routers(self, routers, config_servers):
        if not routers:
            raise RuntimeError(
                "A sharded MongoDB cluster needs a query router")
        config_addresses = [self._address(server)
                            for server in config_servers]
        if not config_addresses:
            raise RuntimeError(
                "A sharded MongoDB cluster needs config servers")
        for router in routers:
            self.get_guest(router).add_config_servers(config_addresses)

    def _create_shard(self, query_router, members):
        addresses = self._initialize_replica_set(members)
        primary = members[0]
        replica_set_name = self.get_guest(primary).get_replica_set_name()
        if not replica_set_name:
            raise RuntimeError("MongoDB shard has no replica-set name")
        result = self.get_guest(query_router).add_shard(
            replica_set_name, addresses[0])
        if isinstance(result, dict) and result.get("ok") not in (None, 1):
            raise RuntimeError(
                "MongoDB query router rejected shard %s: %s" %
                (replica_set_name, result))
        return replica_set_name

    def create_cluster(self, context, cluster_id):
        LOG.info("Creating MongoDB cluster %s", cluster_id)

        def create():
            db_instances = DBInstance.find_all(
                cluster_id=cluster_id, deleted=False).all()
            instance_ids = [instance.id for instance in db_instances]
            if not self._all_instances_ready(instance_ids, cluster_id):
                return

            members = [Instance.load(context, instance_id)
                       for instance_id in instance_ids]
            if not self._is_sharded(members):
                self._initialize_replica_set(members)
            else:
                config_servers = self._by_type(members, "config_server")
                query_routers = self._by_type(members, "query_router")
                shard_members = self._by_type(members, "member")
                self._initialize_replica_set(config_servers)
                self._configure_query_routers(query_routers, config_servers)
                shard_ids = []
                for member in shard_members:
                    if member.shard_id not in shard_ids:
                        shard_ids.append(member.shard_id)
                for shard_id in shard_ids:
                    self._create_shard(
                        query_routers[0],
                        [member for member in shard_members
                         if member.shard_id == shard_id])
            for member in members:
                self.get_guest(member).cluster_complete()

        self._run_with_timeout(cluster_id=cluster_id, operation=create,
                               failure_status=inst_tasks.InstanceTasks.
                               BUILDING_ERROR_SERVER)

    def grow_cluster(self, context, cluster_id, new_instance_ids):
        LOG.info("Growing MongoDB cluster %s", cluster_id)

        def grow():
            if not self._all_instances_ready(new_instance_ids, cluster_id):
                return
            db_instances = DBInstance.find_all(
                cluster_id=cluster_id, deleted=False).all()
            existing = [instance for instance in db_instances
                        if instance.id not in new_instance_ids]
            new_instances = [Instance.load(context, instance_id)
                             for instance_id in new_instance_ids]
            if not existing:
                raise RuntimeError("MongoDB cluster has no existing member")
            if not self._is_sharded(existing + new_instances):
                addresses = [self._address(member)
                             for member in new_instances]
                guest = self.get_guest(Instance.load(
                    context, existing[0].id))
                guest.add_replica_members(addresses)
                self._wait_for_replica_members(guest, addresses)
            else:
                config_servers = self._by_type(existing, "config_server")
                query_routers = self._by_type(existing, "query_router")
                new_members = self._by_type(new_instances, "member")
                new_routers = self._by_type(new_instances, "query_router")
                if new_routers:
                    self._configure_query_routers(
                        new_routers, config_servers)
                    query_routers = query_routers + new_routers
                if new_members:
                    if not query_routers:
                        raise RuntimeError(
                            "A sharded MongoDB cluster needs a query router")
                    shard_ids = []
                    for member in new_members:
                        if member.shard_id not in shard_ids:
                            shard_ids.append(member.shard_id)
                    for shard_id in shard_ids:
                        self._create_shard(
                            query_routers[0],
                            [member for member in new_members
                             if member.shard_id == shard_id])
            for member in new_instances:
                self.get_guest(member).cluster_complete()

        self._run_with_timeout(
            cluster_id=cluster_id, operation=grow,
            failure_status=inst_tasks.InstanceTasks.GROWING_ERROR)

    def add_shard_cluster(self, context, cluster_id, shard_id,
                          replica_set_name=None):
        """Complete a separately scheduled shard-add operation."""
        instance_ids = [instance.id for instance in DBInstance.find_all(
            cluster_id=cluster_id, shard_id=shard_id,
            deleted=False).all()]
        self.grow_cluster(context, cluster_id, instance_ids)

    def restart_cluster(self, context, cluster_id):
        """Restart MongoDB roles one at a time so HA stays available."""
        self.rolling_restart_cluster(
            context, cluster_id, delay_sec=5)

    def shrink_cluster(self, context, cluster_id, instance_ids):
        LOG.info("Completing MongoDB topology shrink for %s", cluster_id)

        def shrink():
            db_instances = DBInstance.find_all(
                cluster_id=cluster_id, deleted=False).all()
            if set(instance_ids).intersection(
                    {instance.id for instance in db_instances}):
                return

        self._run_with_timeout(
            cluster_id=cluster_id, operation=shrink,
            failure_status=inst_tasks.InstanceTasks.SHRINKING_ERROR)

    def upgrade_cluster(self, context, cluster_id, datastore_version):
        self.rolling_upgrade_cluster(context, cluster_id, datastore_version)


class MongoDbTaskManagerAPI(task_api.API):

    def mongodb_add_shard_cluster(self, cluster_id, shard_id,
                                  replica_set_name):
        version = task_api.API.API_BASE_VERSION
        cctxt = self.client.prepare(version=version)
        cctxt.cast(self.context, "add_shard_cluster",
                   cluster_id=cluster_id, shard_id=shard_id,
                   replica_set_name=replica_set_name)
