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


def kinds(steps):
    return [step["kind"] if step["kind"] != "fixed" else "fixed " + step["pick"].split("=")[0] for step in steps]


def plan_key(plan):
    """A rule's answer for the final ranking: the option that is this plan."""
    wording = "; then ".join(suggest.describe(c) for c in plan)
    return lambda options: next(key for key, text in options.items() if text == wording)


class Walk(unittest.TestCase):
    """walk(): the decision model picks each operation in turn and each argument the words leave open."""

    def test_a_plan_is_built_only_from_what_the_request_names(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("What does the request ask for next", "done"), YES)
        calls, steps = suggest.walk("Put Codex in a pane above, in /work/tern.v2.", model)
        self.assertEqual(calls, [call("start_codex", directory="/work/tern.v2", place="up")])
        # The only path and the one direction named are taken without a question.
        self.assertEqual(kinds(steps), ["gate", "operation", "fixed directory", "fixed place", "operation", "verify"])
        # Claude, the applications and the other directions are not there to choose.
        self.assertEqual(set(model.questions("Which operation")[0]), {"start_codex", "wait_for_agent", "other"})
        self.assertEqual(suggest.walk_signals(steps), (0.95, 0.95, 1.0))

    def test_an_argument_the_words_leave_open_is_asked(self):
        request = "Start codex in ./alpha or in ./beta in a split above or below"
        self.assertEqual(suggest.walk(request, Model())[1][-1]["pick"], "conditional")
        request = "Start claude in ./alpha in a split above, then wait for codex in ./beta"
        model = Model(COMMAND, ("Which operation does the request ask for first", "start_claude"),
                      ("What does the request ask for next", "wait_for_agent"),
                      ("Which agent", "codex"), ("Which directory is the first", "1"),
                      ("Which directory is the second", "2"), ("Where should", "up"), YES)
        calls, steps = suggest.walk(request, model)
        self.assertEqual(calls, [call("start_claude", directory="./alpha", place="up"),
                                 call("wait_for_agent", agent="codex", directory="./beta")])
        # The first operation is not the last one expected, so its place is asked; with two paths named,
        # each operation's directory is asked. Two agents are named, so the wait's is asked.
        self.assertEqual(kinds(steps), ["gate", "operation", "directory", "place", "operation", "agent",
                                        "directory", "verify"])
        self.assertEqual(model.questions("Which directory")[0],
                         {"1": "./alpha", "2": "./beta",
                          "0": "the current directory: the request names no directory for this operation"})

    def test_words_the_tools_cannot_express_refuse_before_any_question(self):
        for request, why in (("Start a coding session in /srv/x", "unnamed"),
                             ("Start Codex in /srv/x unless it is busy", "conditional"),
                             ("Open the notepad or the calculator", "conditional"),
                             ("Tell claude in pane 4: the build is green", "target"),
                             ("message codex 0a1b2c3d-1111-2222-3333-444455556666: hi", "target"),
                             ("Start codex in a new window", "target"),
                             ("Start claude in ~/x with high reasoning effort", "effort"),
                             ("Start codex in ~/x with model gpt-6 high: fix it", "effort"),
                             ("Resume my last claude session in ~/x", "resume"),
                             ("Restart codex here", "resume"),
                             ("Open a codex tab titled review", "title"),
                             ("start codex --yolo in ~/x", "flag"),
                             ("Tell claude and grok: hello there", "other agent"),
                             ("Ping the other claude with 'rebase'", "other agent"),
                             ("Start codex on the ssh host", "host"),
                             ("Imagine starting claude in ~/x", "not meant"),
                             ("Yesterday I started codex in ~/x", "past"),
                             ("at 6pm, start claude in sage-dynamo", "later"),
                             ("Start codex in the other folder", "unnamed place"),
                             ("Start codex in ~/x as root", "unsupported"),
                             ("Start claude with model claude-opus-9 in ~/x", "model"),
                             ("Fire up claude with gpt-6.1-sol in ~/x", "unused"),
                             ("Start codex in a split", "unplaced"),
                             ("Start claude in ~/x with the task", "cut off"),
                             ("bring up codex in the folder pine-cone", "cut off"),
                             ("start codex in ~/", "cut off"),
                             ("Need claude. Directory: /opt/x.", "fields"),
                             ("codex in ~/x: let it finish its turn", "unread text"),
                             ("Start claude in ~/x and push the branch to origin", "unsupported"),
                             ("Open the calculator and work out 18% of 2,350", "unsupported"),
                             ("Message claude 'done' and then email the team", "unsupported")):
            with self.subTest(request=request):
                model = Model(COMMAND, ("Which operation", "start_claude"), YES)
                calls, steps = suggest.walk(request, model)
                self.assertEqual(calls, [])
                self.assertEqual(steps[-1]["pick"], why)
                if why != "unused":
                    self.assertEqual((model.asked, suggest.walk_refuses(request)), ([], why))

    def test_plain_requests_are_not_refused_by_those_words(self):
        for request in ("Codex please, left split, in tamarind-sledge", "Open a notepad window.",
                        "I've finished the migration in ~/x. Please tell the codex session there: rerun it",
                        "Spawn codex in ~/x; initial task \"read TODO.md\"", "Open the notepad app; that's all.",
                        "Please tell Claude, verbatim, \"I will review the diff after lunch.\"",
                        "pull the file manager up for me, would you", "Start Claude and Codex, both in ~/x",
                        "Hey, let claude in ~/x know: the API key rotated, restart your dev server.",
                        "Ping codex in ~/x: the fixtures moved to tests/data, rerun it.",
                        "Kick off codex here. Its first job: \"start the dev server\"", "Calculator and notepad, please."):
            with self.subTest(request=request):
                self.assertIsNone(suggest.walk_refuses(request))

    def test_operations_are_asked_one_at_a_time(self):
        model = Model(COMMAND, ("Which operation does the request ask for first", "start_codex"),
                      ("What does the request ask for next", "open_app"), YES)
        calls, steps = suggest.walk("First start Codex here, then open the calculator.", model)
        self.assertEqual(calls, [call("start_codex"), call("open_app", app="kilix-calculator")])
        # The second question sees the first operation as planned, and may end the plan there.
        question, options, state = model.asked[2]
        self.assertIn("Already planned:\n1. start a new Codex coding session", state)
        # Nothing more is not offered while the calculator the request names is not opened.
        self.assertEqual(set(options), {"start_codex", "wait_for_agent", "open_app", "other"})
        # Two clauses allow two operations: no third question.
        self.assertEqual(len(model.questions("Which operation")) + len(model.questions("What does")), 2)

    def test_a_request_that_opens_with_a_launch_begins_with_one(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("What does", "open_app"), YES)
        calls, _ = suggest.walk("Open codex here, then the notepad", model)
        self.assertEqual(calls, [call("start_codex"), call("open_app", app="kilix-notepad")])
        self.assertEqual(set(model.questions("Which operation")[0]), {"start_codex", "other"})
        model = Model(COMMAND, ("Which operation", "open_app"), ("What does", "start_codex"), YES)
        suggest.walk("The notepad, then open codex here", model)
        self.assertIn("open_app", model.questions("Which operation")[0])

    def test_nothing_more_is_offered_only_once_nothing_named_is_left_out(self):
        model = Model(COMMAND, ("Which operation", "open_app"), ("What does the request ask for next", "open_app"),
                      YES)
        calls, _ = suggest.walk("The notepad and the calculator, please", model)
        self.assertEqual(calls, [call("open_app", app="kilix-notepad"), call("open_app", app="kilix-calculator")])
        self.assertNotIn("done", model.questions("What does the request ask for next")[0])
        model = Model(COMMAND, ("Which operation", "message_agent"), ("What does the request ask for next", "done"),
                      YES)
        calls, steps = suggest.walk('Tell claude "lint only", then sit tight', model)
        self.assertEqual(calls, [call("message_agent", agent="claude", text="lint only")])
        self.assertIn("done", model.questions("What does the request ask for next")[0])

    def test_an_unsupported_part_refuses_the_whole_request(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("What does the request ask for next", "other"), YES)
        calls, steps = suggest.walk("Start codex here and get me a coffee.", model)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "operation"))
        self.assertEqual(model.questions("Does the plan"), [])

    def test_the_same_operation_twice_is_not_a_plan(self):
        model = Model(COMMAND, ("Which operation", "start_claude"), ("What does", "start_claude"), YES)
        calls, steps = suggest.walk("Start Claude here and Codex here.", model)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "repeated"))

    def test_both_asks_for_each_agent_again(self):
        answers = iter(["start_codex", "wait_for_agent", "done"])
        model = Model(COMMAND, ("Which operation", "start_claude"), ("What does", lambda options: next(answers)),
                      ("Which agent", "codex"), YES)
        calls, steps = suggest.walk("Start claude, then codex, then wait for both.", model)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "unused"))
        # "both" allows one operation more than the clauses do.
        self.assertEqual(len(model.questions("What does")), 3)

    def test_the_plan_must_win_against_its_near_misses(self):
        request = "In ~/x: start claude, then wait for codex"
        plan = [call("start_claude", directory="~/x"), call("wait_for_agent", agent="codex", directory="~/x")]
        rules = (COMMAND, ("Which operation does the request ask for first", "start_claude"),
                 ("What does the request ask for next", "wait_for_agent"), ("Which agent", "codex"),
                 ("Which directory", "1"))
        model = Model(*rules, ("Which plan does exactly", plan_key(plan)))
        calls, steps = suggest.walk(request, model)
        self.assertEqual(calls, plan)
        options = model.questions("Which plan does exactly")[0]
        # Only near misses that the rules allow compete: the wait without its directory, or the launch
        # without it (the other agent would leave codex unused). The plan's own key is recorded, so
        # walk_signals reads its odds.
        self.assertEqual(sorted(text for key, text in options.items() if key != "0"), sorted([
            "; then ".join(suggest.describe(c) for c in variant) for variant in (
                plan, [plan[0], call("wait_for_agent", agent="codex")], [call("start_claude"), plan[1]])]))
        self.assertIn("0", options)
        self.assertEqual(model.questions("Does the plan"), [])
        self.assertEqual(suggest.walk_signals(steps)[2], 0.95)
        near = Model(*rules, ("Which plan does exactly", plan_key([plan[0], call("wait_for_agent", agent="codex")])))
        calls, steps = suggest.walk(request, near)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "check"))

    def test_a_plan_the_rules_refuse_is_not_checked(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), YES)
        calls, steps = suggest.walk("Quit Codex in a split on the right.", model)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "rules"))
        self.assertEqual(model.questions("Does the plan"), [])

    def test_a_weak_yes_refuses(self):
        model = Model(COMMAND, ("Which operation", "start_codex"), ("Does the plan do exactly", "yes", 0.7))
        calls, steps = suggest.walk("Codex in a split on the right, please.", model)
        self.assertEqual((calls, steps[-1]["pick"]), ([], "verify"))


