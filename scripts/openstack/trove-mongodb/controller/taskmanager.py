"""Trove task-manager strategy for MongoDB replica sets."""

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

    def create_cluster(self, context, cluster_id):
        LOG.info("Creating MongoDB replica-set cluster %s", cluster_id)

        def create():
            db_instances = DBInstance.find_all(
                cluster_id=cluster_id, deleted=False).all()
            instance_ids = [instance.id for instance in db_instances]
            if not self._all_instances_ready(instance_ids, cluster_id):
                return

            members = [Instance.load(context, instance_id)
                       for instance_id in instance_ids]
            addresses = [self._address(member) for member in members]
            guest = self.get_guest(members[0])
            guest.initialize_replica_set(addresses)
            self._wait_for_replica_members(guest, addresses)
            for member in members:
                self.get_guest(member).cluster_complete()

        self._run_with_timeout(cluster_id=cluster_id, operation=create,
                               failure_status=inst_tasks.InstanceTasks.
                               BUILDING_ERROR_SERVER)

    def grow_cluster(self, context, cluster_id, new_instance_ids):
        LOG.info("Growing MongoDB replica-set cluster %s", cluster_id)

        def grow():
            if not self._all_instances_ready(new_instance_ids, cluster_id):
                return
            db_instances = DBInstance.find_all(
                cluster_id=cluster_id, deleted=False).all()
            existing = [instance for instance in db_instances
                        if instance.id not in new_instance_ids]
            new_members = [Instance.load(context, instance_id)
                           for instance_id in new_instance_ids]
            if not existing:
                raise RuntimeError("MongoDB cluster has no existing member")
            addresses = [self._address(member) for member in new_members]
            guest = self.get_guest(Instance.load(context, existing[0].id))
            guest.add_replica_members(addresses)
            self._wait_for_replica_members(guest, addresses)
            for member in new_members:
                self.get_guest(member).cluster_complete()

        self._run_with_timeout(
            cluster_id=cluster_id, operation=grow,
            failure_status=inst_tasks.InstanceTasks.GROWING_ERROR)

    def restart_cluster(self, context, cluster_id):
        """Restart members one at a time so the replica set stays available."""
        self.rolling_restart_cluster(
            context, cluster_id, delay_sec=5)

    def shrink_cluster(self, context, cluster_id, instance_ids):
        LOG.info("Completing MongoDB replica-set shrink for %s", cluster_id)

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
    pass
