"""MongoDB guest-agent support for the injected Trove image.

MongoDB is run in a container, while Trove keeps the configuration and
replica-set metadata on the guest VM.  Keeping those files outside the
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
        super(MongoConfigurationManager, self).reset_configuration(
            options, remove_overrides=remove_overrides)

    def apply_user_override(self, options, change_id="common"):
        super(MongoConfigurationManager, self).apply_user_override(
            self.normalize(options), change_id=change_id)

    def apply_system_override(self, options, change_id="common",
                              pre_user=False):
        super(MongoConfigurationManager, self).apply_system_override(
            self.normalize(options), change_id=change_id,
            pre_user=pre_user)


class MongoDbApp(service.BaseDbApp):
    """Start and supervise a MongoDB container or replica-set member."""

    HEALTHCHECK = {
        "test": [
            "CMD-SHELL",
            "mongosh --quiet --host 127.0.0.1 "
            "--port 27017 "
            "--eval 'db.adminCommand({ ping: 1 }).ok'",
        ],
        "start_period": 10 * 1000000000,
        "interval": 10 * 1000000000,
        "timeout": 5 * 1000000000,
        "retries": 3,
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
        operating_system.write_file(
            MONGODB_CLUSTER_FILE,
            json.dumps(self.cluster_config, sort_keys=True, indent=2),
            as_root=True)

    def _configuration_contents(self, config_contents=None):
        if isinstance(config_contents, str):
            config_contents = (self.configuration_manager.CODEC.deserialize(
                config_contents) or {})
        config = {
            "storage": {"dbPath": MONGODB_DATA_DIR},
        }
        return MongoConfigurationManager._merge(
            config, MongoConfigurationManager.normalize(config_contents))

    def prepare_configuration(self, config_contents=None):
        self.configuration_manager.reset_configuration(
            self._configuration_contents(config_contents))

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

    @staticmethod
    def _network_mode():
        if CONF.network_isolation and \
                os.path.exists(constants.ETH1_CONFIG_PATH):
            return constants.DOCKER_HOST_NIC_MODE
        return constants.DOCKER_BRIDGE_MODE

    def _container_command(self):
        port = str(CONF.mongodb.mongodb_port)
        command = [
            "mongod",
            "--config", "/etc/mongod.conf",
            "--dbpath", MONGODB_DATA_DIR,
            "--bind_ip_all",
            "--port", port,
        ]
        cluster = self.cluster_config or {}
        replica_set_name = cluster.get("replica_set_name")
        if replica_set_name and cluster.get("instance_type", "member") == \
                "member":
            command.extend(["--replSet", replica_set_name])

        key = cluster.get("key")
        if key:
            operating_system.write_file(
                MONGODB_KEY_FILE, str(key), as_root=True)
            os.chmod(MONGODB_KEY_FILE, 0o600)
            command.extend(["--keyFile", "/etc/mongodb-keyfile"])
        return command

    def _container_volumes(self, data_dir):
        volumes = {
            data_dir: {"bind": MONGODB_DATA_DIR, "mode": "rw"},
            MONGODB_CONFIG_FILE: {
                "bind": "/etc/mongod.conf", "mode": "ro"},
        }
        if (self.cluster_config or {}).get("key"):
            volumes[MONGODB_KEY_FILE] = {
                "bind": "/etc/mongodb-keyfile", "mode": "ro"}
        return volumes

    def start_db(self, update_db=False, ds_version=None):
        """Create the MongoDB container and wait for its health check."""
        data_dir = self._data_dir()
        operating_system.ensure_directory(data_dir, force=True, as_root=True)

        image = self._image()
        LOG.info("Starting MongoDB container from image %s", image)

        try:
            docker_util.start_container(
                self.docker_client,
                image,
                name="database",
                network_mode=self._network_mode(),
                ports={"%s/tcp" % CONF.mongodb.mongodb_port:
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

    def _run_mongosh(self, script):
        output = docker_util.run_command(
            self.docker_client,
            ["mongosh", "--quiet", "--host", "127.0.0.1", "--port",
             str(CONF.mongodb.mongodb_port), "--eval", script])
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return output.strip()

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
        """Initialize this member and wait until it is primary."""
        if not self.cluster_config or not self.cluster_config.get(
                "replica_set_name"):
            raise exception.TroveError(
                "MongoDB replica-set metadata is not configured")
        config = {
            "_id": self.cluster_config["replica_set_name"],
            "members": self._member_documents(members),
        }
        script = (
            "const config = %s; "
            "try { rs.status(); print(JSON.stringify({already: true})); } "
            "catch (e) { print(JSON.stringify(rs.initiate(config))); }"
        ) % json.dumps(config, separators=(",", ":"))
        self._run_mongosh(script)
        self._wait_for_primary()
        return True

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
        script = (
            "const hosts = %s; "
            "const hello = db.hello(); "
            "if (hello.isWritablePrimary && hosts.includes(hello.me)) { "
            "try { rs.stepDown(60, 15); } catch (e) {} "
            "print(JSON.stringify({ok: 1, stepped_down: true})); "
            "} else { "
            "for (const host of hosts) { rs.remove(host); } "
            "print(JSON.stringify({ok: 1, removed: true, hosts: hosts})); "
            "}"
        ) % json.dumps(hosts, separators=(",", ":"))
        output = self._run_mongosh(script)
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
