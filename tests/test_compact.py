"""Experimental schema admission; no host tools or model are loaded."""
import unittest

import support  # noqa: F401
import compact


def call(name, **args):
    return {"name": name, "arguments": args}


class CompactAdmission(unittest.TestCase):
    def test_bare_launch_resolves_host_defaults(self):
        out = compact.admit("start claude", [call("start_claude")])
        self.assertEqual(out.job, "agents")
        self.assertEqual(dict(out.actions[0].args), {"agent": "claude", "dir": "here"})

    def test_explicit_directory_placement_and_task_are_retained(self):
        out = compact.admit('start codex in /work/app on the right: say "ready"',
                            [call("start_codex", directory="/work/app", place="right",
                                  task='say "ready"')])
        self.assertEqual(out.actions[0].args["prompt"], 'say "ready"')
        self.assertEqual(out.actions[0].args["dir"], "/work/app")
        self.assertEqual(out.actions[0].args["place"], "right")

    def test_wrong_operation_directory_or_task_never_admits(self):
        cases = [
            ("wait for claude in /work/app to finish", call("start_claude", directory="/work/app")),
            ("start claude in /work/app", call("start_claude", directory="/work/other")),
            ('start claude here: say "ready"', call("start_claude", task="say ready now")),
            ("do not start claude", call("start_claude")),
        ]
        for text, proposal in cases:
            with self.subTest(text=text):
                self.assertFalse(compact.admit(text, [proposal]).actions)

    def test_wait_and_message_target_the_existing_session(self):
        wait = compact.admit("wait for claude in /work/app to finish",
                             [call("wait_for_agent", agent="claude", directory="/work/app")])
        self.assertEqual(wait.actions[0].kind, "wait")
        self.assertEqual(wait.actions[0].args["session"], "claude@/work/app")
        tell = compact.admit("tell codex here: run the tests",
                             [call("message_agent", agent="codex", text="run the tests")])
        self.assertEqual(tell.actions[0].kind, "tell")

    def test_viewer_and_explicit_file_are_distinct(self):
        viewer = compact.admit("open the pdf viewer", [call("open_app", app="kilix-pdf")])
        self.assertEqual(viewer.actions[0].args["app"], "kilix-pdf")
        file = compact.admit("open /work/report.pdf in the PDF viewer",
                             [call("open_app", app="kilix-pdf", file_path="/work/report.pdf")])
        self.assertEqual(file.actions[0].args['file_path'], '/work/report.pdf')
        self.assertFalse(compact.admit("open the pdf viewer", [call(
            "open_app", app="kilix-pdf", file_path="/work/report.pdf")]).actions)

    def test_partial_requests_do_not_admit_a_subset(self):
        for text, proposals in [
            ("open pdf viewer then reboot", [call("open_app", app="kilix-pdf")]),
            ("start claude and start codex", [call("start_claude")]),
        ]:
            with self.subTest(text=text):
                self.assertFalse(compact.admit(text, proposals).actions)

    def test_complete_mixed_plan_preserves_order(self):
        calls = [call('open_app',app='kilix-notepad'),call('start_codex',directory='/work/project')]
        request = 'bring up notepad; next start codex in /work/project'
        result = compact.admit(request,calls)
        self.assertEqual(result.job,'mixed')
        self.assertEqual([x.kind for x in result.actions],['launch','agent'])
        self.assertFalse(compact.admit(request,list(reversed(calls))).actions)

    def test_negative_and_unknown_tails_have_no_complete_reading(self):
        for request in ['do not open notepad', 'has claude started in /work/project?',
                        'open calculator; next restart the computer',
                        'someone said open notepad', 'open notepad if the build passes',
                        'start claude in /work/project and delete the other project']:
            with self.subTest(request=request):
                self.assertIsNone(compact.exact_calls(request))

    def test_pdf_path_is_literal_and_not_interpreted_as_more_actions(self):
        request = 'load "/work/My Folder/start claude; then open notepad.pdf" with the PDF viewer'
        calls = compact.exact_calls(request)
        self.assertEqual(calls,[call('open_app',app='kilix-pdf',
            file_path='/work/My Folder/start claude; then open notepad.pdf')])
        self.assertEqual(len(compact.admit(request,calls).actions),1)

    def test_normalized_payload_does_not_replace_literal_text(self):
        self.assertFalse(compact.admit('tell codex here: say "hello  world"',
            [call('message_agent',agent='codex',text='say "hello world"')]).actions)

    def test_schema_failures_and_empty_calls_never_admit(self):
        for proposal in [None, [], [call("start_claude", directory=7)],
                         [call("start_claude", place="somewhere")],
                         [call("wait_for_agent")], [call("run_shell", command="pwd")],
                         [call("open_app", app="unknown")],
                         [{"name":"start_claude", "arguments":{}, "extra":True}]]:
            with self.subTest(proposal=proposal):
                self.assertFalse(compact.admit("start claude", proposal).actions)


if __name__ == "__main__":
    unittest.main()
