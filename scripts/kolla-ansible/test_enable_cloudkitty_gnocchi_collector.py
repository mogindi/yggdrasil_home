import importlib.util
import pathlib
import unittest
from unittest.mock import Mock


SCRIPT = pathlib.Path(__file__).with_name("enable-cloudkitty-gnocchi-collector.py")
SPEC = importlib.util.spec_from_file_location("enable_cloudkitty_gnocchi_collector", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CollectorReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.connection.endpoint_for.return_value = "http://rating.example/v1/"

    def _response(self, enabled):
        response = Mock()
        response.json.return_value = {"name": "gnocchi", "enabled": enabled}
        return response

    def test_enables_disabled_collector_and_verifies_state(self):
        self.connection.session.get.side_effect = [self._response(False), self._response(True)]

        changed = MODULE.reconcile(self.connection, sleep=lambda _seconds: None)

        self.assertTrue(changed)
        self.connection.session.put.assert_called_once_with(
            "http://rating.example/v1/collector/states/",
            json={"name": "gnocchi", "enabled": True},
            timeout=30,
        )

    def test_leaves_already_enabled_collector_unchanged(self):
        self.connection.session.get.return_value = self._response(True)

        changed = MODULE.reconcile(self.connection, sleep=lambda _seconds: None)

        self.assertFalse(changed)
        self.connection.session.put.assert_not_called()

    def test_retries_until_rating_api_is_ready(self):
        unavailable = Mock()
        unavailable.raise_for_status.side_effect = RuntimeError("not ready")
        self.connection.session.get.side_effect = [unavailable, self._response(True)]

        changed = MODULE.reconcile(self.connection, sleep=lambda _seconds: None)

        self.assertFalse(changed)
        self.assertEqual(self.connection.session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
