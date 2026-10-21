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

    def _capsule_cpu(self, session, base_url, capsule_id):
        try:
            response = session.get(
                urljoin(base_url, f"capsules/{capsule_id}/stats"),
                params={"all_projects": 1},
                timeout=30,
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
                "Could not poll CPU for Zun capsule %s: %s", capsule_id, error
            )
            return None

    def get_samples(self, manager, cache, resources):
        session, base_url = self._api_context(manager)
        for capsule in self._capsules(session, base_url):
            # Zun serializes Kubernetes-style capsule labels as meta_labels.
            # Keep the labels fallback for older API representations.
            labels = capsule.get("meta_labels") or capsule.get("labels") or {}
            cluster_id = labels.get("yggdrasil_senlin_cluster_id")
            if not cluster_id:
                continue

            capsule_id = capsule.get("uuid") or capsule.get("id")
            node_id = labels.get("yggdrasil_senlin_node_id") or capsule_id
            if not capsule_id or not node_id:
                LOG.warning("Skipping Zun capsule with incomplete Senlin labels")
                continue

            cpu_utilization = self._capsule_cpu(session, base_url, capsule_id)
            if cpu_utilization is None:
                continue

            senlin_resource_id = (
                f"{RESOURCE_ID_PREFIX}{cluster_id}_{node_id}"
            )
            yield sample.Sample(
                name=METER_NAME,
                type=sample.TYPE_GAUGE,
                unit="%",
                volume=cpu_utilization,
                user_id=capsule.get("user_id"),
                project_id=capsule.get("project_id"),
                resource_id=capsule_id,
                resource_metadata={
                    "yggdrasil_senlin_resource_id": senlin_resource_id,
                    "cluster_id": str(cluster_id),
                    "node_id": str(node_id),
                    "display_name": capsule.get("name", ""),
                },
            )
