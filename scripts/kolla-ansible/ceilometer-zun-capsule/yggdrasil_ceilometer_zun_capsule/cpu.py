"""Poll Zun capsule container CPU utilization for Senlin autoscaling."""

from urllib.parse import urljoin, urlsplit

from ceilometer import keystone_client
from ceilometer import sample
from ceilometer.polling import plugin_base
from oslo_log import log


LOG = log.getLogger(__name__)
METER_NAME = "yggdrasil.zun.capsule.cpu_util"
RESOURCE_ID_PREFIX = "yggdrasil_senlin_capsule_"


class CapsuleCpuUtilPollster(plugin_base.PollsterBase):
    """Emit one CPU gauge for each Senlin-owned Zun capsule."""

    @property
    def default_discovery(self):
        return None

    def _api_context(self, manager):
        client = manager.keystone
        catalog = keystone_client.get_service_catalog(client)
        interface = getattr(self.conf.service_credentials, "interface", "internal")
        region = getattr(self.conf.service_credentials, "region_name", None)
        endpoint = catalog.url_for(
            service_type="container", interface=interface, region_name=region
        )
        if not endpoint:
            raise RuntimeError("No Zun endpoint is registered in the service catalog")
        return client.session, endpoint.rstrip("/") + "/"

    @staticmethod
    def _json(response):
        response.raise_for_status()
        return response.json() if response.content else {}

    def _capsules(self, session, base_url):
        next_url = urljoin(base_url, "capsules/")
        params = {"all_projects": 1, "limit": 1000}
        base_parts = urlsplit(base_url)
        while next_url:
            parts = urlsplit(next_url)
            if (parts.scheme, parts.netloc) != (base_parts.scheme, base_parts.netloc):
                raise RuntimeError("Zun capsule pagination returned a different host")
            payload = self._json(session.get(next_url, params=params, timeout=30))
            params = None
            for capsule in payload.get("capsules", []):
                yield capsule
            next_url = payload.get("next")
            if next_url:
                next_url = urljoin(base_url, next_url)

    @staticmethod
    def _container_ids(capsule):
        for container in capsule.get("containers") or []:
            if isinstance(container, str):
                yield container
            elif isinstance(container, dict):
                container_id = container.get("uuid") or container.get("id")
                if container_id:
                    yield container_id

    def _container_cpu(self, session, base_url, container_id):
        try:
            response = session.get(
                urljoin(base_url, f"containers/{container_id}/stats"), timeout=30
            )
            payload = self._json(response)
            if "stats_info" in payload and isinstance(payload["stats_info"], dict):
                payload = payload["stats_info"]
            value = payload.get("CPU %")
            if value is None:
                return None
            return float(str(value).strip().rstrip("%"))
        except Exception as error:
            LOG.warning(
                "Could not poll CPU for Zun container %s: %s", container_id, error
            )
            return None

    def get_samples(self, manager, cache, resources):
        session, base_url = self._api_context(manager)
        for capsule in self._capsules(session, base_url):
            labels = capsule.get("labels") or {}
            cluster_id = labels.get("yggdrasil_senlin_cluster_id")
            if not cluster_id:
                continue

            cpu_values = [
                value
                for container_id in self._container_ids(capsule)
                if (value := self._container_cpu(session, base_url, container_id))
                is not None
            ]
            if not cpu_values:
                continue

            capsule_id = capsule.get("uuid") or capsule.get("id")
            node_id = labels.get("yggdrasil_senlin_node_id") or capsule_id
            if not capsule_id or not node_id:
                LOG.warning("Skipping Zun capsule with incomplete Senlin labels")
                continue

            original_resource_id = (
                f"{RESOURCE_ID_PREFIX}{cluster_id}_{node_id}"
            )
            yield sample.Sample(
                name=METER_NAME,
                type=sample.TYPE_GAUGE,
                unit="%",
                volume=sum(cpu_values) / len(cpu_values),
                user_id=capsule.get("user_id"),
                project_id=capsule.get("project_id"),
                resource_id=capsule_id,
                resource_metadata={
                    "original_resource_id": original_resource_id,
                    "cluster_id": str(cluster_id),
                    "node_id": str(node_id),
                    "display_name": capsule.get("name", ""),
                },
            )