def toward(wording):
    """A rule's answer: the option on the way to `wording`, or that whole sentence when it is offered."""
    def pick(options):
        def score(text):
            text = text.replace("   (complete)", "").removesuffix(" ...")
            return 2 if text == wording else 1 if wording.startswith(text) else 0
        return max(options, key=lambda key: score(options[key]))
    return pick


SPLIT = "Bring up Codex in a split pane."
# plans() gives SPLIT none now (a split said without its direction leaves where open), so the tests of how a
# tree is built and checked give it the six plans it once had: a tab, four splits, or a wait.
SPLIT_PLANS = [[call("start_codex")],
               *([call("start_codex", place=place)] for place in ("right", "left", "down", "up")),
               [call("wait_for_agent", agent="codex")]]


def given_plans(request=SPLIT, found=SPLIT_PLANS):
    """plans() gives `request` the plans `found`, and any other request its own."""
    plans = suggest.plans
    return mock.patch.object(suggest, "plans", lambda text: [list(calls) for calls in found] if text == request
                             else plans(text))


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

    def test_the_walk_can_stand_in_for_the_built_sentence(self):
        request = "Codex in a split on the right, please."
        model = Model(COMMAND, ("Which operation", "start_codex"), YES)
        found = suggest.suggest(request, model, plan=suggest.walk)
        self.assertEqual(found, {"request": "start codex in a split to the right",
                                 "calls": [call("start_codex", place="right")], "source": "walk"})
        self.assertEqual(len(model.questions("Which operation")), 1)
        unsure = Model(COMMAND, ("Which operation", "start_codex", 0.45), YES)
        self.assertIsNone(suggest.suggest(request, unsure, plan=suggest.walk))
        # The walk's ranking needs WALK_CHECK, not the yes/no check's VERIFY.
        request = "In ~/x: start claude, then wait for codex"
        plan = [call("start_claude", directory="~/x"), call("wait_for_agent", agent="codex", directory="~/x")]
        for odds, offered in ((suggest.WALK_CHECK + 0.05, True), (suggest.WALK_CHECK - 0.05, False)):
            ranked = Model(COMMAND, ("Which operation does the request ask for first", "start_claude"),
                           ("What does the request ask for next", "wait_for_agent"), ("Which agent", "codex"),
                           ("Which directory", "1"), ("Which plan does exactly", plan_key(plan), odds))
            found = suggest.suggest(request, ranked, plan=suggest.walk)
            self.assertEqual(bool(found), offered)

    def test_a_request_the_grammar_reads_is_never_sent_to_the_model(self):
        model = Model()
        self.assertIsNone(suggest.suggest("start codex in a split to the right", model))
        self.assertIsNone(suggest.suggest("", model))
        self.assertIsNone(suggest.suggest("start codex " + "x" * 2048, model))
        self.assertEqual(model.asked, [])

    @given_plans()
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
        self.assertEqual(suggest.walk(request, model)[1][-1]["pick"], "model")      # the walk plans nothing for it
        self.assertIsNone(suggest.suggest(request, model))
        self.assertIsNone(suggest.suggest(request, model, plan=suggest.walk))
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
    "Claude in ./alpha, codex in ./beta, a notepad", "Codex in a split on the right, please.",
    "Codex in a new tab, please",
    'Run codex in dusk-ferry and tell it "lint only"', "Fire up Codex in mill-pond, then Claude in the same directory",
    "The calculator and the notepad, please", "Claude and codex in ./tide-pool, please",
    'Claude gets "hi", and hang on for codex in /x/y', 'Claude in "plum harbor", model opus, task "open the log"',
    '~/papers/fern\u00e9.pdf in the pdf viewer please, then wait till claude is done', "Let Codex know, the tests pass now",
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
        self.assertTrue(all(sizes) and max(sizes) > 3, sizes)
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
        # "Run" asks for a start of its own, so the message comes after it.
        self.assertEqual(suggest.plans('Run codex in dusk-ferry and tell it "lint only"'),
                         [[call("start_codex", directory="dusk-ferry"),
                           call("message_agent", agent="codex", directory="dusk-ferry", text="lint only")]])

    def test_a_plan_the_grammar_does_not_read_back_whole_is_not_in_the_tree(self):
        carried = suggest._carried
        with mock.patch.object(suggest, "_carried", lambda calls: carried(calls) if len(calls) < 2 else None):
            self.assertEqual(suggest.plans("The calculator and the notepad, please"), [])
        self.assertTrue(suggest.plans("The calculator and the notepad, please"))

    def test_an_operation_must_add_something_the_request_names(self):
        context = suggest._context('Run codex in dusk-ferry and tell it "lint only"')
        start = call("start_codex", directory="dusk-ferry")
        told = call("message_agent", agent="codex", directory="dusk-ferry", text="lint only")
        self.assertFalse(suggest._gains(context, [start, told],
                                        call("wait_for_agent", agent="codex", directory="dusk-ferry")))
        self.assertTrue(suggest._gains(context, [start], call("message_agent", agent="codex", text="lint only")))
        self.assertTrue(suggest._gains(context, [], start))
        twice = suggest._context("Fire up Codex in mill-pond, then Claude in the same directory")
        self.assertTrue(suggest._gains(twice, [call("start_codex", directory="mill-pond")],
                                       call("start_claude", directory="mill-pond")))

    def test_what_the_requests_words_rule_in_and_out(self):
        tab = suggest.plans("Codex in a new tab, please")
        self.assertNotIn("place", json.dumps(tab))         # "a new tab" says no split
        self.assertTrue(any(c["arguments"].get("place") == "down"
                            for p in suggest.plans("Bring up Codex in a split pane below.") for c in p))
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

    @given_plans()
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
        notepad = call("open_app", app="kilix-notepad")
        found = [[call(first, **({"agent": "claude"} if first == "wait_for_agent" else {}), directory=one),
                  call(second, **({"agent": "codex"} if second == "wait_for_agent" else {}), directory=two), notepad]
                 for one, two in (("./alpha", "./beta"), ("./beta", "./alpha"))
                 for first, second in (("start_claude", "wait_for_agent"), ("wait_for_agent", "start_codex"),
                                       ("wait_for_agent", "wait_for_agent"))]
        wanted = 'wait for claude in "./alpha"; then wait for codex in "./beta"; then open the notepad'
        with mock.patch.object(suggest, "WHOLE_GROUP", 2), given_plans(request, found):
            model = builds(wanted)
            calls, _steps = suggest.build(request, model)
        self.assertEqual(suggest.sentence(calls), wanted)
        groups = [options for question, options, _ in model.asked if question == suggest.WHOLE_QUESTION]
        self.assertTrue(len(found) > 2 and len(groups) == len(found) - 1, (len(found), len(groups)))
        self.assertTrue(all(len(options) <= 2 for options in groups))



