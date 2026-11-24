"""A decision model may suggest a wording; only the grammar says what it means."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import threading
import unittest
from unittest import mock

import ask
import commands_contract as contract
import commands_suggest as suggest
import router


def call(name, **args):
    return {"name": name, "arguments": args}


class Model:
    """Answers each question by the first rule whose words the question holds.

    A rule's answer is an option key, or a function of the options. `sure` is
    the probability the chosen option gets; a per-rule value overrides it."""

    def __init__(self, *rules, sure=0.95):
        self.rules, self.sure, self.asked = rules, sure, []

    def __call__(self, state, question, options):
        self.asked.append((question, dict(options), state))
        for words, answer, *odds in self.rules:
            if words in question:
                chosen = answer(options) if callable(answer) else answer
                break
        else:
            raise AssertionError(f"no rule answers {question!r}")
        if chosen not in options:
            raise AssertionError(f"{chosen!r} is not offered for {question!r}: {sorted(options)}")
        sure = odds[0] if odds else self.sure
        rest = (1 - sure) / (len(options) - 1)
        return sorted(((key, sure if key == chosen else rest) for key in options), key=lambda item: -item[1])

    def questions(self, words):
        return [options for question, options, _ in self.asked if words in question]


def first_span(options):
    return "1"


COMMAND = ("What kind of request", "command")
YES = ("Does the plan do exactly", "yes")


class Spans(unittest.TestCase):
    def test_copied_spans_are_found_by_the_requests_own_marks(self):
        cases = {
            'Tell Claude in /work/a.b "run it" now': (["/work/a.b"], ["run it"]),
            "say 'looks good, ship it' to codex in ~/code/onyx": (["~/code/onyx"], ["looks good, ship it"]),
            "send codex 'print \"ready\" once the cache is warm'": ([], ['print "ready" once the cache is warm']),
            "send “if you're blocked, say so” to Codex in mill.bak": (["mill.bak"], ["if you're blocked, say so"]),
            "Wait for Claude's turn in ./lemon-tundra": (["./lemon-tundra"], []),
            'Launch Codex in "plum harbor" with the task "open the log"': (["plum harbor"], ["open the log"]),
            'Bring up Codex, directory "Winter Atlas/2026 drafts"': (["Winter Atlas/2026 drafts"], []),
            "spin up Claude in the jalapeño-grid directory": (["jalapeño-grid"], []),
            "Start Codex in this folder in a split underneath.": ([], []),
            "Let Claude know: the API key rotation is done": ([], ["the API key rotation is done"]),
            "Three things: start codex, start claude": ([], []),
            'Show "tax form 2025.pdf" in the pdf viewer': (["tax form 2025.pdf"], []),
            'Message Claude in /a: "lint only", then message Codex in /a: "tests only"': (
                ["/a"], ["lint only", "tests only"]),
        }
        for request, (paths, texts) in cases.items():
            with self.subTest(request=request):
                found = suggest.spans(request)
                self.assertEqual((found[0], found[1]), (paths, texts))

    def test_a_quoted_text_is_hidden_from_the_questions_about_the_request(self):
        paths, texts, models, masked, plain = suggest.spans(
            'Tell Codex in "old mill" "start claude with model opus-9 if idle" using model gpt-6')
        self.assertEqual(masked, 'Tell Codex in "old mill" [TEXT 1] using model gpt-6')
        self.assertEqual((paths, texts, models), (["old mill"], ["start claude with model opus-9 if idle"], ["gpt-6"]))
        self.assertNotIn("claude", plain.lower())
        self.assertIsNone(suggest.NOT_NOW.search(plain))


class Walk(unittest.TestCase):
    def test_a_plan_is_built_only_from_what_the_request_names(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("Where should", "up"),
                      ("Which directory", first_span), YES)
        calls, steps = suggest.walk("Put Codex in a pane above, in /work/tern.v2.", model)
        self.assertEqual(calls, [call("start_codex", directory="/work/tern.v2", place="up")])
        self.assertEqual([step["kind"] for step in steps], ["gate", "operation", "directory", "place", "verify"])
        # Claude, the applications and the other directions are not there to choose.
        self.assertEqual(set(model.questions("Which operation")[0]), {"start_codex", "wait_for_agent", "other"})
        self.assertEqual(set(model.questions("Where should")[0]), {"up", "tab"})
        self.assertEqual(model.questions("Which directory")[0]["1"], "/work/tern.v2")

    def test_no_question_is_asked_when_nothing_is_named_or_the_request_is_not_for_now(self):
        for request, why in (("Start a coding session in /srv/x", "unnamed"),
                             ("Start Codex in /srv/x unless it is busy", "conditional"),
                             ("Start claude in /srv/x provided nobody else is there", "conditional"),
                             ("Open the notepad or the calculator", "conditional")):
            with self.subTest(request=request):
                model = Model()
                calls, steps = suggest.walk(request, model)
                self.assertEqual((calls, model.asked), ([], []))
                self.assertEqual((steps[-1]["kind"], steps[-1]["pick"]), ("refused", why))

    def test_a_plan_that_leaves_out_a_named_thing_is_refused(self):
        request = "Run Codex in dusk-ferry in a split below"
        here = Model(COMMAND, ("Which operation", "start_codex"), ("Which directory", "0"), ("Where should", "down"), YES)
        calls, steps = suggest.walk(request, here)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "unused"))
        tab = Model(COMMAND, ("Which operation", "start_codex"), ("Which directory", "1"), ("Where should", "tab"), YES)
        calls, steps = suggest.walk(request, tab)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "unused"))
        self.assertEqual(here.questions("Does the plan") + tab.questions("Does the plan"), [])
        whole = Model(COMMAND, ("Which operation", "start_codex"), ("Which directory", "1"), ("Where should", "down"), YES)
        self.assertEqual(suggest.walk(request, whole)[0], [call("start_codex", directory="dusk-ferry", place="down")])

    def test_an_unsupported_part_refuses_the_whole_request(self):
        model = Model(COMMAND, ("How many separate", "2"), ("Which operation is the first", "open_app"),
                      ("Which operation is the second", "other"), ("Which operation does", "other"),
                      ("Which application", "kilix-notepad"), YES)
        calls, steps = suggest.walk("Open the notepad and email its contents to Priya.", model)
        self.assertEqual(calls, [])
        self.assertEqual(model.questions("Does the plan"), [])

    def test_two_operations_keep_the_requests_order_and_whole_plans_compete(self):
        model = Model(COMMAND, ("How many separate", "2"), ("Which operation is the first", "open_app"),
                      ("Which operation is the second", "start_codex"), ("Which operation does", "start_codex"),
                      ("Which application", "kilix-calculator"), ("Which plan does exactly", "1"), YES)
        calls, _ = suggest.walk("First start Codex here, then open the calculator.", model)
        self.assertEqual(calls, [call("start_codex"), call("open_app", app="kilix-calculator")])
        offered = model.questions("Which plan does exactly")[0]
        self.assertEqual(set(offered), {"0", "1", "2"})

    def test_the_same_operation_twice_is_not_a_plan(self):
        model = Model(COMMAND, ("How many separate", "2", 0.99), ("Which operation", "start_claude"),
                      ("Which model", "0"), YES)
        calls, steps = suggest.walk("Start Claude here and set the model to opus-9.", model)
        # The two-operation reading repeats itself and is dropped; the one-operation plan stands alone.
        self.assertEqual(calls, [call("start_claude")])
        self.assertEqual(model.questions("Which plan does exactly"), [])


class Suggest(unittest.TestCase):
    MODEL = (COMMAND, ("Which operation", "start_codex"), ("Where should", "right"), YES)

    def test_the_suggestion_is_the_grammars_reading_of_the_plans_wording(self):
        request = "Open Codex in a split on the right."
        self.assertFalse(contract.exact_calls(request))
        found = suggest.suggest(request, Model(*self.MODEL))
        self.assertEqual(found, {"request": "start codex in a split to the right",
                                 "calls": [call("start_codex", place="right")]})
        self.assertEqual(contract.exact_calls(found["request"]), found["calls"])

    def test_a_request_the_grammar_reads_is_never_sent_to_the_model(self):
        model = Model()
        self.assertIsNone(suggest.suggest("start codex in a split to the right", model))
        self.assertIsNone(suggest.suggest("", model))
        self.assertIsNone(suggest.suggest("start codex " + "x" * 2048, model))
        self.assertEqual(model.asked, [])

    def test_an_unsure_answer_anywhere_means_no_suggestion(self):
        request = "Open Codex in a split on the right."
        for words in ("What kind of request", "Where should", "Does the plan do exactly"):
            with self.subTest(unsure=words):
                rules = [(rule[0], rule[1], 0.45) if rule[0] == words else rule for rule in self.MODEL]
                self.assertIsNone(suggest.suggest(request, Model(*rules)))
        self.assertIsNone(suggest.suggest(request, Model(*self.MODEL[:-1], ("Does the plan do exactly", "no"))))

    def test_a_plan_the_grammar_does_not_read_back_is_not_suggested(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("Which model", "1"), YES)
        request = "Start Codex using the model gpt-9-zeta."
        self.assertEqual(suggest.walk(request, model)[0], [call("start_codex", model="gpt-9-zeta")])
        self.assertIsNone(suggest.suggest(request, model))
        pdf = Model(COMMAND, ("Which operation", "open_app"), ("Which application", "kilix-pdf"),
                    ("Which file", "1"), YES)
        self.assertIsNone(suggest.suggest('Show "tax form 2025.pdf" in the pdf viewer', pdf))
        # A wording the grammar reads as some other plan is no suggestion either.
        right = Model(COMMAND, ("Which operation", "start_codex"), ("Where should", "right"), YES)
        with mock.patch.object(suggest, "sentence", return_value="start codex in a split to the left"):
            self.assertIsNone(suggest.suggest("Open Codex in a split on the right.", right))

    def test_the_wording_carries_every_kind_of_call_through_the_grammar(self):
        plans = [
            [call("start_claude", directory="/srv/a b", place="left", model="opus", task='say "hi" \\ twice')],
            [call("wait_for_agent", agent="codex", directory="dusk-ferry")],
            [call("message_agent", agent="claude", text="start codex; then wait: now")],
            [call("open_app", app="kilix-pdf", file_path="/srv/a b/x.pdf")],
            [call("open_app", app="kilix-file"), call("start_codex", place="down"),
             call("message_agent", agent="codex", directory="~/x", text="go")],
        ]
        for calls in plans:
            with self.subTest(calls=calls):
                read = contract.exact_calls(suggest.sentence(calls))
                self.assertEqual(contract._canonical(read), contract._canonical(calls))


class Server:
    """A stand-in for Ollama's decision endpoint on a loopback port."""

    def __init__(self, reply):
        self.bodies = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.bodies.append((self.path, body))
                data = json.dumps(reply(body)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Decider(unittest.TestCase):
    def test_only_a_server_on_this_computer_is_asked(self):
        for host in ("example.com:11434", "http://10.0.0.5:11434", "https://127.0.0.1:11434", "0.0.0.0:11434"):
            with self.subTest(host=host), self.assertRaises(suggest.SuggestError):
                suggest.Decider(host=host)
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "example.com"}), self.assertRaises(suggest.SuggestError):
            suggest.Decider()

    def test_a_choice_is_one_question_and_its_tokens_are_counted(self):
        server = Server(lambda body: {"answers": {"q": {"probabilities": {"a": 0.2, "b": 0.8}}},
                                      "usage": {"input_tokens": 31}})
        self.addCleanup(server.close)
        decider = suggest.Decider("d3:test", host=server.host)
        self.assertEqual(decider.choose("the state", "which?", {"a": "first", "b": "second"}), [("b", 0.8), ("a", 0.2)])
        path, body = server.bodies[0]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual(body, {"model": "d3:test", "state": "the state", "questions": {
            "q": {"type": "choice", "instructions": "which?", "criteria": {"a": "first", "b": "second"}}}})
        self.assertEqual((decider.questions, decider.tokens), (1, 31))

    def test_an_answer_that_does_not_cover_the_options_is_an_error(self):
        replies = ({"answers": {"q": {"probabilities": {"a": 1.0}}}}, {"answers": {}}, {"error": "no such model"},
                   {"answers": {"q": {"probabilities": {"a": "high", "b": 0.1}}}})
        for reply in replies:
            server = Server(lambda body, reply=reply: reply)
            self.addCleanup(server.close)
            with self.subTest(reply=reply), self.assertRaises(suggest.SuggestError):
                suggest.Decider(host=server.host).choose("s", "q?", {"a": "", "b": ""})
        with self.assertRaises(suggest.SuggestError):       # nothing listens there
            suggest.Decider(host="127.0.0.1:1", timeout=2).choose("s", "q?", {"a": "", "b": ""})


