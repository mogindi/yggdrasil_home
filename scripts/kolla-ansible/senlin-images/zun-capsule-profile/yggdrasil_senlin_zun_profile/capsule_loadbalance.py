"""Capsule-aware variant of Senlin's built-in load-balancing policy."""

from oslo_context import context as oslo_context
from oslo_log import log as logging

from senlin.common import consts
from senlin.common import exception
from senlin.drivers.os import lbaas as lbaas_driver
from senlin.policies import base as policy_base
from senlin.policies import lb_policy
from senlin.profiles import base as profile_base


LOG = logging.getLogger(__name__)


class CapsuleLoadBalancerDriver(lbaas_driver.LoadBalancerDriver):
    """Use Capsule addresses from the configured Neutron subnet."""

    def __init__(self, params):
        super().__init__(params)
        self.member_errors = {}

    @staticmethod
    def _address_for_subnet(addresses, subnet_id, ip_version):
        if not isinstance(addresses, dict):
            return None
        for subnet_addresses in addresses.values():
            if not isinstance(subnet_addresses, list):
                continue
            for address in subnet_addresses:
                if not isinstance(address, dict):
                    continue
                if str(address.get("subnet_id")) != str(subnet_id):
                    continue
                if str(address.get("version")) != str(ip_version):
                    continue
                if address.get("addr"):
                    return address["addr"]
        return None

    def member_add(self, node, lb_id, pool_id, port, subnet):
        """Add a Capsule as an Octavia member using its pool-subnet address."""
        try:
            subnet_obj = self.nc().subnet_get(subnet)
            profile = profile_base.Profile.load(
                oslo_context.get_current(),
                profile_id=node.profile_id,
                project_safe=False,
            )
            details = profile.do_get_details(node)
            if details.get("Error"):
                details_error = details["Error"].get(
                    "message") or "unknown error"
                reason = (
                    f"Failed to read Capsule {node.id} network addresses: "
                    f"{details_error}"
                )
                self.member_errors[node.id] = reason
                LOG.error(reason)
                return None
            addresses = details.get("addresses") or {}
            address = self._address_for_subnet(
                addresses, subnet_obj.id, subnet_obj.ip_version
            )
            if address is None:
                reason = (
                    f"Capsule {node.id} has no address on pool subnet "
                    f"{subnet_obj.id} (IPv{int(subnet_obj.ip_version)})."
                )
                self.member_errors[node.id] = reason
                LOG.error(reason)
                return None

            if not self._wait_for_lb_ready(lb_id):
                reason = (
                    f"Load balancer {lb_id} is not ready for "
                    f"Capsule {node.id}."
                )
                self.member_errors[node.id] = reason
                LOG.error(reason)
                return None

            member = self.oc().pool_member_create(
                details.get("name") or node.name,
                pool_id,
                address,
                port,
                subnet_obj.id,
            )
            if not self._wait_for_lb_ready(lb_id):
                reason = (
                    f"Load balancer {lb_id} did not become ready after adding "
                    f"Capsule {node.id}."
                )
                self.member_errors[node.id] = reason
                LOG.error(reason)
                return None
            return member.id
        except (exception.InternalError, exception.Error) as error:
            reason = (
                f"Failed to add Capsule {node.id} to Octavia pool "
                f"{pool_id}: {error}"
            )
            self.member_errors[node.id] = reason
            LOG.exception(reason)
            return None
        except Exception as error:
            reason = (
                f"Failed to add Capsule {node.id} to Octavia pool "
                f"{pool_id}: {error}"
            )
            self.member_errors[node.id] = reason
            LOG.exception(reason)
            return None

    def member_remove(self, lb_id, pool_id, member_id):
        try:
            result = super().member_remove(lb_id, pool_id, member_id)
        except Exception as error:
            reason = (
                f"Failed to remove Octavia member {member_id} from pool "
                f"{pool_id} on load balancer {lb_id}: {error}"
            )
            self.member_errors[member_id] = reason
            LOG.exception(reason)
            return False
        if result is not True:
            reason = (
                f"Failed to remove Octavia member {member_id} from pool "
                f"{pool_id} on load balancer {lb_id}."
            )
            self.member_errors[member_id] = reason
            LOG.error(reason)
        return result


class CapsuleLoadBalancingPolicy(lb_policy.LoadBalancingPolicy):
    """Senlin's load-balancing lifecycle restricted to Capsule clusters."""

    VERSION = "1.0"
    VERSIONS = {
        "1.0": [{"status": consts.SUPPORTED, "since": "2025.2"}],
    }
    PROFILE_TYPE = ["os.zun.capsule-1.0"]

    def lbaas(self, user, project):
        if self._lbaasclient is None:
            params = self._build_conn_params(user, project)
            self._lbaasclient = CapsuleLoadBalancerDriver(params)
        return self._lbaasclient

    @staticmethod
    def _set_failure_reason(action, driver, fallback):
        if action.data.get("status") != policy_base.CHECK_ERROR:
            return
        errors = list(dict.fromkeys(driver.member_errors.values()))
        if errors:
            action.data["reason"] = "; ".join(errors)
        elif fallback:
            action.data["reason"] = fallback

    def attach(self, cluster, enabled=True):
        driver = self.lbaas(cluster.user, cluster.project)
        driver.member_errors.clear()
        result = super().attach(cluster, enabled)
        if result[0] is False and driver.member_errors:
            errors = dict.fromkeys(driver.member_errors.values())
            return False, "; ".join(errors)
        return result

    def detach(self, cluster):
        driver = self.lbaas(cluster.user, cluster.project)
        driver.member_errors.clear()
        result = super().detach(cluster)
        if result[0] is False and driver.member_errors:
            errors = dict.fromkeys(driver.member_errors.values())
            return False, "; ".join(errors)
        return result

    def pre_op(self, cluster_id, action):
        obj = action.entity
        driver = self.lbaas(obj.user, obj.project)
        driver.member_errors.clear()
        super().pre_op(cluster_id, action)
        self._set_failure_reason(action, driver, action.data.get("reason"))

    def post_op(self, cluster_id, action):
        obj = action.entity
        driver = self.lbaas(obj.user, obj.project)
        driver.member_errors.clear()
        super().post_op(cluster_id, action)
        self._set_failure_reason(action, driver, action.data.get("reason"))
