"""Trove guest RPC methods used by the MongoDB replica-set controller."""

from oslo_log import log as logging

from trove.common.strategies.cluster import base
from trove.guestagent import api as guest_api


LOG = logging.getLogger(__name__)


class MongoDbGuestAgentStrategy(base.BaseGuestAgentStrategy):

    @property
    def guest_client_class(self):
        return MongoDbGuestAgentAPI


class MongoDbGuestAgentAPI(guest_api.API):

    def initialize_replica_set(self, members):
        LOG.debug("Initializing MongoDB replica set with members %s", members)
        return self._call(
            "initialize_replica_set", self.agent_high_timeout,
            version=self.API_BASE_VERSION, members=members)

    def add_replica_members(self, members):
        LOG.debug("Adding MongoDB replica-set members %s", members)
        return self._call(
            "add_replica_members", self.agent_high_timeout,
            version=self.API_BASE_VERSION, members=members)

    def remove_replica_members(self, members):
        LOG.debug("Removing MongoDB replica-set members %s", members)
        return self._call(
            "remove_replica_members", self.agent_high_timeout,
            version=self.API_BASE_VERSION, members=members)

    def replica_set_status(self):
        return self._call(
            "replica_set_status", self.agent_low_timeout,
            version=self.API_BASE_VERSION)
