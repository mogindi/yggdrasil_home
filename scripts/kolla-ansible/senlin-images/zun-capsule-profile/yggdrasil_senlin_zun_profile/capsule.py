"""Senlin profile that manages one Zun capsule per cluster node."""

import copy
import re
import time
from urllib.parse import urljoin

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from senlin.common import exception as exc
from senlin.common import schema
from senlin.drivers import sdk
from senlin.profiles import base


LOG = logging.getLogger(__name__)

_CREATE_TIMEOUT = 600
_DELETE_TIMEOUT = 300
_POLL_INTERVAL = 2
_READY_STATES = {"Running", "Succeeded"}
_FAILED_STATES = {"Failed", "Error", "Unknown"}


class CapsuleProfile(base.Profile):
    """Create and remove a Zun capsule as a Senlin node resource."""

    VERSIONS = {"1.0": [{"status": "SUPPORTED", "since": "2025.2"}]}
    KEYS = (CONTEXT, TEMPLATE) = ("context", "template")
    properties_schema = {
        CONTEXT: schema.Map(
            "Customized context for capsule operations",
            default={},
        ),
        TEMPLATE: schema.Map("Kubernetes-style Zun capsule template", required=True),
    }

    def do_validate(self, obj):
        template = self.properties[self.TEMPLATE]
        if not isinstance(template, dict) or template.get("kind") != "capsule":
            raise exc.InvalidSpec(message="template.kind must be 'capsule'")

        spec = template.get("spec") or {}
        if not isinstance(spec, dict):
            raise exc.InvalidSpec(message="template.spec must be a mapping")
        containers = spec.get("containers") or []
        if not containers:
            raise exc.InvalidSpec(message="template.spec.containers must not be empty")
        if not isinstance(containers, list) or any(
            not isinstance(container, dict) or not container.get("image")
            for container in containers
        ):
            raise exc.InvalidSpec(message="every capsule container needs an image")
        metadata = template.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise exc.InvalidSpec(message="template.metadata must be a mapping")
        labels = metadata.get("labels") or {}
        if not isinstance(labels, dict):
            raise exc.InvalidSpec(message="template.metadata.labels must be a mapping")

    @staticmethod
    def _response_json(response):
        if not response.content:
            return {}
        payload = response.json()
        return payload.get("capsule", payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _is_not_found(error):
        response = getattr(error, "response", None)
        status = getattr(error, "http_status", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        return str(status) == "404"

    @classmethod
    def _get_capsule(cls, session, url):
        try:
            response = session.get(url, timeout=60)
        except Exception as error:
            if cls._is_not_found(error):
                return None
            raise
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response

    def _session_and_base_url(self, obj):
        params = self._build_conn_params(obj.user, obj.project)
        connection = sdk.create_connection(params)
        session = connection.session
        interface = connection.config.get_interface() or "public"
        endpoint = session.get_endpoint(
            service_type="container",
            interface=interface,
            region_name=params.get("region_name") or cfg.CONF.default_region_name,
        )
        if not endpoint:
            raise exc.ResourceNotFound(type="service endpoint", id="container")
        return session, endpoint.rstrip("/") + "/"

    @staticmethod
    def _capsule_url(base_url, capsule_id=None):
        path = "capsules/" if capsule_id is None else f"capsules/{capsule_id}"
        return urljoin(base_url, path)

    @staticmethod
    def _identity(obj):
        cluster_id = str(getattr(obj, "cluster_id", "") or "")
        node_id = str(getattr(obj, "id", "") or "")
        return cluster_id, node_id

    def _make_template(self, obj):
        template = copy.deepcopy(self.properties[self.TEMPLATE])
        metadata = template.setdefault("metadata", {})
        labels = metadata.setdefault("labels", {})
        cluster_id, node_id = self._identity(obj)
        labels.update(
            {
                "yggdrasil_senlin_cluster_id": cluster_id,
                "yggdrasil_senlin_node_id": node_id,
            }
        )

        # Senlin node names are stable and Zun permits the same template to be
        # instantiated repeatedly. Use the node name while preserving the
        # user's other metadata.
        name = re.sub(r"[^a-z0-9-]", "-", str(obj.name).lower()).strip("-")
        if name:
            metadata["name"] = name[:63]
        return template

    def _wait_for_state(self, session, base_url, capsule_id, states, timeout):
        deadline = time.monotonic() + timeout
        last_state = "unknown"
        while time.monotonic() < deadline:
            response = self._get_capsule(
                session, self._capsule_url(base_url, capsule_id)
            )
            if response is None:
                return None
            capsule = self._response_json(response)
            last_state = capsule.get("status", "unknown")
            if last_state in states:
                return capsule
            if last_state in _FAILED_STATES:
                raise RuntimeError(
                    f"Zun capsule {capsule_id} entered {last_state}: "
                    f"{capsule.get('status_reason', 'no status reason')}"
                )
            eventlet.sleep(_POLL_INTERVAL)
        raise TimeoutError(
            f"Timed out waiting for Zun capsule {capsule_id}; last state: {last_state}"
        )

    def _delete_capsule(self, session, base_url, capsule_id):
        url = self._capsule_url(base_url, capsule_id)
        try:
            response = session.delete(url, timeout=60)
            if response.status_code == 404:
                return True
            response.raise_for_status()
        except Exception as error:
            if self._is_not_found(error):
                return True
            # Zun can return an error while cleaning up a capsule whose
            # runtime sandbox already disappeared. Confirm the resource is
            # absent before deciding that deletion failed.
            response = self._get_capsule(session, url)
            if response is None:
                return True
            raise

        deadline = time.monotonic() + _DELETE_TIMEOUT
        while time.monotonic() < deadline:
            response = self._get_capsule(session, url)
            if response is None:
                return True
            eventlet.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"Timed out deleting Zun capsule {capsule_id}")

    def do_create(self, obj):
        session, base_url = self._session_and_base_url(obj)
        template = self._make_template(obj)
        capsule_id = None
        try:
            response = session.post(
                self._capsule_url(base_url), json={"template": template}, timeout=60
            )
            response.raise_for_status()
            capsule = self._response_json(response)
            capsule_id = capsule.get("uuid") or capsule.get("id")
            if not capsule_id:
                raise RuntimeError("Zun did not return the created capsule UUID")
            created_capsule = self._wait_for_state(
                session, base_url, capsule_id, _READY_STATES, _CREATE_TIMEOUT
            )
            if created_capsule is None:
                raise RuntimeError(
                    f"Zun capsule {capsule_id} disappeared before becoming ready"
                )
            return capsule_id
        except Exception as error:
            if capsule_id:
                try:
                    self._delete_capsule(session, base_url, capsule_id)
                except Exception:
                    LOG.exception("Failed to roll back capsule %s", capsule_id)
            raise exc.EResourceCreation(
                type="capsule", message=str(error), resource_id=capsule_id
            ) from error

    def do_delete(self, obj, **params):
        capsule_id = obj.physical_id
        if not capsule_id:
            return True

        session, base_url = self._session_and_base_url(obj)
        try:
            return self._delete_capsule(session, base_url, capsule_id)
        except Exception as error:
            raise exc.EResourceDeletion(
                type="capsule", id=capsule_id, message=str(error)
            ) from error

    def do_check(self, obj):
        if not obj.physical_id:
            return False
        session, base_url = self._session_and_base_url(obj)
        try:
            response = self._get_capsule(
                session, self._capsule_url(base_url, obj.physical_id)
            )
            if response is None:
                return False
            return self._response_json(response).get("status") in _READY_STATES
        except Exception as error:
            LOG.warning("Could not check Zun capsule %s: %s", obj.physical_id, error)
            return False

    def do_get_details(self, obj):
        """Return Capsule details, including Neutron addresses, for
        policies.
        """
        if not obj.physical_id:
            return {}
        session, base_url = self._session_and_base_url(obj)
        try:
            response = self._get_capsule(
                session, self._capsule_url(base_url, obj.physical_id)
            )
            if response is None:
                return {}
            capsule = self._response_json(response)
            if capsule.get("Error"):
                reason = capsule["Error"].get("message") or "unknown error"
                raise RuntimeError(
                    f"Failed to get details for Capsule {obj.physical_id}: "
                    f"{reason}"
                )
            return {
                "id": (
                    capsule.get("uuid")
                    or capsule.get("id")
                    or obj.physical_id
                ),
                "name": capsule.get("name") or obj.name,
                "status": capsule.get("status"),
                "addresses": capsule.get("addresses") or {},
            }
        except Exception as error:
            LOG.warning(
                "Could not get details for Zun capsule %s: %s",
                obj.physical_id,
                error,
            )
            return {"Error": {"message": str(error)}}