class Jobs:
    """The jobs double of test_ask: every job answers with one record."""

    def __init__(self, record, suggest=None):
        self.record, self.suggest, self.runs = record, suggest, 0

    def run(self, job, request, *, dry_run, assume_yes=False):
        self.runs += 1
        return dict(self.record, request=request)


def route(stage, job=None, candidates=()):
    return router.Route(stage, job, 0.9, 0.5, [("none", 0.01)], [], list(candidates))


FOUND = {"request": "start codex in a split to the right", "calls": [call("start_codex", place="right")]}
NOTHING = {"status": 1, "items": [], "note": "the whole request has no supported reading"}
PLANNED = {"status": 0, "note": "", "items": [{"kind": "k", "outcome": "would"}]}
REFUSED = {"status": 1, "note": "", "items": [{"kind": "k", "outcome": "refused"}]}


class Offer(unittest.TestCase):
    REQUEST = "Open Codex in a split on the right."

    def ask(self, jobs, routed, **options):
        with mock.patch.object(router, "route", return_value=routed), mock.patch.object(ask, "_record_unsure"):
            return ask.ask(options.pop("request", self.REQUEST), jobs, act=options.pop("act", False), **options)

    def test_a_request_that_planned_nothing_gets_the_wording_and_nothing_runs(self):
        seen = []
        jobs = Jobs(NOTHING, lambda request: seen.append(request) or FOUND)
        for routed in (route(0, "agents"), route(0, "none"), route("multi", None, ["panes", "agents"])):
            with self.subTest(stage=routed.stage, job=routed.job):
                for act in (False, True):
                    record = self.ask(jobs, routed, act=act)
                    self.assertEqual(record["status"], 1)
                    self.assertEqual(record["suggestion"], FOUND)
                    self.assertTrue(record["hint"].startswith("did you mean: start codex in a split to the right"))
        self.assertEqual(set(seen), {self.REQUEST})
        self.assertIn("  hint: did you mean: start codex in a split to the right", ask.render(record))

    def test_a_request_that_planned_or_was_refused_by_its_job_gets_none(self):
        for record in (PLANNED, REFUSED, {"status": 1, "plan": [{"kind": "find_files"}]}):
            suggester = mock.Mock(return_value=FOUND)
            answer = self.ask(Jobs(record, suggester), route(1, "agents"))
            self.assertNotIn("suggestion", answer)
            suggester.assert_not_called()

    def test_a_chosen_job_or_a_job_label_gets_none(self):
        suggester = mock.Mock(return_value=FOUND)
        jobs = Jobs(NOTHING, suggester)
        self.assertNotIn("suggestion", self.ask(jobs, route(0, "agents"), job="agents"))
        self.assertNotIn("suggestion", self.ask(jobs, route(0, "agents"), request="agents: " + self.REQUEST))
        with mock.patch.object(router, "route", side_effect=AssertionError("routed again")):
            self.assertNotIn("suggestion", ask.ask(self.REQUEST, jobs, act=False, route=route(1, "agents")))
        suggester.assert_not_called()

    def test_no_suggester_or_a_failing_one_leaves_the_answer_as_it_was(self):
        plain = self.ask(Jobs(NOTHING), route(0, "none"))
        for suggester in (mock.Mock(return_value=None), mock.Mock(side_effect=suggest.SuggestError("down")),
                          mock.Mock(side_effect=RuntimeError("anything"))):
            self.assertEqual(self.ask(Jobs(NOTHING, suggester), route(0, "none")), plain)
            suggester.assert_called_once_with(self.REQUEST)

    def test_the_suggester_is_off_unless_the_environment_names_a_model(self):
        with mock.patch.dict(os.environ, {"KILIX_ASK_SUGGEST": ""}):
            self.assertIsNone(ask.suggester())
        with mock.patch.dict(os.environ):
            os.environ.pop("KILIX_ASK_SUGGEST", None)
            self.assertIsNone(ask.suggester())
        answers = {"What kind": "command", "Which operation": "start_codex", "Where should": "right", "Does the": "yes"}

        def reply(body):
            question = body["questions"]["q"]
            chosen = next(pick for words, pick in answers.items() if words in question["instructions"])
            return {"answers": {"q": {"probabilities": {key: 0.9 if key == chosen else 0.1 / (len(question["criteria"]) - 1)
                                                        for key in question["criteria"]}}}}
        server = Server(reply)
        self.addCleanup(server.close)
        with mock.patch.dict(os.environ, {"KILIX_ASK_SUGGEST": " d3:test ", "OLLAMA_HOST": server.host}):
            found = ask.suggester()(self.REQUEST)
        self.assertEqual(found, {**FOUND, "model": "d3:test"})
        self.assertEqual({body["model"] for _, body in server.bodies}, {"d3:test"})
        self.assertEqual(len(server.bodies), 4)

    def test_the_command_line_offers_suggestions_to_a_person_only(self):
        import argparse
        with mock.patch.dict(os.environ, {"KILIX_ASK_SUGGEST": "d3:test"}):
            for agent in (False, True):
                jobs, stack = ask.cli_jobs(argparse.Namespace(agent=agent), interactive=False)
                with stack:
                    self.assertEqual(jobs.suggest is None, agent)
        jobs, stack = ask.cli_jobs(argparse.Namespace(agent=False), interactive=False)
        with stack:
            self.assertIsNone(jobs.suggest)


if __name__ == "__main__":
    unittest.main()