def worded(request):
    return [suggest.sentence(calls) for calls in suggest.plans(request)]


class BuiltRules(unittest.TestCase):
    """The stricter rules plans() holds a request the grammar leaves unread to: a request whose words leave
    no plan gets none, and a plan takes the operations, order and details the request says."""

    def test_a_request_whose_words_leave_no_plan_gets_none(self):
        cases = {
            'Tell codex in pane 9: "lint only"': "unsaid",            # an address no wording says
            "Start codex in ./quill-moss. Model: opus.": "unsaid",       # a field label, not a text
            "Start codex in ./quill-moss with the task": "unsaid",       # a task with no text
            "Start codex in ./quill-moss in a split": "unsaid",          # a split with no direction
            "Bring up claude in a pane in ./quill-moss": "unsaid",
            "Open draft.txt in the notepad": "unsaid",                   # a file no plan carries
            "is claude running in ./quill-moss": "question",
            'start codex in ./quill-moss: "would it pick the parent?"': "question",
            "Start codex in ./quill-moss, then delete the build folder": "unsupported",
            "Start codex in ./quill-moss as root": "unsupported",
            "I need four codex sessions in ./quill-moss": "unsupported",
            "Don't start codex in ./quill-moss": "not now",
            "Suppose I start codex in ./quill-moss": "not now",
            "At 7pm, start codex in ./quill-moss": "not now",
            "Start codex in ./quill-moss in an hour": "not now",
            "Codex in ./quill-moss and aider in ./reed-lark": "unnamed",
            "Start codex in that folder we talked about": "unnamed",
            "Start codex in ./quill-moss and claude in ./reed-lark, then wait for it": "ambiguous",
            "Let claude in ./quill-moss know:": "incomplete",
            "Start codex in ./quill-moss; second,": "incomplete",
        }
        for request, why in cases.items():
            with self.subTest(request=request):
                self.assertTrue(suggest.possible(request))
                self.assertEqual(suggest._ruled_out(suggest._context(request)), why)
                self.assertEqual(suggest.plans(request), [])
        # Requests of the same shape without such a word have a plan.
        for request in ("Let codex in ./quill-moss know: lint only, please",
                        "Codex in ./quill-moss, a split below, please",
                        "Codex in ./quill-moss, and claude in ./reed-lark please"):
            with self.subTest(request=request):
                self.assertIsNone(suggest._ruled_out(suggest._context(request)))
                self.assertTrue(suggest.plans(request))
        self.assertEqual(worded("Let codex in ./quill-moss know: lint only, please"),
                         ['message codex in "./quill-moss": "lint only, please"'])

    def test_a_detail_no_wording_says_means_no_plan(self):
        for request in ("resume claude in ./quill-moss", "Start codex in ./quill-moss with high effort",
                        "Start claude in ./quill-moss, opus model",
                        "Start claude with model gpt-5.5-mint in ./quill-moss"):
            with self.subTest(request=request):
                self.assertEqual(suggest.plans(request), [])
        self.assertTrue(suggest._unsaid(suggest._context("resume claude in ./quill-moss")))
        self.assertFalse(suggest._unsaid(suggest._context("start claude in ./quill-moss")))

    def test_each_operation_verb_asks_for_an_operation_of_its_kind(self):
        self.assertEqual(worded('Start codex in ./quill-moss, wait for it, then tell it "ship it"'),
                         ['start codex in "./quill-moss"; then wait for codex in "./quill-moss"; '
                          'then message codex in "./quill-moss": "ship it"'])
        self.assertEqual(worded("Wait for codex, then for claude"), ["wait for codex; then wait for claude"])
        context = suggest._context('Start codex in ./quill-moss, wait for it, then tell it "ship it"')
        self.assertEqual([count for _ops, count in context["counts"]], [1, 1, 1])
        told = suggest._context('Send the message "hi" to claude')     # "the message" is a noun, not a verb
        self.assertEqual(dict(told["counts"])[("message_agent",)], 1)

    def test_a_plan_keeps_the_order_the_request_states(self):
        request = 'start codex here, wait for it to finish, then tell it "write the docs"'
        self.assertEqual(worded(request), ['start codex; then wait for codex; then message codex: "write the docs"'])
        context = suggest._context(request)
        start, wait = call("start_codex"), call("wait_for_agent", agent="codex")
        told = call("message_agent", agent="codex", text="write the docs")
        self.assertTrue(suggest._stated(context, [start, wait, told]))
        self.assertFalse(suggest._stated(context, [start, told, wait]))
        self.assertFalse(suggest._stated(context, [wait, start, told]))
        self.assertEqual(worded("1) start codex in ./quill-moss 2) wait for it"),
                         ['start codex in "./quill-moss"; then wait for codex in "./quill-moss"'])

    def test_each_call_takes_what_its_own_clause_says(self):
        cases = {
            "Start codex here and in ./quill-moss": 'start codex; then start codex in "./quill-moss"',
            "Start codex in ./quill-moss. Wait for it. Start claude in the same directory.":
                'start codex in "./quill-moss"; then wait for codex in "./quill-moss"; '
                'then start claude in "./quill-moss"',
            "Claude on the left and codex on the right, both in ./quill-moss":
                'start claude in "./quill-moss" in a split to the left; '
                'then start codex in "./quill-moss" in a split to the right',
            'Tell claude in ./quill-moss "done" and codex in ./quill-moss "also done"':
                'message claude in "./quill-moss": "done"; then message codex in "./quill-moss": "also done"',
            "Wait for claude in ./quill-moss and codex in ./quill-moss/web.":
                'wait for claude in "./quill-moss"; then wait for codex in "./quill-moss/web"',
            "Start claude and codex in ./quill-moss, then wait for both":
                'start claude in "./quill-moss"; then start codex in "./quill-moss"; '
                'then wait for claude in "./quill-moss"; then wait for codex in "./quill-moss"',
            "In ./quill-moss, start codex and claude":
                'start codex in "./quill-moss"; then start claude in "./quill-moss"',
        }
        for request, wording in cases.items():
            with self.subTest(request=request):
                self.assertEqual(worded(request), [wording])
        # "Claude and codex in ./x" shares the directory and the kind of operation, whichever that is.
        self.assertEqual(worded("Claude and codex in ./quill-moss, please"),
                         ['start claude in "./quill-moss"; then start codex in "./quill-moss"',
                          'wait for claude in "./quill-moss"; then wait for codex in "./quill-moss"'])

    def test_a_plan_uses_every_directory_and_text_the_request_copies(self):
        context = suggest._context('Start codex in ./quill-moss and tell it "fix the parser"')
        start = call("start_codex", directory="./quill-moss")
        self.assertFalse(suggest._keeps(context, [start]))
        self.assertTrue(suggest._keeps(context, [start, call("message_agent", agent="codex", text="fix the parser")]))

    def test_the_calls_for_one_agent_name_its_directory_alike(self):
        start = call("start_claude", directory="./quill-moss")
        told = call("message_agent", agent="claude", directory="./quill-moss", text="use the test config")
        bare = call("message_agent", agent="claude", text="now the real one")
        waited = call("wait_for_agent", agent="claude", directory="./quill-moss")
        self.assertTrue(suggest._consistent([start, told, waited]))
        self.assertFalse(suggest._consistent([start, told, call("wait_for_agent", agent="claude"), bare]))
        self.assertTrue(suggest._consistent([call("wait_for_agent", agent="claude", directory="./quill-moss"),
                                             call("wait_for_agent", agent="claude", directory="./reed-lark")]))
        # When the one plan that names it alike is refused (a repeated message), there is no plan at all.
        self.assertEqual(suggest.plans('Start claude in ./quill-moss; tell it "use the test config"; wait for it; '
                                       'tell it "now the real one".'), [])

    def test_copied_values_are_found_whole(self):
        request = 'Wait for claude in ./quill-moss and codex in ./quill-moss/web. Tell it "also done"'
        self.assertEqual(len(suggest._occurs(request, "./quill-moss")), 1)
        self.assertEqual(len(suggest._occurs(request, "./quill-moss/web")), 1)
        self.assertEqual(suggest._occurs(request, "done", ["done", "also done"]), [])

    def test_a_search_that_grows_too_large_gives_no_plan(self):
        request = "Start codex in ./quill-moss. Start claude in ./reed-lark."
        self.assertTrue(suggest.plans(request))
        with mock.patch.object(suggest, "MOST_STEPS", 3):
            self.assertEqual(suggest.plans(request), [])


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
        with given_plans():
            self.assertIsNone(suggest.suggest(SPLIT, split, propose=propose))
            self.assertTrue(split.asked)
        propose.assert_not_called()

    def test_a_planned_request_is_held_to_the_rules_too(self):
        request = "Quit Codex in a split on the right."
        self.assertTrue(suggest.possible(request))
        model = Model(*self.WALK)
        self.assertEqual(suggest.build(request, model)[0], [])     # no plan passes refusal(), so none is built
        self.assertEqual(model.asked, [])
        walked = (COMMAND, ("Which operation", "start_codex"), YES)
        calls, steps = suggest.walk(request, Model(*walked))
        self.assertEqual((calls, steps[-1]["pick"]), ([], "rules"))        # the walk holds its plan to them too
        self.assertEqual(suggest.refusal(request, self.PLAN), "unsupported")
        self.assertIsNone(suggest.suggest(request, Model(*walked)))
        self.assertIsNone(suggest.suggest(request, Model(*walked), plan=suggest.walk))
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
        with given_plans():
            self.assertIsNone(ask.suggester(session)(SPLIT, record))
            answers["Which command"] = "6"
            self.assertIsNone(ask.suggester(session)(SPLIT, record))    # both "wait for codex": not the proposal

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


