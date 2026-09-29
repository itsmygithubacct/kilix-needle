"""System status describes its proposal profile without implying execution gates."""
import unittest
from unittest import mock

import support  # noqa: F401
import system_model
import tuning


class Status(unittest.TestCase):
    def test_status_uses_system_profile_and_does_not_consult_other_selections(self):
        runtime = mock.MagicMock()
        runtime.__enter__.return_value.label = "tuned system normalizer testhash"
        with mock.patch.object(system_model, "open_runtime", return_value=runtime), \
             mock.patch.object(tuning, "selected", side_effect=AssertionError("wrong selection")):
            result = tuning.in_use("system")
        self.assertIn("grammar first", result)
        self.assertIn("tuned system normalizer testhash", result)
        self.assertIn("proposals only", result)

    def test_unavailable_profile_still_reports_grammar_instead_of_base(self):
        with mock.patch.object(system_model, "open_runtime", side_effect=system_model.SystemModelError("missing profile")):
            result = tuning.in_use("system")
        self.assertIn("grammar first", result)
        self.assertIn("tuned normalizer unavailable", result)
        self.assertNotIn("base", result)
