"""Trove guest RPC methods used by the MongoDB topology controller."""

from oslo_log import log as logging

from trove.common import cfg
from trove.common.strategies.cluster import base
from trove.guestagent import api as guest_api


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class MongoDbGuestAgentStrategy(base.BaseGuestAgentStrategy):

    @property
    def guest_client_class(self):
        return MongoDbGuestAgentAPI


class MongoDbGuestAgentAPI(guest_api.API):

    def _topology_timeout(self):
        """Give topology changes enough time to converge on small guests.

        MongoDB replica-set and sharding operations can legitimately wait for
        several guests to become reachable.  Trove's generic high timeout is
        shorter than the MongoDB guest strategy's retry budget, which causes
        the controller to abandon a still-running guest RPC.  Keep the
        generic timeout as the floor and extend only this datastore's
        topology calls.
        """
        return max(
            self.agent_high_timeout,
            CONF.mongodb.add_members_timeout + 60)

    def initialize_replica_set(self, members):
        LOG.debug("Initializing MongoDB replica set with members %s", members)
        return self._call(
            "initialize_replica_set", self._topology_timeout(),
            version=self.API_BASE_VERSION, members=members)

    def prep_primary(self):
        return self._call(
            "prep_primary", self._topology_timeout(),
            version=self.API_BASE_VERSION)

    def add_members(self, members):
        return self._call(
            "add_members", self._topology_timeout(),
            version=self.API_BASE_VERSION, members=members)

    def add_replica_members(self, members):
        LOG.debug("Adding MongoDB replica-set members %s", members)
        return self._call(
            "add_replica_members", self._topology_timeout(),
            version=self.API_BASE_VERSION, members=members)

    def remove_replica_members(self, members):
        LOG.debug("Removing MongoDB replica-set members %s", members)
        return self._call(
            "remove_replica_members", self._topology_timeout(),
            version=self.API_BASE_VERSION, members=members)

    def replica_set_status(self):
        return self._call(
            "replica_set_status", self.agent_low_timeout,
            version=self.API_BASE_VERSION)

    def add_config_servers(self, config_servers):
        return self._call(
            "add_config_servers", self._topology_timeout(),
            version=self.API_BASE_VERSION, config_servers=config_servers)

    def add_shard(self, replica_set_name, replica_set_member):
        return self._call(
            "add_shard", self._topology_timeout(),
            version=self.API_BASE_VERSION,
            replica_set_name=replica_set_name,
            replica_set_member=replica_set_member)

    def remove_shard(self, replica_set_name):
        return self._call(
            "remove_shard", self._topology_timeout(),
            version=self.API_BASE_VERSION,
            replica_set_name=replica_set_name)

    def is_shard_active(self, replica_set_name):
        return self._call(
            "is_shard_active", self.agent_low_timeout,
            version=self.API_BASE_VERSION,
            replica_set_name=replica_set_name)

    def get_replica_set_name(self):
        return self._call(
            "get_replica_set_name", self.agent_low_timeout,
            version=self.API_BASE_VERSION)

    def get_key(self):
        return self._call(
            "get_key", self.agent_low_timeout,
            version=self.API_BASE_VERSION)

    def cluster_complete(self):
        return self._call(
            "cluster_complete", self._topology_timeout(),
            version=self.API_BASE_VERSION)
