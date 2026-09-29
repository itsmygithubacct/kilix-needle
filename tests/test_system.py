"""System job: consent binding, bounded collectors, CLI/MCP and scoring isolation."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
import evaluate
import mcp_server
import needle_cli
import system_collect as collect
import system_job as job
import tuning


def calls(request):
    return [{"name": a.kind, "arguments": a.args} for a in job.parse(request) or []]


class Engine:
    def __init__(self, result):
        self.result = result

    def reset(self):
        pass

    def complete(self, request):
        return {"function_calls": self.result}


class Requests(unittest.TestCase):
    def test_representative_queries_have_correct_targets(self):
        cases = {
            "What's using all my memory?": [("processes", {"sort": "memory"})],
            "show top 3 processes by CPU": [("processes", {"sort": "cpu", "limit": 3})],
            "show memory": [("resources", {"kind": "memory"})],
            "how much space is left on the root disk?": [("resources", {"kind": "disk", "path": "/"})],
            "show disk space on /Data": [("resources", {"kind": "disk", "path": "/Data"})],
            "show user failed services": [("services", {"scope": "user", "state": "failed"})],
            "show status of the Example service": [("services", {"unit": "Example.service"})],
            "show errors from the ssh service since yesterday": [("journal", {"unit": "ssh.service", "priority": "err", "since": "yesterday", "boot": "any"})],
            "show user warnings from the previous boot limit 8": [("journal", {"scope": "user", "priority": "warning", "boot": "previous", "limit": 8})],
            "is bash installed?": [("packages", {"operation": "status", "target": "bash"})],
            "which package provides /usr/bin/python3?": [("packages", {"operation": "owner", "target": "/usr/bin/python3"})],
        }
        for request, expected in cases.items():
            with self.subTest(request=request):
                self.assertEqual(job.parse(request), [job.Action(k, job.normalize(k, a)) for k, a in expected])

    def test_compounds_are_atomic_and_complete(self):
        request = "show memory and show failed services"
        wanted = calls(request)
        self.assertEqual(len(wanted), 2)
        for proposed in (wanted[:1], wanted[::-1], wanted + wanted[:1]):
            with self.subTest(proposed=proposed), mock.patch.object(collect.Collector, "collect") as reader:
                result = collect.run_request(Engine(proposed), request, needle_cli.Options())
                self.assertEqual(result["status"], 1)
                reader.assert_not_called()

    def test_extra_or_changed_arguments_never_collect(self):
        request = "show errors from the ssh service since yesterday"
        for extra in ({"unit": "cron"}, {"priority": "all"}, {"scope": "user"},
                      {"boot": "current"}, {"limit": True}, {"limit": 100}, {"execute": "reboot"}):
            proposal = calls(request)
            proposal[0]["arguments"].update(extra)
            with self.subTest(extra=extra), mock.patch.object(collect.Collector, "collect") as reader:
                result = collect.run_request(Engine(proposal), request, needle_cli.Options())
                self.assertEqual(result["status"], 1)
                reader.assert_not_called()

    def test_unsupported_requests_refuse_without_inference(self):
        requests = ["restart ssh", "install bash", "kill the largest process", "show memory and reboot",
                    "don't show memory", "my friend said show memory", "show memory later",
                    "show memory; touch /tmp/oops", "show logs from * service", "show logs from --all service",
                    "which package owns $(id)", "which package owns /usr/bin/*", "is --help installed",
                    "show top 0 processes by cpu", "show top 51 processes by cpu", "show logs limit 101",
                    "show logs since 2026-99-99", "show memory\nshow logs", "show memory\x1b[2J",
                    "show logs and delete them", "show the service", "which package provides this command?",
                    "show logs from ssh service but not cron", "show memory, actually never mind"]
        engine = mock.Mock()
        for request in requests:
            with self.subTest(request=request), mock.patch.object(collect.Collector, "collect") as reader:
                self.assertEqual(collect.run_request(engine, request, needle_cli.Options())["status"], 1)
                reader.assert_not_called()
        engine.complete.assert_not_called()

    def test_malformed_calls_and_unsupported_kinds(self):
        for proposed in (None, {}, [None], [{"name": "restart", "arguments": {}}],
                         [{"name": "resources", "arguments": "memory"}],
                         [{"name": "resources", "arguments": {"kind": "memory", "path": "/secret"}}]):
            self.assertIsInstance(job.interpret("show memory", proposed)[0], job.Refusal)

    def test_defaults_normalize_but_do_not_change_path_case(self):
        self.assertEqual(job.interpret("show memory", [{"name": "resources", "arguments": {"kind": "memory"}}]),
                         job.parse("show memory"))
        self.assertIsInstance(job.interpret("show disk space on /Data", calls("show disk space on /data"))[0], job.Refusal)

    def test_dry_run_has_no_collector_calls(self):
        with mock.patch.object(collect.Collector, "collect") as reader:
            result = collect.run_request(job.Baseline(), "what failed during this boot?", needle_cli.Options(dry_run=True))
        reader.assert_not_called()
        self.assertEqual([i["outcome"] for i in result["items"]], ["would", "would"])


class Collectors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        self.run = mock.Mock(side_effect=AssertionError("unexpected subprocess"))
        self.collector = collect.Collector(proc=self.proc, run=self.run)

    def test_memory_reports_bytes_not_free_equals_available(self):
        (self.proc / "meminfo").write_text("MemTotal: 100 kB\nMemAvailable: 70 kB\nMemFree: 10 kB\nSwapTotal: 20 kB\nSwapFree: 15 kB\n")
        out = self.collector.collect(job.action("resources", kind="memory"))
        self.assertEqual(out["data"]["memory_bytes"]["MemAvailable"], 71680)
        self.assertIn("observed_at", out)
        self.run.assert_not_called()

    def test_incomplete_memory_does_not_report_zero(self):
        (self.proc / "meminfo").write_text("MemTotal: 100 kB\n")
        with self.assertRaises(collect.QueryError):
            self.collector.collect(job.action("resources", kind="memory"))

    def test_disk_query_uses_bounded_command(self):
        self.run.side_effect = None
        self.run.return_value = (0, "1B-blocks Used Avail\n1000 200 700\n", "")
        out = self.collector.collect(job.action("resources", kind="disk", path="/data"))
        self.assertEqual(out["data"]["filesystem"]["available_bytes"], 700)
        self.assertEqual(self.run.call_args.args[0][-2:], ["--", "/data"])

    def stat(self, pid, name, rss, ticks=10, start=1):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['S'] + ['0'] * 21
        fields[11], fields[12], fields[19], fields[21] = str(ticks), '0', str(start), str(rss)
        (directory / "stat").write_text(f"{pid} ({name}) " + ' '.join(fields))

    def test_processes_rank_rss_handle_parentheses_and_missing_proc(self):
        self.stat(3, 'name (odd)', 1)
        self.stat(4, 'large', 5)
        (self.proc / '9').mkdir()
        out = self.collector.collect(job.action("processes", sort="memory", limit=1))["data"]
        self.assertEqual(out["processes"][0]["pid"], 4)
        self.assertEqual(out["omitted_by_limit"], 1)
        self.assertEqual(out["unreadable_or_changed"], 1)
        self.assertNotIn('ticks', out["processes"][0])

    def test_pid_reuse_not_counted_as_cpu(self):
        first = {1: {"pid": 1, "start": 1, "ticks": 100, "rss_bytes": 1}}
        second = {1: {"pid": 1, "start": 2, "ticks": 200, "rss_bytes": 1}}
        with mock.patch.object(self.collector, '_process_snapshot', side_effect=[(first,0,False),(second,0,False)]), mock.patch.object(collect.time, 'sleep'):
            out = self.collector.collect(job.action("processes", sort="cpu"))["data"]
        self.assertEqual(out["processes"], [])
        self.assertEqual(out["unreadable_or_changed"], 1)

    def test_missing_unit_and_permission_errors_are_not_healthy(self):
        self.run.side_effect = None
        for result in [(0, 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n', ''),
                       (1, '', 'Permission denied')]:
            self.run.return_value = result
            with self.assertRaises(collect.QueryError):
                self.collector.collect(job.action('services', unit='missing'))

    def test_journal_filters_preserve_scope_and_exclude_unrequested_fields(self):
        self.run.side_effect = None
        self.run.return_value = (0, json.dumps({'MESSAGE':'untrusted\x1b[2J', 'PRIORITY':'3', 'EXTRA':'secret'})+'\n', 'limited access')
        out = self.collector.collect(job.action('journal',unit='ssh',since='yesterday',boot='any',priority='err',limit=2))
        argv = self.run.call_args.args[0]
        for arg in ('--unit=ssh.service','--since=yesterday','--priority=err','--lines=2'):
            self.assertIn(arg,argv)
        self.assertFalse(any(x.startswith('--boot') for x in argv))
        self.assertNotIn('EXTRA',out['data']['entries'][0])
        self.assertEqual(out['warnings'],['limited access'])
        self.assertNotIn('\x1b',collect.render(out))
        self.assertIn('\\u001b',collect.render(out))

    def test_journal_malformed_and_over_limit_fail(self):
        self.run.side_effect = None
        for output in ('not json', '[]\n', '{}\n{}\n'):
            self.run.return_value = (0, output, '')
            with self.assertRaises((collect.QueryError,ValueError)):
                self.collector.collect(job.action('journal',limit=1))

    def test_packages_absence_is_distinct_from_database_failure(self):
        self.run.side_effect = None
        self.run.return_value = (1,'','dpkg-query: no packages found matching absent')
        self.assertFalse(self.collector.collect(job.action('packages',operation='status',target='absent'))['data']['installed'])
        self.run.return_value = (2,'','database corrupted')
        with self.assertRaises(collect.QueryError):
            self.collector.collect(job.action('packages',operation='status',target='absent'))
        self.run.return_value = (0,'removed\t1\tconfig-files\n','')
        self.assertFalse(self.collector.collect(job.action('packages',operation='status',target='removed'))['data']['installed'])

    def test_package_owner_exact_path_and_no_command_execution(self):
        def query(argv):
            self.assertEqual(argv[:3],['/usr/bin/dpkg-query','--search','--'])
            return 0,'bash: /bin/bash\nother: /bin/bash-other\n',''
        self.run.side_effect = query
        with mock.patch.object(collect.os.path,'realpath',return_value='/usr/bin/bash'):
            out=self.collector.collect(job.action('packages',operation='owner',target='/bin/bash'))
        self.assertEqual(out['data']['ownership'],[{'packages':['bash'],'path':'/bin/bash'}])

    def test_unrecognized_tool_refused_before_dispatch(self):
        with self.assertRaises(ValueError):
            self.collector.collect(job.Action('__init__',{}))


class CommandBounds(unittest.TestCase):
    def test_stderr_and_nonzero_are_preserved(self):
        code,out,err=collect.command([sys.executable,'-c','import sys; print("ok"); print("no",file=sys.stderr); sys.exit(4)'])
        self.assertEqual((code,out.strip(),err.strip()),(4,'ok','no'))

    def test_timeout_and_output_cap(self):
        for code,kwargs in [('import time; time.sleep(10)',{'timeout':0.05}),
                            ('print("x"*10000)',{'max_bytes':100})]:
            with self.subTest(code=code),self.assertRaises(collect.QueryError):
                collect.command([sys.executable,'-c',code],**kwargs)

    def test_does_not_inherit_env_hooks(self):
        with mock.patch.dict(os.environ,{'PAGER':'evil','SYSTEMD_HOST':'elsewhere','DPKG_ROOT':'/tmp/not-real'}):
            _,out,_=collect.command([sys.executable,'-c','import os,json; print(json.dumps(dict(os.environ)))'])
        env=json.loads(out)
        for name in ('PAGER','SYSTEMD_HOST','DPKG_ROOT'):
            self.assertNotIn(name,env)


class Integration(unittest.TestCase):
    def test_cli_baseline_and_mcp_plan_need_no_model(self):
        with mock.patch.object(needle_cli,'open_runtime',side_effect=AssertionError('model must not open')),mock.patch.object(collect.Collector,'collect',side_effect=AssertionError('no observation')):
            with mock.patch.object(needle_cli,'handle',wraps=needle_cli.handle) as handle, mock.patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(needle_cli.main(['system','--baseline','--dry-run','show memory']),0)
                self.assertEqual(handle.call_args.kwargs['job'],'system')
            server=mcp_server.Server(mock.Mock(side_effect=AssertionError('model must not open')))
            result=server.call_tool('kilix_system_plan',{'request':'show memory','baseline':True})
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent']['items'][0]['outcome'],'would')

    def test_mcp_strict_arguments_and_failed_read_is_error(self):
        server=mcp_server.Server(mock.Mock())
        for args in ({'request':'show memory','baseline':1},{'request':'show memory','confirm_risky':True},{'request':[]}):
            with self.assertRaises(ValueError):
                server.call_tool('kilix_system_read',args)
        with mock.patch.object(collect.Collector,'collect',side_effect=PermissionError('denied')):
            result=server.call_tool('kilix_system_read',{'request':'show memory','baseline':True})
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['items'][0]['outcome'],'failed')

    def test_evaluation_never_collects(self):
        cases=[{'request':'show memory','expect':[['resources',{'kind':'memory'}]]},
               {'request':'restart ssh','expect':[]}]
        with mock.patch.object(collect.Collector,'collect',side_effect=AssertionError('no execution')):
            report=evaluate.score(job.Baseline(),cases,job='system')
        self.assertEqual(report['totals']['exact'],2)
        self.assertEqual(report['totals']['unsafe'],0)

    def test_system_runtime_errors_never_earn_negative_credit(self):
        case={'request':'restart ssh','expect':[]}
        for reply in ({'error':'failed','function_calls':[]}, {}, None):
            engine=mock.Mock()
            engine.complete.return_value=reply
            report=evaluate.score(engine,[case],job='system')
            self.assertEqual(report['totals']['exact'],0)
            self.assertEqual(report['totals']['runtime_errors'],1)

    def test_error_reply_with_plausible_calls_never_collects(self):
        engine=mock.Mock()
        engine.complete.return_value={'error':'incomplete','function_calls':calls('show memory')}
        with mock.patch.object(collect.Collector,'collect') as reader:
            result=collect.run_request(engine,'show memory',needle_cli.Options())
        reader.assert_not_called()
        self.assertEqual(result['status'],1)

    def test_registered_development_cases(self):
        # Development fixtures only: a blind held-out set records what the grammar misses.
        for path in [Path(__file__).resolve().parents[1]/'evals/system'/name for name in ('dev.jsonl','test.jsonl')]:
            cases=[json.loads(line) for line in path.read_text().splitlines()]
            result=evaluate.score(job.Baseline(),cases,job='system')
            self.assertEqual(result['totals']['exact'],len(cases))

    def test_no_unqualified_model_selection(self):
        with self.assertRaises(tuning.TuneError):
            tuning._check_gated_job('system')
        with self.assertRaises(tuning.TuneError):
            tuning.tune(None,None,None,job='system')


if __name__ == '__main__':
    unittest.main()
