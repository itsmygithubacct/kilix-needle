"""A decision model may suggest a wording; only the grammar says what it means."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import ask
import commands_contract as contract
import commands_selection
import commands_suggest as suggest
import router
import workflows


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


def toward(wording):
    """A rule's answer: the option on the way to `wording`, or that whole sentence when it is offered."""
    def pick(options):
        def score(text):
            text = text.replace("   (complete)", "").removesuffix(" ...")
            return 2 if text == wording else 1 if wording.startswith(text) else 0
        return max(options, key=lambda key: score(options[key]))
    return pick


SPLIT = "Bring up Codex in a split pane."      # six plans: a tab, four splits, or a wait


def builds(wording, *extra):
    return Model(COMMAND, ("Which continuation", toward(wording)), ("Which command does exactly", toward(wording)),
                 *extra, YES)


class Suggest(unittest.TestCase):
    MODEL = (COMMAND, YES)

    def test_the_suggestion_is_the_grammars_reading_of_the_plans_wording(self):
        request = "Codex in a split on the right, please."
        self.assertFalse(contract.exact_calls(request))
        model = Model(*self.MODEL)
        found = suggest.suggest(request, model)
        self.assertEqual(found, {"request": "start codex in a split to the right",
                                 "calls": [call("start_codex", place="right")], "source": "built"})
        self.assertEqual(contract.exact_calls(found["request"]), found["calls"])
        # One wording only: nothing to choose, so only the kind of request and the final check are asked.
        self.assertEqual([question for question, _, _ in model.asked], [suggest.GATE_QUESTION, suggest.VERIFY_QUESTION])

    def test_a_request_the_grammar_reads_is_never_sent_to_the_model(self):
        model = Model()
        self.assertIsNone(suggest.suggest("start codex in a split to the right", model))
        self.assertIsNone(suggest.suggest("", model))
        self.assertIsNone(suggest.suggest("start codex " + "x" * 2048, model))
        self.assertEqual(model.asked, [])

    def test_an_unsure_answer_anywhere_means_no_suggestion(self):
        wording = "start codex in a split below"
        self.assertEqual(suggest.suggest(SPLIT, builds(wording))["request"], wording)
        for words in ("What kind of request", "Which continuation", "Which command does exactly",
                      "Does the plan do exactly"):
            with self.subTest(unsure=words):
                rules = [(rule[0], rule[1], 0.45) if rule[0] == words else rule for rule in builds(wording).rules]
                self.assertIsNone(suggest.suggest(SPLIT, Model(*rules)))
        self.assertIsNone(suggest.suggest(SPLIT, Model(*builds(wording).rules[:-1], ("Does the plan do exactly", "no"))))

    def test_a_plan_the_grammar_does_not_read_back_is_not_suggested(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("Which model", "1"), YES)
        request = "Start Codex using the model gpt-9-zeta."
        self.assertEqual(suggest.walk(request, model)[0], [call("start_codex", model="gpt-9-zeta")])
        self.assertIsNone(suggest.suggest(request, model))
        self.assertIsNone(suggest.suggest('Show "tax form 2025.pdf" in the pdf viewer', Model(*self.MODEL)))
        # A wording the grammar reads as some other plan is no suggestion either.
        with mock.patch.object(suggest, "sentence", return_value="start codex in a split to the left"):
            self.assertIsNone(suggest.suggest("Codex in a split on the right, please.", Model(*self.MODEL)))

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


SAMPLE = (
    SPLIT, "Codex in a split on the right, please.", "Codex in a new tab, please",
    'Run codex in dusk-ferry and tell it "lint only"', "Fire up Codex in mill-pond, then Claude in the same directory",
    "The calculator and the notepad, please", "Claude and codex in ./tide-pool, please",
    'Claude gets "hi", and hang on for codex in /x/y', 'Claude in "plum harbor", model opus, task "open the log"',
    '~/papers/fern\u00e9.pdf in the pdf viewer please, then wait till claude is done', "Let Codex know: the tests pass now",
    'give claude in ./a.b the task "start; then wait" and open the file manager',
    "Wait for Claude in ./alpha and for Codex in ./beta, then open the notepad",
)


