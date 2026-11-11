from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RgpuAuthorizationPolicyTests(unittest.TestCase):
    def test_nova_allows_the_rgpu_role_to_show_exact_server_ids_only(self):
        policy = (ROOT / "scripts/kolla-ansible/policies/nova/policy.yaml").read_text()

        self.assertIn(
            '"os_compute_api:servers:show": "rule:project_reader_or_admin or '
            '(role:rgpu_reader and system_scope:all)"',
            policy,
        )
        self.assertIn(
            '"os_compute_api:servers:show:flavor-extra-specs": '
            '"rule:project_reader_or_admin or (role:rgpu_reader and system_scope:all)"',
            policy,
        )
        self.assertNotIn('"os_compute_api:servers:index"', policy)
        self.assertNotIn('"os_compute_api:servers:detail"', policy)
        self.assertNotIn('"os_compute_api:servers:show:host_status"', policy)
        self.assertNotIn('"os_compute_api:servers:update"', policy)

    def test_heat_allows_only_stack_template_and_resource_reads(self):
        policy = (ROOT / "scripts/kolla-ansible/policies/heat/policy.yaml").read_text()

        self.assertIn('"stacks:show":', policy)
        self.assertIn(
            '"stacks:template": "role:rgpu_heat_reader and project_id:%(project_id)s"',
            policy,
        )
        self.assertIn(
            '"stacks:lookup": "role:rgpu_heat_reader and project_id:%(project_id)s"',
            policy,
        )
        self.assertIn(
            '"resource:index": "role:rgpu_heat_reader and project_id:%(project_id)s"',
            policy,
        )
        self.assertIn(
            '(role:rgpu_heat_reader and project_id:%(project_id)s)',
            policy,
        )
        self.assertNotIn('"stacks:list": "role:rgpu_heat_reader', policy)
        self.assertNotIn('"stacks:delete": "role:rgpu_heat_reader', policy)
        self.assertNotIn('"resources:update": "role:rgpu_heat_reader', policy)

    def test_only_a_dedicated_role_can_use_the_cross_scope_read_policy(self):
        config = (ROOT / "scripts/kolla-ansible/configure.sh").read_text()
        playbook = (ROOT / "ansible/kolla_ansible.yml").read_text()

        self.assertEqual(config.count("[oslo_policy]\nenforce_scope = false"), 1)
        self.assertIn("retain Heat's normal scope enforcement", config)
        self.assertNotIn("etc/kolla/config/heat.conf", config)
        self.assertIn("Stage Heat reader policy for configure script", playbook)
        self.assertIn("OPENSTACK_HEAT_POLICY_FILE: /tmp/yggdrasil-heat-reader-policy.yaml", playbook)


if __name__ == "__main__":
    unittest.main()