class PaneTargetWording(unittest.TestCase):
    """The grammar's own sentence says a pane target and reads back as it;
    a pane number or broker id is a target span, never a name or model."""
    BROKER = '0c9e1f2a3b4d5e6f'

    def test_sentence_reads_back_as_the_same_pane_plan(self):
        for calls in ([call('message_agent', pane='9', text='run it')],
                      [call('message_agent', agent='codex', pane='9', broker=self.BROKER, text='run it')],
                      [call('wait_for_agent', pane='31'), call('message_agent', pane='31', text='next batch')],
                      [call('wait_for_agent', agent='grok', pane='4')],
                      [call('start_codex', directory='/tmp/sandpiper-bay'),
                       call('message_agent', agent='claude', pane='5', text='x')]):
            with self.subTest(calls=calls):
                carried = suggest._carried(calls)
                self.assertIsNotNone(carried, suggest.sentence(calls))
                self.assertEqual(carried['calls'], calls)

    def test_plan_wording_names_the_pane(self):
        self.assertEqual(suggest.describe(call('message_agent', agent='codex', pane='9', broker=self.BROKER, text='x')),
                         f'send the existing Codex session in pane 9 with broker {self.BROKER} the message "x"')
        self.assertEqual(suggest.describe(call('wait_for_agent', pane='9')),
                         'wait for the existing coding-agent session in pane 9 to finish its turn')
        self.assertEqual(suggest.describe(call('wait_for_agent', agent='claude', directory='/w')),
                         'wait for the existing Claude session in /w to finish its turn')

    def test_spans_copy_the_target_and_offer_no_broker_as_a_model(self):
        request = f'tell pane 32 on broker {self.BROKER}: rerun with gpt-6 later'
        paths, texts, models, _masked, plain = suggest.spans(request)
        self.assertEqual((paths, texts, models), ([], ['rerun with gpt-6 later'], []))
        self.assertNotIn('32', plain)
        self.assertEqual(suggest.targets(request), [{'pane': '32', 'broker': self.BROKER}])
        self.assertEqual(suggest.targets('tell "pane 4" to codex here; and pane 7'), [{'pane': '7', 'broker': None}])
        self.assertEqual(suggest.targets('tell pane 7 broker 0C9E1F2A3B4D5E6F: x'), [{'pane': '7', 'broker': None}])

    def test_refusal_knows_a_named_pane(self):
        request = "message pane 59 'hello'"
        self.assertIsNone(suggest.refusal(request, [call('message_agent', pane='59', text='hello')]))
        self.assertEqual(suggest.refusal(request, [call('message_agent', pane='58', text='hello')]), 'reworded')
        self.assertEqual(suggest.refusal("message pane 59 and pane 60 'hello'",
                                         [call('message_agent', pane='59', text='hello')]), 'unused')
        self.assertEqual(suggest.refusal(f"message pane 59 broker {self.BROKER} 'hello'",
                                         [call('message_agent', pane='59', text='hello')]), 'unused')
        self.assertEqual(suggest.refusal("message my other pane 'hello'",
                                         [call('message_agent', pane='59', text='hello')]), 'unnamed')