class Build(unittest.TestCase):
    """plans() and build(): the decision model builds the grammar's own sentence."""

    def test_every_sentence_the_generator_can_make_is_read_back_as_its_plan(self):
        sizes = []
        for request in SAMPLE:
            with self.subTest(request=request):
                found = suggest.plans(request)
                sizes.append(len(found))
                paths, texts, models, _masked, _plain = suggest.spans(request)
                root = suggest.tree(found)
                complete = suggest.sentences(root)
                self.assertEqual(len(complete), len(found))
                for node in complete:
                    self.assertEqual(node.text, suggest.sentence(node.plan))
                    self.assertEqual("".join(suggest.pieces(node.plan)), node.text)
                    read = contract.exact_calls(node.text)
                    self.assertEqual(contract._canonical(read), contract._canonical(node.plan))
                    self.assertTrue(contract.admit(node.text, read).actions)
                    self.assertIsNone(suggest.refusal(request, node.plan))
                    for value in (v for c in node.plan for k, v in c["arguments"].items()
                                  if k in ("directory", "file_path", "task", "text", "model")):
                        self.assertIn(value, paths + texts + models)     # copied, never made up

                def walk(node):     # every partial sentence goes on to a complete one
                    offered = suggest.continuations(node)
                    self.assertTrue(offered)
                    for piece, child in offered:
                        if child is None:
                            self.assertIsNotNone(node.plan)
                        else:
                            self.assertEqual(child.text, node.text + piece)
                            walk(child)
                walk(root)
        self.assertTrue(all(sizes) and max(sizes) > 5, sizes)
        pdf = suggest.plans(SAMPLE[9])      # a PDF is a file to open, never a directory
        self.assertEqual(pdf, [[call("open_app", app="kilix-pdf", file_path="~/papers/fern\u00e9.pdf"),
                                call("wait_for_agent", agent="claude")]])

    def test_the_fewest_operations_in_the_requests_order(self):
        self.assertEqual(suggest.plans("The calculator and the notepad, please"),
                         [[call("open_app", app="kilix-calculator"), call("open_app", app="kilix-notepad")]])
        found = suggest.plans("Claude and codex in ./tide-pool, please")
        self.assertTrue(found and all(len(calls) == 2 for calls in found))
        self.assertTrue(all("claude" in json.dumps(calls[0]) for calls in found))     # claude is named first
        # An operation that adds nothing the request names is never offered: no third call, no extra wait.
        self.assertEqual(suggest.plans('Run codex in dusk-ferry and tell it "lint only"'),
                         [[call("message_agent", agent="codex", directory="dusk-ferry", text="lint only")]])

    def test_a_plan_the_grammar_does_not_read_back_whole_is_not_in_the_tree(self):
        carried = suggest._carried
        with mock.patch.object(suggest, "_carried", lambda calls: carried(calls) if len(calls) < 2 else None):
            self.assertEqual(suggest.plans("The calculator and the notepad, please"), [])
        self.assertTrue(suggest.plans("The calculator and the notepad, please"))

    def test_an_operation_must_add_something_the_request_names(self):
        context = suggest._context('Run codex in dusk-ferry and tell it "lint only"')
        start = call("start_codex", directory="dusk-ferry")
        self.assertFalse(suggest._gains(context, [start], call("wait_for_agent", agent="codex", directory="dusk-ferry")))
        self.assertTrue(suggest._gains(context, [start], call("message_agent", agent="codex", text="lint only")))
        self.assertTrue(suggest._gains(context, [], start))
        twice = suggest._context("Fire up Codex in mill-pond, then Claude in the same directory")
        self.assertTrue(suggest._gains(twice, [call("start_codex", directory="mill-pond")],
                                       call("start_claude", directory="mill-pond")))

    def test_what_the_requests_words_rule_in_and_out(self):
        tab = suggest.plans("Codex in a new tab, please")
        self.assertNotIn("place", json.dumps(tab))         # "a new tab" says no split
        self.assertTrue(any(c["arguments"].get("place") == "down" for p in suggest.plans(SPLIT) for c in p))
        same = suggest.plans("Fire up Codex in mill-pond, then Claude in the same directory")
        self.assertTrue(same and all([c["arguments"].get("directory") for c in p] == ["mill-pond"] * 2 for p in same))
        # A model the request names as one is never left out, and one the grammar cannot carry means no plan.
        self.assertTrue(all("opus" in json.dumps(p) for p in
                            suggest.plans('Claude in "plum harbor", model opus, task "open the log"')))
        self.assertEqual(suggest.plans("Claude in ./a, model gpt-9-zeta, please"), [])
        self.assertIn([call("wait_for_agent", agent="claude", directory="./a")],
                      suggest.plans("Would you kindly hold on until Claude in ./a finishes"))     # "on until" names no model
        for request in ("Quit Codex in a split on the right.", "start codex in a split to the right",
                        "what is the weather like", "Start Codex if the tests pass"):
            self.assertEqual(suggest.plans(request), [])

    def test_the_model_chooses_only_among_the_grammars_continuations(self):
        wording = "start codex in a split below"
        model = builds(wording)
        calls, steps = suggest.build(SPLIT, model)
        self.assertEqual(calls, [call("start_codex", place="down")])
        sentences = [node.text for node in suggest.sentences(suggest.tree(suggest.plans(SPLIT)))]
        asked = {question: options for question, options, _ in model.asked}
        self.assertEqual(list(asked), [suggest.GATE_QUESTION, suggest.STEP_QUESTION, suggest.WHOLE_QUESTION,
                                       suggest.VERIFY_QUESTION])
        # Each continuation is shown as the whole sentence so far plus the piece; the end only where a plan is complete.
        self.assertEqual(asked[suggest.STEP_QUESTION]["0"], "start codex   (complete)")
        for key, text in asked[suggest.STEP_QUESTION].items():
            if key != "0":
                self.assertTrue(text.startswith("start codex in a split"), text)
                self.assertIn(text.replace("   (complete)", ""), sentences)
        self.assertEqual(sorted(asked[suggest.WHOLE_QUESTION].values()), sorted(sentences))
        self.assertEqual([step["kind"] for step in steps], ["gate", "step", "step", "whole", "verify"])
        self.assertTrue(model.asked[-1][2].endswith("The assistant's plan:\n" + suggest.plan_text(calls)))
        # The two readings disagree: no plan, and the final check is not asked.
        model = Model(COMMAND, ("Which continuation", toward(wording)),
                      ("Which command does exactly", toward("start codex")), YES)
        self.assertEqual(suggest.build(SPLIT, model)[0], [])
        self.assertNotIn(suggest.VERIFY_QUESTION, [question for question, _, _ in model.asked])

    def test_many_whole_sentences_are_ranked_a_group_at_a_time(self):
        request = "Wait for Claude in ./alpha and for Codex in ./beta, then open the notepad"
        found = suggest.plans(request)
        wanted = 'wait for claude in "./alpha"; then wait for codex in "./beta"; then open the notepad'
        with mock.patch.object(suggest, "WHOLE_GROUP", 2):
            model = builds(wanted)
            calls, _steps = suggest.build(request, model)
        self.assertEqual(suggest.sentence(calls), wanted)
        groups = [options for question, options, _ in model.asked if question == suggest.WHOLE_QUESTION]
        self.assertTrue(len(found) > 2 and len(groups) == len(found) - 1, (len(found), len(groups)))
        self.assertTrue(all(len(options) <= 2 for options in groups))


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

            def do_GET(self):
                data = json.dumps({"models": [{"name": "d3:test"}, {"name": "other:latest"}]}).encode()
                self.send_response(200 if self.path == "/api/tags" else 404)
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


