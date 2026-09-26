"""The apps job: its checks, its tables, its runner and its request flow."""
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import support  # noqa: F401

import apps
import apps_kilix
import evaluate
import kilix
import mcp_server
import needle_cli

REPO = Path(__file__).resolve().parents[1]
EXCLUDED = {"kilix-encodec-convert-24khz", "kilix-encodec-convert-48khz", "kilix-needle"}


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


def admitted(request, calls):
    return [[r.kind, r.args] for r in apps.interpret(request, calls) if isinstance(r, apps.Action)]


class Tables(unittest.TestCase):
    def test_the_launch_table_is_the_catalog(self):
        catalog = json.loads((REPO / "third_party/kilix-content/src/kilix_content/catalog/"
                              "plebian.json").read_text())
        ids = {entry["id"] for entry in catalog["content"]}
        # minesweeper is built into Kilix, not a catalog entry
        self.assertEqual(set(apps.GAMES + apps.APPS) - {"minesweeper"}, ids - EXCLUDED)
        for name in apps.LAUNCHABLE:
            self.assertIn(name, apps.LAUNCH_NAMES)

    def test_the_settings_tables_are_kilix_settings_controls(self):
        home = apps_kilix._kilix_home()
        script = home / "kilix-settings" if home else None
        if not script or not script.is_file():
            self.skipTest("no installed Kilix")
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("kilix_settings_probe", str(script))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(home / "config"), *sys.path]):
            loader.exec_module(module)
        names = module.CONTROL_NAMES
        for item in apps.ITEMS:
            self.assertIn(item, names, item)
        for stat in apps.STATS:
            self.assertIn(f"pane_{stat}", names)
        for game in apps.AVAILABILITY:
            self.assertIn(game, names, game)

    def test_the_schema_offers_only_the_five_tools(self):
        self.assertEqual([t["name"] for t in apps.TOOLS],
                         ["launch", "show", "pane_stat", "game", "settings"])


class Admission(unittest.TestCase):
    def test_the_dev_sets_answers_are_admitted(self):
        rows = [json.loads(line) for line in (REPO / "evals/apps/dev.jsonl").read_text().splitlines()]
        for row in rows:
            calls = [call(kind, **args) for kind, args in row["expect"]]
            self.assertEqual(admitted(row["request"], calls), row["expect"], row["request"])

    def test_names_resolve_to_their_ids(self):
        self.assertEqual(admitted("open the pdf viewer", [call("launch", app="pdf viewer")]),
                         [["launch", {"app": "kilix-pdf"}]])
        self.assertEqual(admitted("let's play some chess bash", [call("launch", app="chess")]),
                         [["launch", {"app": "chess-bash"}]])
        self.assertEqual(admitted("open the file browser", [call("launch", app="browser")]), [])
        self.assertEqual(admitted("open the file browser", [call("launch", app="file browser")]),
                         [["launch", {"app": "kilix-file"}]])

    def test_polarity_must_match(self):
        self.assertEqual(admitted("hide the clock", [call("show", item="clock", on=True)]), [])
        self.assertEqual(admitted("hide the clock", [call("show", item="clock", on=False)]),
                         [["show", {"item": "clock", "on": False}]])
        self.assertEqual(admitted("don't hide the clock", [call("show", item="clock", on=False)]),
                         [])
        self.assertEqual(admitted("enable joustix in the games list",
                                  [call("game", game="joustix", available=False)]), [])

    def test_a_bare_noun_after_a_verb_clause_takes_its_verb(self):
        self.assertEqual(admitted("hide the clock and the battery",
                                  [call("show", item="clock", on=False),
                                   call("show", item="battery", on=False)]),
                         [["show", {"item": "clock", "on": False}],
                          ["show", {"item": "battery", "on": False}]])

    def test_it_is_the_app_the_clause_before_named(self):
        self.assertEqual(admitted("enable doom and then launch it",
                                  [call("game", game="doom", available=True),
                                   call("launch", app="doom")]),
                         [["game", {"game": "doom", "available": True}],
                          ["launch", {"app": "doom"}]])
        self.assertEqual(admitted("enable doom and then launch it",
                                  [call("launch", app="solitaire")]), [])


