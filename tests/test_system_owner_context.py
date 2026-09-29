"""Generic object references must not silently name an installed command."""
import unittest
from unittest import mock

import support  # noqa: F401
import needle_cli
import system_collect
import system_job
import system_normalize


class OwnerContext(unittest.TestCase):
    def test_generic_owner_reference_neither_infers_nor_collects(self):
        classifier = mock.Mock(side_effect=AssertionError("model called"))
        collector = mock.Mock(side_effect=AssertionError("collected"))
        for target in ("the file", "this file", "that command", "my path", "the executable"):
            request = f"Which package owns {target}?"
            with self.subTest(target=target):
                self.assertIsNone(system_job.parse(request))
                proposal = system_normalize.plan(request, classifier)
                self.assertEqual(proposal["decision"], "clarify")
                self.assertEqual(proposal["actions"], [])
                record = system_collect.run_request(system_job.Baseline(), request,
                                                   needle_cli.Options(), collector=collector)
                self.assertEqual(record["status"], 1)
        classifier.assert_not_called()
        collector.collect.assert_not_called()

    def test_explicit_file_utility_and_absolute_path_remain_supported(self):
        for request, target in (("which package owns file?", "file"),
                                ("which package owns the file command?", "file"),
                                ("which package owns /usr/bin/file?", "/usr/bin/file")):
            with self.subTest(request=request):
                self.assertEqual(system_job.parse(request), [system_job.action(
                    "packages", operation="owner", target=target)])

    def test_ambiguous_compound_has_no_partial_read(self):
        request = "show memory and which package owns the file?"
        self.assertIsNone(system_job.parse(request))
        classifier = mock.Mock(side_effect=AssertionError("model called"))
        self.assertEqual(system_normalize.plan(request, classifier)["actions"], [])
        classifier.assert_not_called()