class Rules(unittest.TestCase):
    """refusal(): what the request's own words rule out, whichever model made the plan."""

    def test_a_plan_the_request_states_is_allowed(self):
        cases = {
            "Bring up the notepad app for me.": [call("open_app", app="kilix-notepad")],
            "Run Codex in dusk-ferry in a split below": [call("start_codex", directory="dusk-ferry", place="down")],
            'wait for claude in yarrow-valve to finish, then start codex there in a right split': [
                call("wait_for_agent", agent="claude", directory="yarrow-valve"),
                call("start_codex", directory="yarrow-valve", place="right")],
            'In myrtle-piston there is a Claude session; send it "which migrations are pending"': [
                call("message_agent", agent="claude", directory="myrtle-piston", text="which migrations are pending")],
            "Wait till Claude's current turn ends.": [call("wait_for_agent", agent="claude")],
            "Start codex in an up split and claude in a left split, both in ./walnut-relay": [
                call("start_codex", directory="./walnut-relay", place="up"),
                call("start_claude", directory="./walnut-relay", place="left")],
            "Start both Codex and Claude here": [call("start_codex"), call("start_claude")],
        }
        for request, calls in cases.items():
            with self.subTest(request=request):
                self.assertIsNone(suggest.refusal(request, calls))
                # The grammar reads some of these itself now; those need no suggestion.
                read = contract.exact_calls(request)
                if read:
                    self.assertEqual(read, calls)
                    self.assertFalse(suggest.possible(request))
                else:
                    self.assertTrue(suggest.possible(request))
        # An agent's name inside a model's name is not the agent named a second time.
        self.assertIsNone(suggest.refusal("Kick off Claude with model claude-opus-4-5 in the current directory",
                                          [call("start_claude", model="claude-opus-4-5")]))

    def test_what_the_words_rule_out(self):
        cases = [
            ("Open the editor in /srv/x", [call("open_app", app="kilix-notepad")], "unnamed"),
            ("Every Monday morning, begin a codex session in ~/work/x", [call("start_codex", directory="~/work/x")],
             "conditional"),
            ('once the review is approved, tell claude "merge it"', [call("message_agent", agent="claude", text="merge it")],
             "conditional"),
            ("Start Codex in", [call("start_codex")], "cut off"),
            ("Start Codex here", [call("start_claude")], "unnamed"),
            ("Open the calculator", [call("open_app", app="kilix-notepad")], "unnamed"),
            ("Start Codex in /srv/x", [call("start_codex", directory="/srv/y")], "reworded"),
            ('Tell Claude "fix the tests"', [call("message_agent", agent="claude", text="fix tests")], "reworded"),
            ("Start Codex here", [call("start_codex", place="right")], "unnamed"),
            ("Start Codex in a split below", [call("start_codex", place="right")], "unnamed"),
            ("Start Codex here and Codex here", [call("start_codex"), call("start_codex")], "repeated"),
            ("Start Codex in /srv/x in a split below", [call("start_codex", place="down")], "unused"),
            ("Start Codex here, then wait for Claude", [call("start_codex"), call("wait_for_agent", agent="codex")],
             "unused"),
            ('Send Codex in crimson-ladder "rerun" and then wait for Codex in crimson-ladder',
             [call("message_agent", agent="codex", text="rerun"),
              call("wait_for_agent", agent="codex", directory="crimson-ladder")], "unused"),
            ("wait for claude in yarrow-valve to finish, then start codex there in a right split",
             [call("wait_for_agent", agent="claude", directory="yarrow-valve"), call("start_codex", place="right")],
             "unused"),
            ('Message claude in ~/src/x "write the changelog", then start codex here in a right split',
             [call("message_agent", agent="claude", directory="~/src/x", text="write the changelog"),
              call("start_codex", task="write the changelog", place="right")], "reused"),
            ('Start Claude here. Task: "fix the files under lib/"',
             [call("message_agent", agent="claude", text="fix the files under lib/")], "unused"),
            ('Tell Codex "pause", then wait for Codex', [call("message_agent", agent="codex", text="pause")], "unused"),
            ("Quit the calculator app.", [call("open_app", app="kilix-calculator")], "unsupported"),
            ("Start Codex here for every branch", [call("start_codex")], "conditional"),
            ("Start Codex here once the build is green", [call("start_codex")], "conditional"),
            ("Stop the Claude session here.", [call("wait_for_agent", agent="claude")], "unsupported"),
            ('Send claude in aspen-winch "pull" and after that wait for claude in aspen-winch',
             [call("start_claude", directory="aspen-winch", task="pull"),
              call("wait_for_agent", agent="claude", directory="aspen-winch")], "unused"),
            ("Start codex in an up split and claude in a left split, both in ./walnut-relay",
             [call("start_codex", place="up"), call("start_claude", directory="./walnut-relay", place="left")], "unused"),
            ('Start Codex here, then wait for Claude, then message Claude "go"',
             [call("start_codex"), call("wait_for_agent", agent="codex"),
              call("message_agent", agent="claude", text="go")], "unused"),
        ]
        for request, calls, why in cases:
            with self.subTest(request=request):
                self.assertEqual(suggest.refusal(request, calls), why)

    def test_no_model_is_needed_to_see_that_nothing_can_be_suggested(self):
        for request in ("start codex in a split to the right", "what is the weather like", "Start Codex in",
                        "Start Codex here unless it is busy", "", "open the calculator " + "x" * 2048):
            with self.subTest(request=request):
                self.assertFalse(suggest.possible(request))

    def test_a_proposal_is_tidied_or_dropped(self):
        self.assertEqual(suggest.tidy([call("start_codex", place="tab", directory="here")]), [call("start_codex")])
        self.assertEqual(suggest.tidy([call("start_codex", place="left", directory=".")]),
                         [call("start_codex", place="left")])
        for bad in (None, [], "start", [{"name": "rm", "arguments": {}}], [{"name": "start_codex"}],
                    [call("start_codex", directory=7)], [None]):
            with self.subTest(bad=bad):
                self.assertIsNone(suggest.tidy(bad))


