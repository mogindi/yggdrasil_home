"""MongoDB guest-agent support for the injected Trove image.

MongoDB is run in a container, while Trove keeps the configuration and
topology metadata on the guest VM.  Keeping those files outside the
container is important: a container restart must pick up a changed Trove
configuration without recreating the data container.
"""

import copy
import json
import os
import time

from oslo_log import log as logging

from trove.common import cfg
from trove.common import constants
from trove.common import exception
from trove.common import stream_codecs
from trove.guestagent.common import operating_system
from trove.guestagent.common import configuration
from trove.guestagent.datastore import service
from trove.guestagent.utils import docker as docker_util
from trove.instance import service_status


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

MONGODB_IMAGE_FILE = "/etc/trove/mongodb-image"
MONGODB_CONFIG_FILE = "/etc/trove/mongodb.conf"
MONGODB_CLUSTER_FILE = "/etc/trove/mongodb-cluster.json"
MONGODB_KEY_FILE = "/etc/trove/mongodb-keyfile"
MONGODB_DATA_DIR = "/data/db"


class MongoConfigurationManager(configuration.ConfigurationManager):
    """Configuration manager that accepts Trove's dotted MongoDB keys.

    Trove's MongoDB validation rules use names such as
    ``systemLog.verbosity``.  MongoDB's YAML configuration is naturally
    nested, so normalize dotted keys before writing the file.  This also
    makes configuration groups and the guest's base configuration compose
    correctly instead of creating two unrelated top-level keys.
    """

    CODEC = stream_codecs.SafeYamlCodec(default_flow_style=False)

    @classmethod
    def _merge(cls, target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
        return target

    @classmethod
    def _normalize(cls, options):
        if not options:
            return {}

        normalized = {}
        for key, value in options.items():
            if not isinstance(key, str) or "." not in key:
                if isinstance(value, dict):
                    value = cls._normalize(value)
                normalized[key] = copy.deepcopy(value)
                continue

            target = normalized
            parts = [part for part in key.split(".") if part]
            for part in parts[:-1]:
                existing = target.get(part)
                if not isinstance(existing, dict):
                    existing = {}
                    target[part] = existing
                target = existing
            target[parts[-1]] = copy.deepcopy(value)

        return normalized

    @classmethod
    def _sanitize(cls, options):
        """Remove options removed with MMAPv1 from modern MongoDB.

        The stock Trove MongoDB template still contains an old MMAPv1
        setting.  Passing it to MongoDB 8 makes the daemon reject the whole
        configuration, so discard only that obsolete subtree.
        """
        options = copy.deepcopy(options or {})
        storage = options.get("storage")
        if isinstance(storage, dict):
            storage.pop("mmapv1", None)
            journal = storage.get("journal")
            if isinstance(journal, dict):
                # Journaling is always enabled in MongoDB 6.1 and later;
                # the old enabled switch was removed from mongod.
                journal.pop("enabled", None)
                if not journal:
                    storage.pop("journal", None)
            if not storage:
                options.pop("storage", None)
        system_log = options.get("systemLog")
        if isinstance(system_log, dict) and \
                system_log.get("destination") == "stderr":
            # MongoDB accepts "file" and "syslog" here, but not the
            # Docker-friendly value used by some older Trove templates.
            system_log.pop("destination", None)
            if not system_log:
                options.pop("systemLog", None)
        return options

    @classmethod
    def normalize(cls, options):
        return cls._sanitize(cls._normalize(options))

    def reset_configuration(self, options, remove_overrides=False):
        if isinstance(options, str):
            options = self._codec.deserialize(options) or {}
        options = self.normalize(options)
        # ConfigurationManager.reset_configuration dispatches back to
        # ``self.reset_configuration`` when passed a dict.  Serialize first
        # so the base implementation takes its file-writing path instead of
        # recursing through this override.
        configuration.ConfigurationManager.reset_configuration(
            self, self._codec.serialize(options),
            remove_overrides=remove_overrides)

    def apply_user_override(self, options, change_id="common"):
        super(MongoConfigurationManager, self).apply_user_override(
            self.normalize(options), change_id=change_id)

    def apply_system_override(self, options, change_id="common",
                              pre_user=False):
        super(MongoConfigurationManager, self).apply_system_override(
            self.normalize(options), change_id=change_id,
            pre_user=pre_user)


class MongoDbApp(service.BaseDbApp):
    """Start and supervise a MongoDB service role.

    A normal instance runs ``mongod``.  Replica-set members add the
    appropriate replica-set options, while sharded clusters use the same
    image for config servers, shard members, and query routers (``mongos``).
    Query routers are initially started as a temporary standalone ``mongod``
    because their config-server addresses are not known until Nova has
    assigned addresses to all cluster instances.
    """

    HEALTHCHECK = {
        "test": [
            "CMD-SHELL",
            "mongosh --quiet --host 127.0.0.1 "
            "--port 27017 "
            "--eval 'db.adminCommand({ ping: 1 }).ok'",
        ],
        # mongosh starts a Node.js process for every probe.  On the small
        # Trove flavors that can briefly exceed five seconds while the guest
        # agent, mongod, and the VM are contending for memory.  A transient
        # slow probe must not turn a healthy database into CRASHED while a
        # cluster operation is still converging.
        "start_period": 30 * 1000000000,
        "interval": 15 * 1000000000,
        "timeout": 15 * 1000000000,
        "retries": 6,
    }

    def __init__(self, status, docker_client):
        super(MongoDbApp, self).__init__(status, docker_client)
        self.configuration_manager = MongoConfigurationManager(
            MONGODB_CONFIG_FILE,
            owner="root",
            group="root",
            codec=MongoConfigurationManager.CODEC,
            requires_root=True)
        self.cluster_config = self._read_cluster_config()

    @staticmethod
    def _read_cluster_config():
        try:
            with open(MONGODB_CLUSTER_FILE) as cluster_file:
                return json.load(cluster_file)
        except (OSError, ValueError):
            return None

    def _write_cluster_config(self):
        operating_system.write_file(
            MONGODB_CLUSTER_FILE,
            json.dumps(self.cluster_config, sort_keys=True, indent=2),
            as_root=True)

    def configure_cluster(self, cluster_config):
        """Persist cluster metadata before the first container start."""
        if not cluster_config:
            self.cluster_config = None
            try:
                operating_system.remove(MONGODB_CLUSTER_FILE, force=True,
                                        as_root=True)
            except Exception:
                LOG.debug("No MongoDB cluster metadata to remove",
                          exc_info=True)
            return

        self.cluster_config = dict(cluster_config)
        self._write_cluster_config()

    def _instance_type(self):
        return (self.cluster_config or {}).get("instance_type", "member")

    def _config_server_hosts(self):
        return [str(host) for host in
                (self.cluster_config or {}).get("config_servers", [])]

    def _config_server_replica_set(self):
        return (self.cluster_config or {}).get(
            "config_server_replica_set_name", "configRS")

    def _configuration_contents(self, config_contents=None, memory_mb=None):
        if isinstance(config_contents, str):
            config_contents = (self.configuration_manager.CODEC.deserialize(
                config_contents) or {})
        config = {
            "storage": {"dbPath": MONGODB_DATA_DIR},
        }
        if memory_mb:
            try:
                memory_mb = int(memory_mb)
            except (TypeError, ValueError):
                memory_mb = 0
        if memory_mb > 0:
            # Leave most of a small guest for Ubuntu, Trove, Docker, and
            # mongosh.  An explicit user configuration still wins during the
            # merge below.  This keeps MongoDB from exhausting 512/768 MiB
            # guests before the guest agent can finish topology work.
            # MongoDB requires at least 0.256 GB for this setting.  Keep
            # that floor even on the smallest test flavor; values below it
            # make mongod reject the configuration before its health check
            # can ever pass.
            cache_size_gb = min(max(memory_mb * 0.25 / 1024, 0.256), 2.0)
            config["storage"]["wiredTiger"] = {
                "engineConfig": {"cacheSizeGB": cache_size_gb},
            }
        return MongoConfigurationManager._merge(
            config, MongoConfigurationManager.normalize(config_contents))

    def prepare_configuration(self, config_contents=None, memory_mb=None):
        self.configuration_manager.reset_configuration(
            self._configuration_contents(config_contents, memory_mb=memory_mb))

    def update_overrides(self, overrides):
        if overrides:
            self.configuration_manager.apply_user_override(overrides)

    def apply_overrides(self, overrides):
        """Apply a configuration group and make the running daemon use it.

        MongoDB parameters exposed by the Trove validation rules are marked
        restart-required.  This method is still implemented for callers that
        provide a dynamic parameter: restarting the container is safe and
        guarantees that the complete normalized configuration is active.
        Cluster callers invoke this one member at a time.
        """
        self.update_overrides(overrides)
        self.restart()

    @staticmethod
    def _image():
        """Return the image selected while the guest image was prepared."""
        image = "mongo"
        try:
            with open(MONGODB_IMAGE_FILE) as image_file:
                image = image_file.read().strip() or image
        except OSError:
            LOG.warning("MongoDB image file %s is unavailable; using %s",
                        MONGODB_IMAGE_FILE, image)

        if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
            image = "%s:%s" % (image, CONF.datastore_version or "8.0")
        return image

    @staticmethod
    def _data_dir():
        return cfg.get_configuration_property("mount_point") \
            or "/var/lib/mongodb"

    def _network_mode(self):
        # Cluster members advertise their VM addresses to MongoDB.  Bridge
        # networking gives mongod a container-only identity, so MongoDB can
        # reject rs.initiate() with "No host described ... maps to this
        # node".  Use the guest's host network for every clustered role so
        # the advertised Trove address is also the address MongoDB sees.
        if self.cluster_config:
            return "host"
        if CONF.network_isolation and \
                os.path.exists(constants.ETH1_CONFIG_PATH):
            return constants.DOCKER_HOST_NIC_MODE
        return constants.DOCKER_BRIDGE_MODE

    def _container_command(self):
        port = str(CONF.mongodb.mongodb_port)
        cluster = self.cluster_config or {}
        instance_type = self._instance_type()

        if instance_type == "query_router" and self._config_server_hosts():
            configdb = "%s/%s" % (
                self._config_server_replica_set(),
                ",".join(self._config_server_hosts()))
            command = [
                "mongos",
                "--configdb", configdb,
                "--bind_ip_all",
                "--port", port,
            ]
        else:
            # A query router without addresses is a temporary standalone
            # mongod.  It lets Trove mark the Nova instance healthy while the
            # task manager discovers the addresses of all cluster members.
            command = [
                "mongod",
                "--config", "/etc/mongod.conf",
                "--dbpath", MONGODB_DATA_DIR,
                "--bind_ip_all",
                "--port", port,
            ]

            replica_set_name = cluster.get("replica_set_name")
            if replica_set_name and instance_type in (
                    "member", "config_server"):
                command.extend(["--replSet", replica_set_name])
            if instance_type == "config_server":
                command.append("--configsvr")
            elif instance_type == "member" and cluster.get(
                    "topology") == "sharded":
                command.append("--shardsvr")

        key = cluster.get("key")
        if key:
            operating_system.write_file(
                MONGODB_KEY_FILE, str(key), as_root=True)
            os.chmod(MONGODB_KEY_FILE, 0o600)
            command.extend(["--keyFile", "/etc/mongodb-keyfile"])
        return command

    def _container_volumes(self, data_dir):
        volumes = {}
        if not (self._instance_type() == "query_router" and
                self._config_server_hosts()):
            volumes.update({
                data_dir: {"bind": MONGODB_DATA_DIR, "mode": "rw"},
                MONGODB_CONFIG_FILE: {
                    "bind": "/etc/mongod.conf", "mode": "ro"},
            })
        if (self.cluster_config or {}).get("key"):
            volumes[MONGODB_KEY_FILE] = {
                "bind": "/etc/mongodb-keyfile", "mode": "ro"}
        return volumes

    def start_db(self, update_db=False, ds_version=None):
        """Create the MongoDB container and wait for its health check."""
        data_dir = self._data_dir()
        operating_system.ensure_directory(data_dir, force=True, as_root=True)

        image = self._image()
        network_mode = self._network_mode()
        LOG.info("Starting MongoDB container from image %s", image)

        try:
            docker_util.start_container(
                self.docker_client,
                image,
                name="database",
                network_mode=network_mode,
                ports={} if network_mode == "host" else {
                    "%s/tcp" % CONF.mongodb.mongodb_port:
                    CONF.mongodb.mongodb_port},
                volumes=self._container_volumes(data_dir),
                command=self._container_command(),
                healthcheck=self.HEALTHCHECK,
            )
        except Exception:
            LOG.exception("Failed to start MongoDB container")
            raise exception.TroveError("Failed to start MongoDB container")

        if not self.status.wait_for_status(
                service_status.ServiceStatuses.HEALTHY,
                CONF.state_change_wait_time, update_db):
            raise exception.TroveError("MongoDB container did not become "
                                       "healthy")

    def restart(self):
        LOG.info("Restarting MongoDB container")
        try:
            docker_util.restart_container(self.docker_client)
        except Exception:
            LOG.exception("Failed to restart MongoDB container")
            raise exception.TroveError("Failed to restart MongoDB container")

        if not self.status.wait_for_status(
                service_status.ServiceStatuses.HEALTHY,
                CONF.state_change_wait_time, update_db=False):
            raise exception.TroveError("MongoDB container did not become "
                                       "healthy after restart")

    def _replace_container(self):
        """Recreate the container after changing its process role."""
        try:
            docker_util.stop_container(self.docker_client)
        except Exception:
            LOG.debug("MongoDB container was already stopped", exc_info=True)
        try:
            docker_util.remove_container(self.docker_client)
        except Exception:
            LOG.debug("MongoDB container was already removed", exc_info=True)
        self.start_db(update_db=False)

    def _run_mongosh(self, script, host="127.0.0.1"):
        output = docker_util.run_command(
            self.docker_client,
            ["mongosh", "--quiet", "--host", host, "--port",
             str(CONF.mongodb.mongodb_port), "--eval", script])
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return output.strip()

    @staticmethod
    def _mongosh_host(host):
        """Return a host value suitable for mongosh's ``--host`` option."""
        host = str(host or "")
        if host.startswith("[") and "]" in host:
            return host[1:host.index("]")]
        if host.count(":") == 1:
            return host.rsplit(":", 1)[0]
        return host

    def _replica_hello(self):
        output = self._run_mongosh("JSON.stringify(db.hello())")
        try:
            return json.loads(output)
        except ValueError as exc:
            raise exception.TroveError(
                "MongoDB did not return replica-set hello data: %s" %
                output) from exc

    def _wait_for_primary(self):
        deadline = time.time() + CONF.mongodb.add_members_timeout
        while time.time() < deadline:
            try:
                result = self._run_mongosh(
                    "JSON.stringify(rs.status().myState)")
                if result.rstrip().endswith("1"):
                    return
            except Exception:
                LOG.debug("MongoDB replica set is not primary yet",
                          exc_info=True)
            time.sleep(2)
        raise exception.TroveError(
            "MongoDB replica set did not elect a primary in time")

    @staticmethod
    def _member_documents(members):
        documents = []
        for index, member in enumerate(members):
            host = str(member)
            documents.append({"_id": index, "host": host})
        return documents

    def initialize_replica_set(self, members):
        """Initialize this member's replica set and wait until primary.

        Guest readiness means that each local MongoDB process is healthy, but
        the Neutron path between freshly-created VMs can lag by a few seconds.
        MongoDB's quorum check is strict, so retry only the transient network
        and quorum errors until the normal cluster operation timeout expires.
        """
        if not self.cluster_config or not self.cluster_config.get(
                "replica_set_name"):
            raise exception.TroveError(
                "MongoDB replica-set metadata is not configured")
        config = {
            "_id": self.cluster_config["replica_set_name"],
            "members": self._member_documents(members),
        }
        if self._instance_type() == "config_server":
            config["configsvr"] = True
        script = (
            "const config = %s; "
            "try { rs.status(); print(JSON.stringify({already: true})); } "
            "catch (e) { print(JSON.stringify(rs.initiate(config))); }"
        ) % json.dumps(config, separators=(",", ":"))
        deadline = time.time() + CONF.mongodb.add_members_timeout
        last_error = None
        while time.time() < deadline:
            try:
                self._run_mongosh(script)
                self._wait_for_primary()
                return True
            except Exception as exc:
                message = str(exc)
                transient = any(marker in message for marker in (
                    "quorum check failed",
                    "No route to host",
                    "Connection refused",
                    "Connection timed out",
                    "No host described in new configuration"))
                if not transient:
                    raise
                last_error = exc
                LOG.warning(
                    "MongoDB replica-set peers are not reachable yet; "
                    "retrying initialization: %s", message)
                time.sleep(2)

        raise exception.TroveError(
            "MongoDB replica set did not become reachable in time: %s" %
            last_error)

    def prep_primary(self):
        """Compatibility operation used by Trove's stock MongoDB strategy."""
        return True

    def add_members(self, members):
        """Compatibility alias for the stock MongoDB guest API."""
        return self.add_replica_members(members)

    def add_replica_members(self, members):
        """Add new members to the existing replica set."""
        hosts = [str(member) for member in members]
        script = (
            "const hosts = %s; "
            "for (const host of hosts) { "
            "try { rs.add(host); } catch (e) { "
            "if (e.codeName !== 'NewReplicaSetConfigurationIncompatible' && "
            "e.codeName !== 'AlreadyInitialized') throw e; } } "
            "print(JSON.stringify({ok: 1, hosts: hosts}));"
        ) % json.dumps(hosts, separators=(",", ":"))
        self._run_mongosh(script)
        return True

    def remove_replica_members(self, members):
        """Remove members from this replica set before Nova deletion."""
        hosts = [str(member) for member in members]
        hello = self._replica_hello()
        local_host = hello.get("me")
        primary_host = hello.get("primary")
        if hello.get("isWritablePrimary"):
            primary_host = local_host or primary_host
        if not primary_host:
            raise exception.TroveError(
                "MongoDB replica set has no elected primary")

        def host_matches(left, right):
            return (str(left or "") == str(right or "") or
                    self._mongosh_host(left) == self._mongosh_host(right))

        primary_is_removed = any(
            host_matches(primary_host, host) for host in hosts)
        if primary_is_removed:
            script = (
                "try { rs.stepDown(60, 15); } catch (e) {} "
                "print(JSON.stringify({ok: 1, stepped_down: true}));"
            )
        else:
            script = (
                "const hosts = %s; "
                "for (const host of hosts) { "
                "try { rs.remove(host); } catch (e) { "
                "if (e.codeName !== 'MemberNotFound') throw e; } } "
                "print(JSON.stringify({ok: 1, removed: true, hosts: hosts}));"
            ) % json.dumps(hosts, separators=(",", ":"))

        # Trove can call this operation through any member.  MongoDB only
        # accepts replSet reconfiguration on the writable primary, so route
        # the command to the elected primary instead of attempting rs.remove
        # on a secondary and turning an expected topology transition into an
        # RPC error.
        output = self._run_mongosh(
            script, host=self._mongosh_host(primary_host))
        try:
            return json.loads(output)
        except ValueError:
            return {"ok": 1, "removed": True, "hosts": hosts}

    def replica_set_status(self):
        output = self._run_mongosh(
            "JSON.stringify(rs.status(), (key, value) => "
            "key === 'lastHeartbeat' || key === 'lastHeartbeatRecv' ? "
            "undefined : value)")
        return json.loads(output)

    def get_replica_set_name(self):
        return (self.cluster_config or {}).get("replica_set_name")

    def get_key(self):
        return (self.cluster_config or {}).get("key")

    def add_config_servers(self, config_servers):
        """Configure a query router and switch it from mongod to mongos."""
        if self._instance_type() != "query_router":
            raise exception.TroveError(
                "Config-server addresses can only be applied to a query "
                "router")
        hosts = [str(host) for host in config_servers]
        if not hosts:
            raise exception.TroveError(
                "At least one config server is required for mongos")
        self.cluster_config["config_servers"] = hosts
        self._write_cluster_config()
        self._replace_container()
        return True

    def add_shard(self, replica_set_name, replica_set_member):
        """Register a replica set with the local mongos router."""
        if self._instance_type() != "query_router" or not \
                self._config_server_hosts():
            raise exception.TroveError(
                "MongoDB shard registration requires a configured mongos")
        connection = "%s/%s" % (replica_set_name, replica_set_member)
        script = (
            "const connection = %s; "
            "const name = %s; "
            "const existing = db.adminCommand({listShards: 1}); "
            "const found = (existing.shards || []).some((shard) => "
            "shard._id === name); "
            "print(JSON.stringify(found ? {ok: 1, already: true} : "
            "db.adminCommand({addShard: connection})));"
        ) % (json.dumps(connection), json.dumps(replica_set_name))
        return json.loads(self._run_mongosh(script))

    def remove_shard(self, replica_set_name):
        """Start removal of a shard through the local mongos router."""
        if self._instance_type() != "query_router" or not \
                self._config_server_hosts():
            raise exception.TroveError(
                "MongoDB shard removal requires a configured mongos")
        script = (
            "const name = %s; "
            "const result = db.adminCommand({listShards: 1}); "
            "const found = (result.shards || []).some((shard) => "
            "shard._id === name); "
            "print(JSON.stringify(found ? "
            "db.adminCommand({removeShard: name}) : "
            "{ok: 1, state: 'completed', already: true}));"
        ) % json.dumps(str(replica_set_name))
        return json.loads(self._run_mongosh(script))

    def is_shard_active(self, replica_set_name):
        """Return whether a replica set is still registered as a shard."""
        if self._instance_type() != "query_router" or not \
                self._config_server_hosts():
            raise exception.TroveError(
                "MongoDB shard status requires a configured mongos")
        script = (
            "const result = db.adminCommand({listShards: 1}); "
            "print(JSON.stringify((result.shards || []).some((shard) => "
            "shard._id === %s)));"
        ) % json.dumps(str(replica_set_name))
        return json.loads(self._run_mongosh(script))

    def cluster_complete(self):
        """Finish clustered preparation and publish the real service state.

        Trove deliberately leaves clustered guests in ``INSTANCE_READY``
        while the task manager assembles the topology.  The stock MongoDB
        guest strategy does not provide a completion hook, so without this
        transition the prepare marker is never written and the guest-agent
        heartbeat expires shortly after cluster creation.
        """
        status = self.status.get_actual_db_status()
        if status != service_status.ServiceStatuses.HEALTHY:
            raise exception.TroveError(
                "MongoDB container is not healthy after cluster setup: %s" %
                status)
        self.status.set_ready()
        self.status.set_status(status, force=True)
        return True
