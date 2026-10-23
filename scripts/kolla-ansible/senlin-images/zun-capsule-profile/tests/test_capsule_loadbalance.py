from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from yggdrasil_senlin_zun_profile import capsule_loadbalance as lb


class CapsuleAddressSelectionTest(TestCase):
    def test_selects_address_by_subnet_and_ip_version(self):
        addresses = {
            "network-a": [
                {
                    "subnet_id": "subnet-v6",
                    "version": 6,
                    "addr": "2001:db8::1",
                },
                {
                    "subnet_id": "subnet-v4",
                    "version": 4,
                    "addr": "192.0.2.8",
                },
                {
                    "subnet_id": "other-v4",
                    "version": 4,
                    "addr": "198.51.100.8",
                },
            ]
        }

        result = lb.CapsuleLoadBalancerDriver._address_for_subnet(
            addresses, "subnet-v4", 4
        )

        self.assertEqual("192.0.2.8", result)

    def test_returns_none_when_exact_subnet_is_missing(self):
        result = lb.CapsuleLoadBalancerDriver._address_for_subnet(
            {"network-a": [{"subnet_id": "other",
                            "version": 4, "addr": "192.0.2.8"}]},
            "subnet-v4",
            4,
        )

        self.assertIsNone(result)


class CapsuleMemberDriverTest(TestCase):
    def setUp(self):
        self.driver = lb.CapsuleLoadBalancerDriver({})
        self.driver._nc = Mock()
        self.driver._nc.subnet_get.return_value = SimpleNamespace(
            id="subnet-v4", ip_version=4
        )
        self.driver._oc = Mock()
        self.driver._oc.pool_member_create.return_value = SimpleNamespace(
            id="member-id"
        )
        self.driver._wait_for_lb_ready = Mock(return_value=True)
        self.node = SimpleNamespace(
            id="node-id", profile_id="profile-id", name="senlin-node"
        )

    @patch.object(lb.profile_base.Profile, "load")
    def test_member_add_uses_capsule_address_on_configured_subnet(
        self, load_profile
    ):
        profile = Mock()
        profile.do_get_details.return_value = {
            "name": "capsule-node",
            "addresses": {
                "network-id": [
                    {
                        "subnet_id": "other-subnet",
                        "version": 4,
                        "addr": "198.51.100.4",
                    },
                    {
                        "subnet_id": "subnet-v4",
                        "version": 4,
                        "addr": "192.0.2.4",
                    },
                ]
            },
        }
        load_profile.return_value = profile

        result = self.driver.member_add(
            self.node, "lb-id", "pool-id", 8080, "subnet-v4"
        )

        self.assertEqual("member-id", result)
        self.driver._oc.pool_member_create.assert_called_once_with(
            "capsule-node", "pool-id", "192.0.2.4", 8080, "subnet-v4"
        )
        self.assertEqual({}, self.driver.member_errors)

    @patch.object(lb.profile_base.Profile, "load")
    def test_missing_pool_subnet_address_fails_with_reason(self, load_profile):
        profile = Mock()
        profile.do_get_details.return_value = {
            "addresses": {
                "network-id": [
                    {
                        "subnet_id": "other-subnet",
                        "version": 4,
                        "addr": "192.0.2.4",
                    }
                ]
            }
        }
        load_profile.return_value = profile

        result = self.driver.member_add(
            self.node, "lb-id", "pool-id", 8080, "subnet-v4"
        )

        self.assertIsNone(result)
        self.assertIn(
            "no address on pool subnet subnet-v4",
            self.driver.member_errors["node-id"],
        )
        self.driver._oc.pool_member_create.assert_not_called()

    @patch.object(lb.profile_base.Profile, "load")
    def test_capsule_detail_lookup_failure_is_reported(self, load_profile):
        profile = Mock()
        profile.do_get_details.return_value = {
            "Error": {"message": "Zun API unavailable"}
        }
        load_profile.return_value = profile

        result = self.driver.member_add(
            self.node, "lb-id", "pool-id", 8080, "subnet-v4"
        )

        self.assertIsNone(result)
        self.assertIn(
            "Zun API unavailable", self.driver.member_errors["node-id"]
        )
        self.driver._oc.pool_member_create.assert_not_called()

    @patch.object(lb.profile_base.Profile, "load")
    def test_octavia_failure_fails_with_reason(self, load_profile):
        profile = Mock()
        profile.do_get_details.return_value = {
            "addresses": {
                "network-id": [
                    {
                        "subnet_id": "subnet-v4",
                        "version": 4,
                        "addr": "192.0.2.4",
                    }
                ]
            }
        }
        load_profile.return_value = profile
        self.driver._oc.pool_member_create.side_effect = RuntimeError(
            "Octavia unavailable")

        result = self.driver.member_add(
            self.node, "lb-id", "pool-id", 8080, "subnet-v4"
        )

        self.assertIsNone(result)
        self.assertIn("Octavia unavailable",
                      self.driver.member_errors["node-id"])


