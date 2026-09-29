"""Regression tests for ordered effects and object-bound settings.

Resolution and execution are mocked; these tests never touch a live terminal.
"""
import unittest
from unittest import mock

import support  # noqa: F401
import apps
import apps_kilix
import mcp_server
import needle_cli


def admitted(request):
    return [(a.kind, a.args) for a in apps.interpret(request, apps.propose(request))
            if isinstance(a, apps.Action)]


class RequestOrder(unittest.TestCase):
    def test_unrelated_earlier_launch_does_not_move_a_later_launch_before_enable(self):
        expected = [("launch", {"app": "kilix-calculator"}),
                    ("game", {"game": "kilix-pong", "available": True}),
                    ("launch", {"app": "kilix-pong"})]
        for ending in ("launch pong", "then launch it"):
            with self.subTest(ending=ending):
                self.assertEqual(admitted("open calculator, enable pong, " + ending), expected)

    def test_later_reference_does_not_move_across_an_intervening_action(self):
        self.assertEqual(admitted("enable doom, open calculator, launch doom"), [
            ("game", {"game": "doom", "available": True}),
            ("launch", {"app": "kilix-calculator"}),
            ("launch", {"app": "doom"})])

    def test_explicit_launch_before_enable_stays_in_that_order(self):
        self.assertEqual(admitted("open calculator, launch pong, enable pong"), [
            ("launch", {"app": "kilix-calculator"}),
            ("launch", {"app": "kilix-pong"}),
            ("game", {"game": "kilix-pong", "available": True})])

    def test_enable_is_performed_before_launch_readiness_in_cli_and_mcp(self):
        request = "open calculator, enable pong, launch pong"
        for transport in ("cli", "mcp"):
            with self.subTest(transport=transport):
                enabled = False
                performed = []

                def resolve(action):
                    ready = (action.kind != "launch" or
                             action.args["app"] != "kilix-pong" or enabled)
                    return apps_kilix.Step(action, str(action), ready=ready)

                def perform(step, *, install):
                    nonlocal enabled
                    self.assertFalse(install)
                    performed.append((step.action.kind, step.action.args))
                    if step.action.kind == "game":
                        enabled = step.action.args["available"]

                runtime = needle_cli.Runtime(apps.Proposer(), [], label="grammar")
                with mock.patch.object(apps_kilix, "resolve", side_effect=resolve), \
                        mock.patch.object(apps_kilix, "perform", side_effect=perform):
                    if transport == "cli":
                        record = needle_cli.run_apps_request(
                            runtime, request, needle_cli.Options(assume_yes=True, agent=True))
                    else:
                        server = mcp_server.Server(lambda job="panes": runtime)
                        try:
                            record = server.call_tool("kilix_apps_act", {
                                "request": request, "confirm_risky": True})["structuredContent"]
                        finally:
                            server.close()
                self.assertEqual(record["status"], 0, record)
                self.assertEqual([x["outcome"] for x in record["items"]], ["done"] * 3)
                self.assertEqual(performed, [
                    ("launch", {"app": "kilix-calculator"}),
                    ("game", {"game": "kilix-pong", "available": True}),
                    ("launch", {"app": "kilix-pong"})])


class ItemObjects(unittest.TestCase):
    def test_temporal_and_date_qualifiers_do_not_name_widgets(self):
        cases = [
            ("I want memory usage visible on panes all of the time", "clock"),
            ("show cpu on panes from time to time", "clock"),
            ("show cpu on panes most of the time", "clock"),
            ("I want the memory stats on panes up to date", "calendar"),
        ]
        for request, item in cases:
            with self.subTest(request=request):
                self.assertFalse(any(k == "show" and a["item"] == item
                                     for k, a in admitted(request)))

    def test_memory_setting_is_preserved_when_time_idiom_is_ignored(self):
        self.assertEqual(admitted("I want memory usage visible on panes all of the time"), [
            ("pane_stat", {"stat": "memory", "mode": "always"})])

    def test_nouns_qualified_by_item_words_are_not_the_widgets(self):
        for request in ("hide temp files", "hide network traffic"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [])
        self.assertEqual(admitted("show the windows close button"), [
            ("show", {"item": "close", "on": True})])

    def test_explicit_widgets_with_time_qualifiers_remain_supported(self):
        for request, item in (("show the time", "clock"), ("show the date", "calendar"),
                              ("show the time on the top bar at all times", "clock"),
                              ("show the calendar up to date", "calendar")):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [("show", {"item": item, "on": True})])

    def test_widget_location_does_not_allow_arbitrary_battery_objects(self):
        self.assertEqual(admitted("get rid of the battery thing up top"), [
            ("show", {"item": "battery", "on": False})])
        self.assertEqual(admitted("hide the battery compartment"), [])

    def test_full_font_size_button_names_remain_supported(self):
        for verb, item in (("increase", "font_increase"), ("decrease", "font_decrease")):
            with self.subTest(verb=verb):
                self.assertEqual(admitted(f"hide the {verb} font size button"), [
                    ("show", {"item": item, "on": False})])

    def test_widget_nouns_and_one_references_remain_supported(self):
        self.assertEqual(admitted("hide the dictation button but show the read aloud one"), [
            ("show", {"item": "dictate", "on": False}),
            ("show", {"item": "speak", "on": True})])
        for noun in ("control", "toggle", "symbol", "badge", "label"):
            with self.subTest(noun=noun):
                self.assertEqual(admitted(f"show the battery {noun}"), [
                    ("show", {"item": "battery", "on": True})])


