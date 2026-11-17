"""The system bundle: reads only on ground it was trained on, and only what the checks admit."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401

import system_bundle as sb
import system_bundle_train as train
import system_dispatch
import system_job
import system_normalize

REPO = Path(__file__).resolve().parent.parent


def row(request, *calls):
    return {"request": request, "expect": [list(c) for c in calls], "tag": "t", "source": "test"}


def res(kind, **a):
    return ("resources", {"kind": kind, **a})


def journal(**a):
    return ("journal", a)


ROWS = [
    row("show memory usage", res("memory")), row("show disk usage", res("disk")),
    row("show cpu usage", res("cpu")), row("how much memory is free", res("memory")),
    row("how much disk is free", res("disk")), row("show disk usage on /var", res("disk", path="/var")),
    row("show disk usage on /home", res("disk", path="/home")),
    row("show memory usage please", res("memory")), row("show disk usage please", res("disk")),
    row("show nginx logs", journal(unit="nginx")), row("show ssh logs", journal(unit="ssh")),
    row("show cups logs", journal(unit="cups")), row("show cron logs", journal(unit="cron")),
    row("show cron logs since yesterday", journal(unit="cron", since="yesterday", boot="any")),
    row("show ssh logs since yesterday", journal(unit="ssh", since="yesterday", boot="any")),
    row("show nginx logs from the last 2 hours", journal(unit="nginx", since="2 hours ago", boot="any")),
    row("show cups logs from the last 3 hours", journal(unit="cups", since="3 hours ago", boot="any")),
    row("top 5 processes by memory", ("processes", {"sort": "memory", "limit": 5})),
    row("top 3 processes by memory", ("processes", {"sort": "memory", "limit": 3})),
    row("top 7 processes by cpu", ("processes", {"sort": "cpu", "limit": 7})),
    row("top processes by cpu", ("processes", {"sort": "cpu"})),
    row("is curl installed", ("packages", {"operation": "status", "target": "curl"})),
    row("is jq installed", ("packages", {"operation": "status", "target": "jq"})),
    row("is tmux installed", ("packages", {"operation": "status", "target": "tmux"})),
    row("show memory usage and disk usage", res("memory"), res("disk")),
    row("show cpu usage and memory usage", res("cpu"), res("memory")),
    row("restart nginx"), row("reboot the box"), row("delete my logs"),
]


def tiny(members=1, rows=ROWS):
    data = train.build(rows, seed=7, members=members, log=lambda *_: None)
    return sb.Bundle(json.loads(json.dumps(data)))


class Readers(unittest.TestCase):
    def test_times(self):
        cases = {"since yesterday": "yesterday", "logs today": "today", "since 2 hours ago": "2 hours ago",
                 "in the last hour": "1 hour ago", "the past 30 minutes": "30 minutes ago",
                 "last 45 min": "45 minutes ago", "over the past 3 days": "3 days ago",
                 "since 2026-09-01": "2026-09-01", "since 2026-09-01 10:30": "2026-09-01 10:30",
                 "since 1 hours ago": "1 hours ago", "the last two hours": "2 hours ago"}
        for text, want in cases.items():
            self.assertEqual(sb.read_since(sb.tokenize(text))[0], want, text)
        for text in ("since last week", "yesterday and today", "logs", "in the last few hours"):
            self.assertIsNone(sb.read_since(sb.tokenize(text))[0], text)

    def test_numbers_and_paths(self):
        self.assertEqual(sb.read_number(sb.tokenize("top five procs"), set()), 5)
        self.assertIsNone(sb.read_number(sb.tokenize("top 5 of 10"), set()))
        self.assertEqual(sb.read_path(sb.tokenize("disk on /var/lib?")), "/var/lib")
        self.assertIsNone(sb.read_path(sb.tokenize("/var and /srv")))

    def test_words(self):
        self.assertEqual(sb.tokenize("Is nginx.service up, and cpu?"), ["Is", "nginx.service", "up", ",", "and", "cpu"])
        self.assertEqual(sb.form("17", set()), "<num>")
        self.assertEqual(sb.form("/srv", set()), "<path>")
        self.assertEqual(sb.form("zzz", set()), "<unk>")


class Reading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = tiny()

    def read(self, text):
        return self.bundle.read(text)

    def test_reads_familiar_wording_with_new_names(self):
        self.assertEqual(self.read("show apache2 logs"), [["journal", {"boot": "current", "priority": "all",
                                                                       "scope": "system", "unit": "apache2"}]])
        self.assertEqual(self.read("show disk usage on /srv"), [["resources", {"kind": "disk", "path": "/srv"}]])
        self.assertEqual(self.read("is ripgrep installed"),
                         [["packages", {"operation": "status", "target": "ripgrep"}]])

    def test_reads_code_values(self):
        self.assertEqual(self.read("top 9 processes by cpu"), [["processes", {"sort": "cpu", "limit": 9}]])
        got = self.read("show redis logs from the last 6 hours")
        self.assertEqual(got[0][1]["since"], "6 hours ago")
        self.assertEqual(got[0][1]["unit"], "redis")

    def test_abstains_off_its_ground(self):
        for text in ("show apache2 logs quickly",       # an unknown word that is not the name
                     "logs show nginx",                 # a pair never seen in a journal read
                     "usage memory show",
                     "show nginx logs since last week",  # a time code cannot read
                     "top 5 of 10 processes by cpu",    # two numbers for one limit
                     "show memory usage and disk usage and cpu usage and memory usage",  # more clauses
                     "restart nginx", "delete my logs"):
            self.assertIsNone(self.read(text), text)

    def test_value_words_must_agree_with_the_reading(self):
        bundle = tiny()
        bundle.value_words.setdefault("resources", {}).setdefault("kind", {})["memory"] = "memory"
        calls, why = bundle._read(0, "show memory usage")
        self.assertEqual(calls[0][1]["kind"], "memory")
        bundle.value_words["resources"]["kind"]["usage"] = "disk"
        calls, why = bundle._read(0, "show memory usage")
        self.assertIsNone(calls)
        self.assertIn("another kind", why)

    def test_an_unknown_word_must_be_the_copied_name_even_where_the_frame_allows_it(self):
        class Everything(frozenset):
            def __contains__(self, _pair):
                return True
        bundle = tiny()
        self.assertIsNotNone(bundle._read(0, "show apache2 logs")[0])
        bundle.frames = {tool: Everything() for tool in sb.TOOLS}
        for text in ("show apache2 xyzzy logs", "show xyzzy apache2 logs", "xyzzy show apache2 logs"):
            calls, why = bundle._read(0, text)
            self.assertIsNone(calls, text)

    def test_no_more_clauses_than_training_had(self):
        bundle = tiny()
        self.assertIsNotNone(bundle._read(0, "show memory usage and disk usage")[0])
        bundle.max_clauses = 1
        calls, why = bundle._read(0, "show memory usage and disk usage")
        self.assertIsNone(calls)
        self.assertIn("more clauses", why)

    def test_joins_must_be_familiar(self):
        self.assertIsNotNone(self.read("show memory usage and disk usage"))
        self.assertIsNone(self.read("show memory usage plus disk usage"))
        self.assertIsNone(self.read("show memory usage , disk usage"))

    def test_tool_words_must_agree_with_the_reading(self):
        bundle = tiny()
        self.assertIsNotNone(bundle._read(0, "show memory usage")[0])
        bundle.tool_words["usage"] = "journal"
        calls, why = bundle._read(0, "show memory usage")
        self.assertIsNone(calls)
        self.assertIn("only ever came with journal", why)

    def test_a_copied_name_needs_a_clear_winner(self):
        self.assertIsNotNone(self.read("show apache2 logs"))
        with mock.patch.object(sb, "MARGIN", 1e9):
            self.assertIsNone(self.read("show apache2 logs"))

    def test_kinds_of_word_are_never_tool_or_value_words(self):
        # "<path>" once counted as a resources word and blocked every package-owner path.
        rows = ROWS + [row(f"show disk usage on / {w}", res("disk")) for w in ("now", "today", "here", "again", "pls")] \
            + [row(f"which package owns /usr/bin/{p}", ("packages", {"operation": "owner", "target": f"/usr/bin/{p}"}))
               for p in ("curl", "jq", "tmux")]
        bundle = tiny(rows=rows)
        self.assertFalse(set(bundle.tool_words) & sb.MARKS)
        for per_arg in bundle.value_words.values():
            for words in per_arg.values():
                self.assertFalse(set(words) & sb.MARKS)
        self.assertEqual(bundle.read("which package owns /usr/bin/git"),
                         [["packages", {"operation": "owner", "target": "/usr/bin/git"}]])

    def test_members_must_agree(self):
        bundle = tiny(members=3)
        first = [["resources", {"kind": "memory"}]]
        with mock.patch.object(bundle, "_read", side_effect=[(first, None), (first, None),
                                                             ([["resources", {"kind": "disk"}]], None)]):
            self.assertIsNone(bundle.read("show memory usage"))
        with mock.patch.object(bundle, "_read", side_effect=[(first, None)] * 3):
            self.assertEqual(bundle.read("show memory usage"), first)


class Route(unittest.TestCase):
    def stub(self, calls):
        return SimpleNamespace(read=lambda request: calls)

    def test_a_bundle_read_is_admitted_only_through_the_checks(self):
        request = "anything in the journal for nginx"
        good = self.stub([["journal", {"unit": "nginx"}]])
        plan = system_normalize.plan(request, None, bundle=good)
        self.assertEqual((plan["path"], plan["trust"]), ("bundle", "bundle"))
        for bad in ([["journal", {"unit": "apache2"}]],          # not written in the request
                    [["journal", {"unit": "nginx", "bogus": 1}]],  # not the schema
                    [["journal", {"unit": "nginx"}]] * 2,          # the same read twice
                    [["resources", {"kind": "memory"}]] * 4):      # too many
            plan = system_normalize.plan(request, None, bundle=self.stub(bad))
            self.assertEqual(plan["path"], "refused", bad)

    def test_a_bundle_never_sees_what_preflight_refuses(self):
        seen = []
        bundle = SimpleNamespace(read=lambda request: seen.append(request) or [["resources", {"kind": "memory"}]])
        for request in ("restart nginx and show memory", "show memory on the other host", "show memory; rm x",
                        "memory on web01", "how much ram does the remote box have", "ssh into db2 and check memory"):
            plan = system_normalize.plan(request, None, bundle=bundle)
            self.assertNotEqual(plan["path"], "bundle", request)
        self.assertEqual(seen, [])

    def test_a_failing_bundle_is_an_abstention(self):
        def boom(_request):
            raise RuntimeError("broken")
        plan = system_normalize.plan("anything in the journal for nginx", None, bundle=SimpleNamespace(read=boom))
        self.assertEqual(plan["path"], "refused")

    def test_dispatch_collects_an_admitted_bundle_read(self):
        class Collector:
            def __init__(self):
                self.queries = []

            def collect(self, query):
                self.queries.append(query)
                return {"fake": True}
        collector = Collector()
        bundle = self.stub([["journal", {"unit": "nginx"}]])
        with mock.patch("system_bundle.default", return_value=bundle):
            record = system_dispatch._dispatch("anything in the journal for nginx", None,
                                               SimpleNamespace(dry_run=False), collector=collector)
        self.assertEqual(record["query_source"], "system bundle")
        self.assertTrue(record["collection_performed"])
        self.assertEqual(collector.queries[0].args["unit"], "nginx.service")
        with mock.patch("system_bundle.default", return_value=bundle):
            record = system_dispatch._dispatch("anything in the journal for nginx", None,
                                               SimpleNamespace(dry_run=False), baseline=True,
                                               collector=Collector())
        self.assertNotEqual(record["query_source"], "system bundle")


class Artifact(unittest.TestCase):
    def test_off_unless_switched_on(self):
        with mock.patch.dict(os.environ, {sb.ENV: ""}):
            self.assertIsNone(sb.default())

    def test_a_file_that_does_not_match_its_manifest_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="kn-sb-") as folder:
            folder = Path(folder)
            (folder / sb.FILE).write_text("{}")
            (folder / sb.MANIFEST).write_text(json.dumps({"sha256": "0" * 64}))
            with self.assertRaises(sb.BundleError):
                sb.load(folder)

    def test_the_checked_in_bundle_matches_and_reproduces(self):
        manifest = json.loads((sb.DIRECTORY / sb.MANIFEST).read_text())
        body = (sb.DIRECTORY / sb.FILE).read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), manifest["sha256"])
        self.assertEqual(hashlib.sha256((sb.DIRECTORY / "training.jsonl").read_bytes()).hexdigest(),
                         manifest["training_sha256"])
        self.assertTrue(all(32 <= b < 127 for b in body[:4096]))
        rows = train.load_rows(sb.DIRECTORY / "training.jsonl")
        data = train.build(rows, seed=manifest["seed"], members=manifest["members"], log=lambda *_: None)
        again = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.assertEqual(hashlib.sha256(again).hexdigest(), manifest["sha256"])

    def test_training_rows_never_include_a_gate_request(self):
        def key(text):
            return " ".join(text.lower().split())
        gate = {key(json.loads(line)["request"]) for line in
                (REPO / "evals" / "system" / "heldout-v2.jsonl").read_text().splitlines() if line.strip()}
        rows = train.load_rows(sb.DIRECTORY / "training.jsonl")
        self.assertEqual(sum(key(r["request"]) in gate for r in rows), 0)
        self.assertNotIn("heldout-v2", {r["source"] for r in rows})


if __name__ == "__main__":
    unittest.main()
