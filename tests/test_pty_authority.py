"""Public ingress cannot turn model-controlled text or consent into authority."""
import copy
import os
import unittest
from unittest import mock

import support
import pty_cli
import pty_backend
from test_pty_selector_receipts import success


class AuthorityIngress(unittest.TestCase):
    def test_cli_api_and_mcp_refuse_all_selected_ends_without_controller(self):
        action, _ = success()
        for request in (action, 'end selector {"id":"target-7","attached":false} operation fixture-key',
                        'end session "target-7"'):
            for yes in (False, True):
                with mock.patch.object(pty_backend, "call", side_effect=AssertionError("no backend dispatch")):
                    record = pty_cli.run(request, assume_yes=yes)
                    self.assertEqual((record["status"], record["refused"]), (3, "authority_required"))
                    record = pty_cli.mcp({"request": request, "confirm_risky": yes})
                    self.assertEqual((record["status"], record["refused"]), (3, "authority_required"))

    def test_no_model_argument_can_supply_controller_or_transport_bypass(self):
        action, _ = success()
        for field in ("controller", "authority", "nonce", "_supplied", "original_user"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    pty_cli.mcp({"request": action, field: True})
                record = pty_cli.run({**action, field: True}, assume_yes=True)
                self.assertEqual(record["status"], 2)

    def test_embedding_controller_receives_exact_request_and_checks_receipt(self):
        action, doc = success()
        environment = mock.patch.dict(os.environ, {"KITTY_PTY_BROKER_SESSION": "fixture-caller"})
        environment.start()
        self.addCleanup(environment.stop)
        controller = mock.Mock()
        controller.dispatch.return_value = (0, doc)
        with mock.patch.object(pty_backend, "call", side_effect=AssertionError("no CLI fallback")):
            result = pty_cli.mcp({"request": action}, controller=controller)
        # The controller's direct selection supplies consent, not a model boolean.
        self.assertEqual(result["status"], 0)
        controller.dispatch.assert_called_once_with({"schema": "kilix.pty.request/v2", "verb": "end",
                                                     "selector": action["selector"], "operation_id": action["operation_id"]})
        bad = copy.deepcopy(doc)
        bad["resolved"]["id"] = "different"
        controller.dispatch.return_value = (0, bad)
        self.assertEqual(pty_cli.mcp({"request": action}, controller=controller)["status"], 1)


if __name__ == "__main__":
    unittest.main()