class GameWishes(unittest.TestCase):
    def test_an_ordinary_wish_does_not_change_game_availability(self):
        for request in ("I want mines", "I need doom", "I would like doom",
                        "I don't need games like doom", "I don't want games like mines; open mines",
                        "I want doom and pong", "I don't need doom and pong",
                        "I don't want doom for my shopping list"):
            with self.subTest(request=request):
                self.assertFalse(any(k == "game" for k, _ in admitted(request)))

    def test_explicit_games_list_and_availability_controls_remain_supported(self):
        for request, game, available in (
            ("I don't need pong in my game picker anymore", "kilix-pong", False),
            ("I don't want doom in the games list", "doom", False),
            ("I want doom in the games list", "doom", True),
            ("enable pong", "kilix-pong", True),
            ("disable doom", "doom", False),
        ):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [
                    ("game", {"game": game, "available": available})])

    def test_bare_continuations_inherit_explicit_list_scope(self):
        self.assertEqual(admitted("I want doom in the games list and pong"), [
            ("game", {"game": "doom", "available": True}),
            ("game", {"game": "kilix-pong", "available": True})])


class Confirmation(unittest.TestCase):
    def test_partial_dry_run_explains_the_unaccounted_operation(self):
        runtime = needle_cli.Runtime(apps.Proposer(), [], label="grammar")
        with mock.patch.object(apps_kilix, "resolve", side_effect=lambda a: apps_kilix.Step(a, str(a))), \
                mock.patch.object(apps_kilix, "perform") as perform:
            record = needle_cli.run_apps_request(
                runtime, "disable doom and open doom", needle_cli.Options(dry_run=True))
        perform.assert_not_called()
        self.assertIn("open doom", record["note"])
        self.assertIn("confirmation", record["note"])

    def test_unaccounted_launch_never_waives_confirmation_of_a_disable(self):
        request = "disable doom and open doom"
        runtime = needle_cli.Runtime(apps.Proposer(), [], label="grammar")
        with mock.patch.object(apps_kilix, "resolve", side_effect=lambda a: apps_kilix.Step(a, str(a))), \
                mock.patch.object(apps_kilix, "perform") as perform:
            record = needle_cli.run_apps_request(
                runtime, request, needle_cli.Options(assume_yes=True, agent=True))
        perform.assert_not_called()
        self.assertNotEqual(record["status"], 0)


if __name__ == "__main__":
    unittest.main()


class Round2(unittest.TestCase):
    """Review R16 round 2: the whole object, continued wishes, "control"."""

    def test_an_item_name_is_the_whole_object_or_none(self):              # KN-R16-202
        for request in ("hide the clock icon files", "hide network status page",
                        "hide the battery percentage spreadsheet", "hide the clock family",
                        "hide the close button pictures", "show the windows close button files",
                        "hide the clock 2 files", "hide the clock /files"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [])
        for request, want in (("show the battery control", ("show", {"item": "battery", "on": True})),
                              ("hide the clock completely", ("show", {"item": "clock", "on": False})),
                              ("hide the clock, please", ("show", {"item": "clock", "on": False})),
                              ("get rid of the battery thing up top",
                               ("show", {"item": "battery", "on": False}))):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [want])

    def test_wishes_change_no_games_list_unless_they_say_so(self):        # KN-R16-201
        for request in ("I don't need doom and pong", "I want mines", "I need doom",
                        "I would like doom", "I want doom and pong",
                        "I don't want doom for my shopping list",
                        "I don't want games like doom and pong"):
            with self.subTest(request=request):
                self.assertFalse([a for a in admitted(request) if a[0] == "game"])