class CapsulePolicyRegistrationTest(TestCase):
    def test_capsule_policy_inherits_senlin_lifecycle_and_schema(self):
        self.assertEqual(["os.zun.capsule-1.0"],
                         lb.CapsuleLoadBalancingPolicy.PROFILE_TYPE)
        self.assertIn(("AFTER", "CLUSTER_SCALE_OUT"),
                      lb.CapsuleLoadBalancingPolicy.TARGET)
        self.assertIn(("BEFORE", "CLUSTER_SCALE_IN"),
                      lb.CapsuleLoadBalancingPolicy.TARGET)
        self.assertIn("pool", lb.CapsuleLoadBalancingPolicy.properties_schema)
        self.assertIn("vip", lb.CapsuleLoadBalancingPolicy.properties_schema)
        self.assertIn("health_monitor",
                      lb.CapsuleLoadBalancingPolicy.properties_schema)


class CapsulePolicyLifecycleTest(TestCase):
    def setUp(self):
        self.policy = lb.CapsuleLoadBalancingPolicy(
            "test-policy",
            {
                "type": "yggdrasil.policy.capsule_loadbalance",
                "version": "1.0",
                "properties": {},
            },
        )
        self.driver = Mock()
        self.driver.member_errors = {}
        self.cluster = SimpleNamespace(user="user-id", project="project-id")

    @patch.object(lb.lb_policy.LoadBalancingPolicy, "attach")
    @patch.object(lb.CapsuleLoadBalancingPolicy, "lbaas")
    def test_attach_preserves_clear_member_failure_reason(
        self, get_driver, parent_attach
    ):
        get_driver.return_value = self.driver

        def attach_with_failure(cluster, enabled=True):
            self.driver.member_errors["node-id"] = "No address on pool subnet"
            return False, "Failed in adding node"

        parent_attach.side_effect = attach_with_failure

        result = self.policy.attach(self.cluster)

        self.assertEqual(
            (False, "No address on pool subnet"),
            result,
        )
        parent_attach.assert_called_once_with(self.cluster, True)

    @patch.object(lb.lb_policy.LoadBalancingPolicy, "detach")
    @patch.object(lb.CapsuleLoadBalancingPolicy, "lbaas")
    def test_detach_surfaces_octavia_member_removal_failure(
        self, get_driver, parent_detach
    ):
        get_driver.return_value = self.driver

        def detach_with_failure(cluster):
            self.driver.member_errors["member-id"] = (
                "Octavia member removal failed"
            )
            return False, "Failed to remove servers"

        parent_detach.side_effect = detach_with_failure

        result = self.policy.detach(self.cluster)

        self.assertEqual((False, "Octavia member removal failed"), result)
        parent_detach.assert_called_once_with(self.cluster)

    @patch.object(lb.lb_policy.LoadBalancingPolicy, "pre_op")
    @patch.object(lb.CapsuleLoadBalancingPolicy, "lbaas")
    def test_scale_in_runs_the_pre_delete_hook_and_propagates_failure(
        self, get_driver, parent_pre_op
    ):
        action = SimpleNamespace(
            entity=self.cluster,
            data={
                "status": lb.policy_base.CHECK_ERROR,
                "reason": "generic removal failure",
            },
        )
        get_driver.return_value = self.driver

        def pre_op_with_failure(cluster_id, current_action):
            self.driver.member_errors["member-id"] = (
                "Octavia pool member removal failed"
            )

        parent_pre_op.side_effect = pre_op_with_failure

        self.policy.pre_op("cluster-id", action)

        parent_pre_op.assert_called_once_with("cluster-id", action)
        self.assertEqual(
            "Octavia pool member removal failed", action.data["reason"]
        )
        self.assertIn(
            ("BEFORE", "CLUSTER_SCALE_IN"),
            lb.CapsuleLoadBalancingPolicy.TARGET,
        )

    @patch.object(lb.lb_policy.LoadBalancingPolicy, "post_op")
    @patch.object(lb.CapsuleLoadBalancingPolicy, "lbaas")
    def test_scale_out_runs_post_create_hook_and_propagates_failure(
        self, get_driver, parent_post_op
    ):
        action = SimpleNamespace(
            entity=self.cluster,
            data={
                "status": lb.policy_base.CHECK_ERROR,
                "reason": "generic add failure",
            },
        )
        get_driver.return_value = self.driver

        def post_op_with_failure(cluster_id, current_action):
            self.driver.member_errors["node-id"] = (
                "Capsule has no matching subnet address"
            )

        parent_post_op.side_effect = post_op_with_failure

        self.policy.post_op("cluster-id", action)

        parent_post_op.assert_called_once_with("cluster-id", action)
        self.assertEqual(
            "Capsule has no matching subnet address", action.data["reason"]
        )
        self.assertIn(
            ("AFTER", "CLUSTER_SCALE_OUT"),
            lb.CapsuleLoadBalancingPolicy.TARGET,
        )

    def test_member_remove_reports_unexpected_octavia_exception(self):
        driver = lb.CapsuleLoadBalancerDriver({})
        driver._wait_for_lb_ready = Mock(
            side_effect=RuntimeError("Octavia down"))

        result = driver.member_remove("lb-id", "pool-id", "member-id")

        self.assertFalse(result)
        self.assertIn("Octavia down", driver.member_errors["member-id"])