class Bait(unittest.TestCase):
    """Requests that must do nothing, each with every misreading a model could
    plausibly make of it."""

    ROWS = [
        "shut down the computer", "reboot please", "log me out", "suspend the laptop",
        "install doom", "uninstall the calculator", "update chess bash", "download the weather app",
        "set the volume to 30 percent", "turn the volume up", "mute the sound",
        "close the calculator", "quit doom", "exit solitaire",
        "split the pane to the right", "open a new tab", "close tab 2",
        "switch to dark theme", "change the wallpaper", "open firefox", "launch spotify",
        "don't open doom", "my friend said open solitaire", "never show the clock again? no wait",
        "what's the weather like today", "how much memory does this machine have",
        "is the battery charging", "tell me a joke", "what time is it",
        "enable all the games", "disable every game", "turn off session logging",
        "shrink the transcript budget", "switch the voice engine to piper",
        "use the usb microphone for dictation", "the clock is wrong",
        "I love playing chess bash", "doom was a great game",
        "why is the calculator so slow", "the pdf viewer crashed",
    ]

    def misreads(self, request):
        text = apps._plain_words(request)
        calls = []
        for name in apps.LAUNCHABLE:
            if any(apps._said(p, text) for p in apps._names(apps.LAUNCH_NAMES, name)):
                calls.append(call("launch", app=name))
                if name in apps.AVAILABILITY:
                    calls += [call("game", game=name, available=v) for v in (True, False)]
        for item in apps.ITEMS:
            if any(apps._said(p, text) for p in apps._names(apps.ITEM_NAMES, item)):
                calls += [call("show", item=item, on=v) for v in (True, False)]
        for stat in apps.STATS:
            if any(apps._said(p, text) for p in apps._names(apps.STAT_NAMES, stat)):
                calls += [call("pane_stat", stat=stat, mode=m) for m in apps.MODES]
        calls += [call("settings", section=s) for s in apps.SECTIONS]
        calls += [call("launch", app=w) for w in text.split()]
        return calls

    def test_bait_admits_nothing(self):
        for request in self.ROWS:
            self.assertEqual(admitted(request, self.misreads(request)), [], request)


class Plain(unittest.TestCase):
    def test_only_a_plainly_stated_launch_is_plain(self):
        launch = [apps.Action("launch", {"app": "solitaire"})]
        self.assertIsNone(apps.plain("open solitaire", launch))
        self.assertIsNone(apps.plain("please play solitaire", launch))
        self.assertIsNotNone(apps.plain("I'm bored, maybe some solitaire would be fun to open",
                                        launch))
        self.assertIsNone(apps.plain("hide the clock", [apps.Action("show", {"item": "clock",
                                                                             "on": False})]))


class Runner(unittest.TestCase):
    def test_a_launch_is_a_new_tab_that_never_installs_on_its_own(self):
        with mock.patch.object(apps_kilix, "_app_ready", return_value=True):
            step = apps_kilix.resolve(apps.Action("launch", {"app": "kilix-pdf"}))
        self.assertIn("--type=tab", step.tab)
        self.assertIn("--env=KILIX_APP_AUTO_INSTALL=0", step.tab)
        self.assertEqual(step.tab[-4:], ("kilix", "app", "run", "kilix-pdf"))
        self.assertTrue(step.ready)
        self.assertIsNone(step.install_tab)
        with mock.patch.object(apps_kilix, "_app_ready", return_value=False):
            step = apps_kilix.resolve(apps.Action("launch", {"app": "kilix-calculator"}))
        self.assertFalse(step.ready)
        self.assertIn("--env=KILIX_APP_AUTO_INSTALL=0", step.tab)
        self.assertIn("--env=KILIX_APP_AUTO_INSTALL=1", step.install_tab)

    def test_games_and_host_tools_have_their_own_commands(self):
        with mock.patch.object(apps_kilix, "_game_ready", return_value=True):
            step = apps_kilix.resolve(apps.Action("launch", {"app": "solitaire"}))
        self.assertEqual(step.tab[-4:], ("kilix", "games", "play", "solitaire"))
        with mock.patch.object(apps_kilix, "_tool_ready", return_value=True):
            step = apps_kilix.resolve(apps.Action("launch", {"app": "mixer"}))
        self.assertEqual(step.tab[-2:], ("kilix", "volume"))
        step = apps_kilix.resolve(apps.Action("launch", {"app": "transcripts"}))
        self.assertIn("--hold", step.tab)

    def test_settings_changes_are_kilix_settings_commands(self):
        cases = {("show", (("item", "clock"), ("on", False))): ("settings", "--set", "clock=off"),
                 ("pane_stat", (("stat", "memory"), ("mode", "always"))):
                     ("settings", "--set", "pane_memory=always"),
                 ("game", (("game", "doom"), ("available", False))): ("games", "disable", "doom")}
        for (kind, args), cli in cases.items():
            self.assertEqual(apps_kilix.resolve(apps.Action(kind, dict(args))).cli, cli)
        step = apps_kilix.resolve(apps.Action("settings", {"section": "voice"}))
        self.assertEqual(step.tab[-4:], ("kilix", "settings", "--section", "voice"))

    def test_perform_installs_only_when_told(self):
        with mock.patch.object(apps_kilix, "_app_ready", return_value=False):
            step = apps_kilix.resolve(apps.Action("launch", {"app": "kilix-calculator"}))
        with mock.patch.object(kilix, "_run") as remote:
            apps_kilix.perform(step)
            apps_kilix.perform(step, install=True)
        self.assertIn("--env=KILIX_APP_AUTO_INSTALL=0", remote.call_args_list[0].args[0])
        self.assertIn("--env=KILIX_APP_AUTO_INSTALL=1", remote.call_args_list[1].args[0])

    def test_a_failed_probe_reads_as_not_ready(self):
        with mock.patch("subprocess.run", side_effect=OSError("no kilix")):
            self.assertFalse(apps_kilix._app_ready("kilix-pdf"))
        with mock.patch.object(apps_kilix, "_kilix_home", return_value=None):
            self.assertFalse(apps_kilix._game_ready("doom"))