class Round3(unittest.TestCase):
    """Review R16 round 3: file names, groups, described objects, places, list scope."""

    def test_described_or_filed_names_are_no_widgets(self):    # KN-R16-301, -302, -303
        for request in ("hide clock.png", "hide network.status", "hide the clock icon.svg",
                        "hide clock#files", "hide clock icon.files",
                        "hide the split buttons pictures", "hide font size buttons files",
                        "hide pictures of the clock", "hide files about the clock",
                        "hide pictures of clock, battery and network",
                        "hide the clock icon on the poster",
                        "hide the clock icon from the screenshot"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [])
        self.assertEqual(admitted("hide clock, files of the battery"),
                         [("show", {"item": "clock", "on": False})])

    def test_names_and_places_that_are_widgets_remain(self):
        for request, n in (("hide clock.", 1), ("hide clock, battery", 2), ("hide clock, please", 1),
                           ("hide the split buttons", 4), ("hide font size buttons", 2),
                           ("get rid of the battery thing up top", 1),
                           ("take the thermals icon out of the status bar", 1),
                           ("restore the wi-fi icon in the bar at the top", 1),
                           ("hide the windows icon from the status bar for me", 1),
                           ("turn the split left icon on in the top bar", 1),
                           ("take the dictation button off the pane controls", 1),
                           ("show the time on the top bar at all times", 1)):
            with self.subTest(request=request):
                self.assertEqual(len(admitted(request)), n)

    def test_a_list_named_after_a_coordinated_wish_scopes_all_of_it(self):  # KN-R16-304
        for request in ("I want doom and pong in the games list",
                        "I want doom in the games list and pong"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [
                    ("game", {"game": "doom", "available": True}),
                    ("game", {"game": "kilix-pong", "available": True})])
        self.assertEqual(admitted("I want doom and pong"), [])


class Round4(unittest.TestCase):
    """Review R16 round 4: list scope from games only; places."""

    def test_an_unrelated_continuation_lends_no_list_scope(self):         # KN-R16-401
        for request in ("I want doom, and the games list screenshot",
                        "I want doom and a screenshot of the games list",
                        "I need doom and a screenshot of the games list"):
            with self.subTest(request=request):
                self.assertFalse([a for a in admitted(request) if a[0] == "game"])
        self.assertEqual(len(admitted("I don't want doom and pong in the games list")), 2)

    def test_places_take_possessives_directions_and_a_closing_word(self):  # KN-R16-402
        for request in ("hide the clock on our top bar", "hide the clock in the upper right corner",
                        "hide the clock from the top right corner",
                        "hide the clock from the top bar completely",
                        "hide the clock on the top bar permanently"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [("show", {"item": "clock", "on": False})])

    def test_place_words_do_not_run_together(self):                        # KN-R16-404
        for request in ("hide the clock in the thememenu", "hide the clock on terminalstatus"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [])


class Round5(unittest.TestCase):
    """Review R16 round 5: game objects in wishes; a place must remain."""

    def test_a_game_named_inside_another_object_is_no_game_object(self):   # KN-R16-501
        for request in ("I want doom and the pong games list screenshot",
                        "I want doom and pong's games list screenshot",
                        "I want doom and pictures of pong in the games list"):
            with self.subTest(request=request):
                self.assertFalse([a for a in admitted(request) if a[0] == "game"])
        for request in ("I want doom and pong in the games list",
                        "I want doom in the games list and pong",
                        "enable doom and pong in the games list", "disable doom and pong"):
            with self.subTest(request=request):
                self.assertEqual(len(admitted(request)), 2)

    def test_a_tail_never_stands_for_a_place(self):                         # KN-R16-502
        for request in ("hide clock in permanently", "hide clock in completely permanently",
                        "hide clock in it", "hide clock in that", "hide clock in from",
                        "hide clock in completely, battery"):
            with self.subTest(request=request):
                self.assertEqual(admitted(request), [])
        for request in ("turn the clock on", "take the clock off",
                        "hide the clock from the top bar completely",
                        "hide the clock on the top bar now"):
            with self.subTest(request=request):
                self.assertEqual(len(admitted(request)), 1)


class Round6(unittest.TestCase):
    """Review R16 round 6: the continuation-admission guard on its own."""

    def test_a_non_game_continuation_changes_no_game_when_scope_is_valid(self):  # KN-R16-602
        self.assertEqual(admitted("I want doom in the games list and a picture of pong"), [
            ("game", {"game": "doom", "available": True})])