class Sources(unittest.TestCase):
    """suggest(): a decision model's walk, a command model's proposal, or both in agreement."""
    REQUEST = "Codex in a split on the right, please."
    PLAN = [call("start_codex", place="right")]
    FOUND = {"request": "start codex in a split to the right", "calls": [call("start_codex", place="right")]}
    WALK = (COMMAND, YES)       # the request has one possible wording
    NO_WALK = (COMMAND, YES)

    def test_a_command_model_alone_offers_a_proposal_that_passes_the_rules(self):
        found = suggest.suggest(self.REQUEST, propose=lambda: [call("start_codex", place="right", directory="here")])
        self.assertEqual(found, {**self.FOUND, "source": "proposal"})
        for proposal in ([call("start_codex", place="left")], [call("start_claude", place="right")], [],
                         [call("start_codex", place="right", model="gpt-9-zeta")], None, "nonsense"):
            with self.subTest(proposal=proposal):
                self.assertIsNone(suggest.suggest(self.REQUEST, propose=lambda proposal=proposal: proposal))
        self.assertIsNone(suggest.suggest(self.REQUEST))

    def test_a_decision_model_alone_offers_its_plan_when_every_answer_is_sure(self):
        self.assertEqual(suggest.suggest(self.REQUEST, Model(*self.WALK)), {**self.FOUND, "source": "built"})
        self.assertIsNone(suggest.suggest(self.REQUEST, Model(("What kind", "question"), YES)))
        unsure = (COMMAND, ("Does the plan do exactly", "yes", 0.7))
        self.assertIsNone(suggest.suggest(self.REQUEST, Model(*unsure)))

    def test_with_both_the_two_plans_must_be_the_same(self):
        self.assertEqual(suggest.suggest(self.REQUEST, Model(*self.WALK), propose=lambda: self.PLAN),
                         {**self.FOUND, "source": "agreed"})
        spelled = [call("start_codex", place="right", directory="here")]     # the same plan, defaults spelled out
        self.assertEqual(suggest.suggest(self.REQUEST, Model(*self.WALK), propose=lambda: spelled)["source"], "agreed")
        for proposal in ([call("start_codex")], [call("start_codex", place="left")], [], None,
                         [call("start_codex", place="right"), call("wait_for_agent", agent="codex")]):
            with self.subTest(proposal=proposal):
                self.assertIsNone(suggest.suggest(self.REQUEST, Model(*self.WALK),
                                                  propose=lambda proposal=proposal: proposal))
        # Agreement is the check: the walk's own doubt about a plan both made does not refuse it.
        unsure = (COMMAND, ("Does the plan do exactly", "yes", 0.7))
        self.assertEqual(suggest.suggest(self.REQUEST, Model(*unsure), propose=lambda: self.PLAN)["source"], "agreed")

    def test_the_command_model_is_not_asked_when_nothing_can_be_suggested_or_the_decision_model_gives_no_plan(self):
        propose = mock.Mock(return_value=self.PLAN)
        for request in ("start codex in a split to the right", "what is the weather like",
                        "Open Codex in a split on the right unless it is busy"):
            self.assertIsNone(suggest.suggest(request, Model(), propose=propose))
        # the step-by-step sentence and the ranking of whole sentences disagree: no plan
        split = Model(COMMAND, ("Which continuation", toward("start codex in a split below")),
                      ("Which command does exactly", toward("start codex in a split above")), YES)
        self.assertIsNone(suggest.suggest(SPLIT, split, propose=propose))
        propose.assert_not_called()

    def test_a_planned_request_is_held_to_the_rules_too(self):
        request = "Quit Codex in a split on the right."
        self.assertTrue(suggest.possible(request))
        model = Model(*self.WALK)
        self.assertEqual(suggest.build(request, model)[0], [])     # no plan passes refusal(), so none is built
        self.assertEqual(model.asked, [])
        walked = (COMMAND, ("Which operation", "start_codex"), ("Where should", "right"), YES)
        calls, steps = suggest.walk(request, Model(*walked))
        self.assertEqual(calls, self.PLAN)
        self.assertTrue(all(value >= limit for value, limit in
                            zip(suggest.signals(steps), (suggest.GATE, suggest.STEP, suggest.VERIFY))))
        self.assertEqual(suggest.refusal(request, calls), "unsupported")
        self.assertIsNone(suggest.suggest(request, Model(*walked)))
        self.assertIsNone(suggest.suggest(request, Model(*walked), propose=lambda: self.PLAN))

    def test_a_decision_model_that_cannot_be_asked_offers_nothing(self):
        def down(*_):
            raise suggest.SuggestError("down")
        self.assertIsNone(suggest.suggest(self.REQUEST, down))
        self.assertIsNone(suggest.suggest(self.REQUEST, down, propose=lambda: self.PLAN))

    def test_the_listing_says_whether_the_decision_model_is_there(self):
        server = Server(lambda body: {})
        self.addCleanup(server.close)
        self.assertIs(suggest.available("d3:test", server.host), True)
        self.assertIs(suggest.available("other", server.host), True)
        self.assertIs(suggest.available("d3:2b", server.host), False)
        self.assertIsNone(suggest.available("d3:test", "127.0.0.1:1"))
        self.assertIsNone(suggest.available("d3:test", "example.com:11434"))


