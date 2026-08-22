"""Container lifecycle support for the MongoDB Trove MVP.

This adapter intentionally implements only the current container-based
guest-agent lifecycle. Database/user administration, backups, and clusters
remain outside this first image-injection milestone.
"""

import os

from oslo_log import log as logging

from trove.common import cfg
from trove.common import constants
from trove.common import exception
from trove.guestagent.common import operating_system
from trove.guestagent.datastore import service
from trove.guestagent.utils import docker as docker_util
from trove.instance import service_status


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

MONGODB_IMAGE_FILE = "/etc/trove/mongodb-image"
MONGODB_DATA_DIR = "/data/db"


class MongoDbApp(service.BaseDbApp):
    """Start and supervise a single MongoDB container."""

    HEALTHCHECK = {
        "test": [
            "CMD-SHELL",
            "mongosh --quiet --host 127.0.0.1 "
            "--eval 'db.adminCommand({ ping: 1 }).ok'",
        ],
        "start_period": 10 * 1000000000,
        "interval": 10 * 1000000000,
        "timeout": 5 * 1000000000,
        "retries": 3,
    }

    def __init__(self, status, docker_client):
        super(MongoDbApp, self).__init__(status, docker_client)

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

    def start_db(self, update_db=False, ds_version=None):
        """Create the MongoDB container and wait for its health check."""
        data_dir = self._data_dir()
        operating_system.ensure_directory(data_dir, force=True, as_root=True)

        image = self._image()
        port = CONF.mongodb.mongodb_port
        LOG.info("Starting MongoDB container from image %s", image)

        try:
            docker_util.start_container(
                self.docker_client,
                image,
                name="database",
                network_mode=self._network_mode(),
                ports={"%s/tcp" % port: port},
                volumes={
                    data_dir: {"bind": MONGODB_DATA_DIR, "mode": "rw"},
                },
                command=["mongod", "--bind_ip_all"],
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
                CONF.state_change_wait_time, update_db=True):
            raise exception.TroveError("MongoDB container did not become "
                                       "healthy after restart")