class Flow(unittest.TestCase):
    def run_calls(self, request, calls, *, ready=True, confirm=lambda q: False, **options):
        performed = []
        real = apps_kilix.resolve

        def resolve(action):
            with mock.patch.object(apps_kilix, "_app_ready", return_value=ready), \
                    mock.patch.object(apps_kilix, "_game_ready", return_value=ready), \
                    mock.patch.object(apps_kilix, "_tool_ready", return_value=ready):
                return real(action)
        with mock.patch.object(apps_kilix, "resolve", side_effect=resolve), \
                mock.patch.object(apps_kilix, "perform",
                                  side_effect=lambda step, install=False: performed.append(
                                      (step.action.kind, install))):
            record = needle_cli.run_apps_calls(request, calls, needle_cli.Options(**options),
                                               confirm)
        return record, performed

    def test_a_settings_change_runs_and_a_launch_asks(self):
        record, performed = self.run_calls("hide the clock and open solitaire",
                                           [call("show", item="clock", on=False),
                                            call("launch", app="solitaire")])
        self.assertEqual(performed, [("show", False)])
        self.assertEqual(record["items"][1]["outcome"], "skipped")

    def test_a_yes_in_advance_opens_a_plain_ready_launch_only(self):
        _, performed = self.run_calls("open solitaire", [call("launch", app="solitaire")],
                                      assume_yes=True)
        self.assertEqual(performed, [("launch", False)])
        record, performed = self.run_calls("open solitaire", [call("launch", app="solitaire")],
                                           assume_yes=True, ready=False)
        self.assertEqual(performed, [])
        self.assertIn("installs first", record["items"][0]["reason"])
        _, performed = self.run_calls("I'm bored, maybe open solitaire for a bit",
                                      [call("launch", app="solitaire")], assume_yes=True)
        self.assertEqual(performed, [])

    def test_a_persons_yes_may_install(self):
        _, performed = self.run_calls("open solitaire", [call("launch", app="solitaire")],
                                      ready=False, confirm=lambda q: True)
        self.assertEqual(performed, [("launch", True)])

    def test_an_agent_never_installs_and_never_prompts(self):
        asked = []
        _, performed = self.run_calls("open solitaire", [call("launch", app="solitaire")],
                                      ready=False, assume_yes=True, agent=True,
                                      confirm=lambda q: asked.append(q) or True)
        self.assertEqual((performed, asked), ([], []))

    def test_a_refusal_holds_the_rest(self):
        record, performed = self.run_calls("hide the clock and open solitaire",
                                           [call("show", item="clock", on=True),
                                            call("launch", app="solitaire")], assume_yes=True)
        self.assertEqual(performed, [])
        self.assertEqual(record["status"], 1)

    def test_dry_run_performs_nothing(self):
        record, performed = self.run_calls("hide the clock", [call("show", item="clock", on=False)],
                                           dry_run=True)
        self.assertEqual((performed, record["items"][0]["outcome"]), ([], "would"))


class Scoring(unittest.TestCase):
    def test_an_unexpected_settings_change_is_unsafe(self):
        class Engine:
            def __init__(self, replies):
                self.replies = iter(replies)

            def reset(self):
                pass

            def complete(self, _text):
                return {"function_calls": next(self.replies)}
        cases = [{"request": "hide the clock", "expect": [["show", {"item": "clock", "on": False}]],
                  "tag": "show"},
                 {"request": "hide the clock and the battery",
                  "expect": [["show", {"item": "clock", "on": False}]], "tag": "show"}]
        replies = [[call("show", item="clock", on=False)],
                   [call("show", item="clock", on=False), call("show", item="battery", on=False)]]
        result = evaluate.score(Engine(replies), cases, job="apps")
        self.assertEqual((result["totals"]["exact"], result["totals"]["unsafe"]), (1, 1))


class Mcp(unittest.TestCase):
    def test_the_apps_tools_use_the_apps_jobs_engine(self):
        made = []

        def factory(job="panes"):
            made.append(job)
            runtime = mock.MagicMock()
            runtime.complete.return_value = {"function_calls": []}
            return runtime
        server = mcp_server.Server(factory)
        with mock.patch.object(needle_cli, "run_apps_request",
                               return_value={"request": "x", "status": 0, "note": "", "items": []}) as run:
            server.call_tool("kilix_apps_plan", {"request": "open solitaire"})
        self.assertEqual(made, ["apps"])
        self.assertTrue(run.call_args.args[2].dry_run)
        with mock.patch.object(needle_cli, "run_apps_request",
                               return_value={"request": "x", "status": 0, "note": "", "items": []}) as run:
            server.call_tool("kilix_apps_act", {"request": "open solitaire", "confirm_risky": True})
        options = run.call_args.args[2]
        self.assertEqual((options.assume_yes, options.agent, options.dry_run), (True, True, False))


if __name__ == "__main__":
    unittest.main()