class Isolated(unittest.TestCase):
    """No saved preference and no setting from the environment, unless a test makes one."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state" / "ask-suggest.json"
        patches = (mock.patch.object(commands_selection, "suggest_path", return_value=self.path),
                   mock.patch.dict(os.environ))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("KILIX_ASK_SUGGEST", None)
        os.environ.pop("OLLAMA_HOST", None)


class Setting(Isolated):
    def test_the_environment_then_the_saved_choice_then_off(self):
        self.assertEqual(commands_selection.suggest_choice(), ("off", "default"))
        self.assertIsNone(ask.suggester())
        self.assertEqual(commands_selection.save_suggest(" d3:2b "), "d3:2b")
        self.assertEqual(json.loads(self.path.read_text()), {"schema": "kilix.ask.suggest/v1", "suggest": "d3:2b"})
        self.assertEqual(commands_selection.suggest_choice(), ("d3:2b", "saved"))
        os.environ["KILIX_ASK_SUGGEST"] = "OFF"
        self.assertEqual(commands_selection.suggest_choice(), ("off", "environment"))
        os.environ["KILIX_ASK_SUGGEST"] = "  "
        self.assertEqual(commands_selection.suggest_choice(), ("d3:2b", "saved"))

    def test_a_value_that_is_no_model_name_is_refused_and_nothing_is_saved(self):
        for value in ("", "two words", "-rf", "x" * 129, "d3;2b", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                commands_selection.save_suggest(value)
        self.assertFalse(self.path.exists())
        os.environ["KILIX_ASK_SUGGEST"] = "two words"
        with self.assertRaises(ValueError):
            commands_selection.suggest_choice()
        self.assertIsNone(ask.suggester())       # an unusable setting offers nothing
        os.environ.pop("KILIX_ASK_SUGGEST")
        self.path.parent.mkdir(parents=True)
        for text in ("{}", '{"schema": "kilix.ask.suggest/v1", "suggest": "two words"}', "[1]", "x" * 5000,
                     '{"schema": "kilix.ask/v1", "suggest": "off"}'):
            self.path.write_text(text)
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                commands_selection.suggest_choice()

    def test_workflows_shows_and_saves_the_setting(self):
        self.assertEqual(workflows.suggestions()["detail"], "Ask suggestions are off (default).")
        self.assertIn("command model's own proposal (saved)", workflows.suggestions("model")["detail"])
        self.assertEqual(workflows.suggestions("off")["detail"], "Ask suggestions are off (saved).")
        server = Server(lambda body: {})
        self.addCleanup(server.close)
        os.environ["OLLAMA_HOST"] = server.host
        there = workflows.suggestions("d3:test")
        self.assertEqual((there["suggest"], there["source"], there["available"]), ("d3:test", "saved", True))
        self.assertIn("is on the local Ollama server", there["detail"])
        self.assertIs(workflows.suggestions("d3:2b")["available"], False)
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        self.assertIn("did not answer", workflows.suggestions()["detail"])
        with mock.patch("builtins.print") as shown:
            self.assertEqual(workflows.main(["suggest", "--set", "model", "--json"]), 0)
            self.assertEqual(json.loads(shown.call_args.args[0])["source"], "saved")
        with mock.patch("sys.stderr"):
            self.assertEqual(workflows.main(["suggest", "--set", "two words"]), 1)
        self.assertEqual(commands_selection.suggest_choice(), ("model", "saved"))
        self.assertEqual(len(server.bodies), 0)      # looking a model up asks it nothing


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


class Offer(Isolated):
    REQUEST = "Codex in a split on the right, please."

    def ask(self, jobs, routed, **options):
        with mock.patch.object(router, "route", return_value=routed), mock.patch.object(ask, "_record_unsure"):
            return ask.ask(options.pop("request", self.REQUEST), jobs, act=options.pop("act", False), **options)

    def test_a_request_that_planned_nothing_gets_the_wording_and_nothing_runs(self):
        seen = []
        jobs = Jobs(NOTHING, lambda request, record: seen.append(request) or FOUND)
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
            self.assertEqual(suggester.call_args.args[0], self.REQUEST)

    def test_the_command_line_offers_suggestions_to_a_person_only(self):
        import argparse
        for setting, offered in ((None, False), ("off", False), ("model", True), ("d3:test", True)):
            if setting:
                os.environ["KILIX_ASK_SUGGEST"] = setting
            for agent in (False, True):
                jobs, stack = ask.cli_jobs(argparse.Namespace(agent=agent), interactive=False)
                with stack:
                    self.assertEqual(jobs.suggest is not None, offered and not agent)


class Session:
    """The command session's part in a suggestion: its backend and its model's decision."""

    def __init__(self, backend, calls=(), source=None):
        self.backend, self.calls, self.source, self.asked = backend, list(calls), source or backend, []

    def decide(self, request):
        self.asked.append(request)
        return type("Decision", (), {"source": self.source, "calls": self.calls})()


class Suggester(Isolated):
    """ask.suggester(): which sources a setting and a command session give."""
    REQUEST = "Codex in a split on the right, please."
    PLAN = [call("start_codex", place="right")]

    def setUp(self):
        super().setUp()
        os.environ["KILIX_ASK_SUGGEST"] = "model"

    def test_the_proposal_admission_refused_is_reused_without_asking_the_model_again(self):
        session = Session("lfm2", self.PLAN)
        record = {**NOTHING, "interpretation": "lfm2", "calls": self.PLAN}
        found = ask.suggester(session)(self.REQUEST, record)
        self.assertEqual(found, {**FOUND, "source": "proposal", "model": "lfm2"})
        self.assertEqual(session.asked, [])

    def test_the_selected_command_model_is_asked_when_the_request_never_reached_it(self):
        session = Session("needle3", self.PLAN)
        self.assertEqual(ask.suggester(session)(self.REQUEST, dict(NOTHING)),
                         {**FOUND, "source": "proposal", "model": "needle3"})
        self.assertEqual(session.asked, [self.REQUEST])
        ask.suggester(session)("what is the weather like", dict(NOTHING))
        self.assertEqual(session.asked, [self.REQUEST])      # nothing could be suggested, so it was not asked
        # Calls that did not come from the model (its session fell back to the grammar) are no proposal.
        fallen = Session("needle3", self.PLAN, source="grammar")
        self.assertIsNone(ask.suggester(fallen)(self.REQUEST, dict(NOTHING)))
        self.assertEqual(fallen.asked, [self.REQUEST])

    def test_the_grammar_alone_has_no_proposal_to_offer(self):
        session = Session("grammar", self.PLAN)
        self.assertIsNone(ask.suggester(session)(self.REQUEST, {**NOTHING, "interpretation": "grammar", "calls": []}))
        self.assertIsNone(ask.suggester()(self.REQUEST, dict(NOTHING)))
        self.assertEqual(session.asked, [])

    def decision_model(self, answers):
        def reply(body):
            question = body["questions"]["q"]
            chosen = next(pick for words, pick in answers.items() if words in question["instructions"])
            return {"answers": {"q": {"probabilities": {key: 0.9 if key == chosen else 0.1 / (len(question["criteria"]) - 1)
                                                        for key in question["criteria"]}}}}
        server = Server(reply)
        self.addCleanup(server.close)
        os.environ.update(KILIX_ASK_SUGGEST=" d3:test ", OLLAMA_HOST=server.host)
        return server

    def test_a_decision_model_with_the_grammar_selected_offers_its_plan(self):
        answers = {"What kind": "command", "Does the": "yes"}
        server = self.decision_model(answers)
        for session in (None, Session("grammar")):
            self.assertEqual(ask.suggester(session)(self.REQUEST, dict(NOTHING)),
                             {**FOUND, "source": "built", "model": "d3:test"})
        self.assertEqual(({body["model"] for _, body in server.bodies}, len(server.bodies)), ({"d3:test"}, 4))
        answers["What kind"] = "question"
        self.assertIsNone(ask.suggester()(self.REQUEST, dict(NOTHING)))

    def test_a_decision_model_and_a_command_model_must_agree(self):
        answers = {"What kind": "command", "Does the": "yes"}
        self.decision_model(answers)
        session = Session("lfm2", self.PLAN)
        record = {**NOTHING, "interpretation": "lfm2", "calls": self.PLAN}
        self.assertEqual(ask.suggester(session)(self.REQUEST, record), {**FOUND, "source": "agreed", "model": "d3:test"})
        self.assertEqual(ask.suggester(session)(self.REQUEST, dict(NOTHING))["source"], "agreed")
        self.assertEqual(session.asked, [self.REQUEST])      # asked only because the record held no proposal
        other = {**NOTHING, "interpretation": "lfm2", "calls": [call("start_codex", place="left")]}
        self.assertIsNone(ask.suggester(session)(self.REQUEST, other))
        self.assertIsNone(ask.suggester(session)(self.REQUEST, {**NOTHING, "interpretation": "lfm2", "calls": []}))
        # The decision model makes no plan (its sentence and its pick of whole sentences differ):
        # the proposal alone is not offered.
        answers.update({"Which continuation": "2", "Which command": "1"})       # "wait for codex", "start codex"
        self.assertIsNone(ask.suggester(session)(SPLIT, record))
        answers["Which command"] = "6"
        self.assertIsNone(ask.suggester(session)(SPLIT, record))    # both "wait for codex": not the proposal either

    def test_a_decision_model_that_is_not_there_offers_nothing(self):
        session = Session("lfm2", self.PLAN)
        record = {**NOTHING, "interpretation": "lfm2", "calls": self.PLAN}
        for host in ("127.0.0.1:1", "example.com"):      # no server; never another computer
            os.environ.update(KILIX_ASK_SUGGEST="d3:test", OLLAMA_HOST=host)
            with self.subTest(host=host):
                self.assertIsNone(ask.suggester(session)(self.REQUEST, record))
                self.assertIsNone(ask.suggester()(self.REQUEST, dict(NOTHING)))


if __name__ == "__main__":
    unittest.main()
