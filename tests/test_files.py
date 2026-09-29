"""File discovery tests use only temporary directories; no user file content is read."""
import dataclasses
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import support  # isolate existing runtime integrations
from actions import Refusal
import files_job as job
import files_backend as backend
import files_cli as cli
import mcp_server
import needle_cli


class Grammar(unittest.TestCase):
    def test_expected_intents(self):
        cases=[
            ('find pdf files in Downloads modified yesterday','find_files',{'scope':'Downloads','extension':'pdf','modified':'yesterday','name':''}),
            ('find files named "Needle" in research','find_files',{'scope':'research','extension':'','modified':'any','name':'Needle'}),
            ('find text "a; then delete everything" in here','search_text',{'scope':'here','text':'a; then delete everything'}),
            ('show largest files in Downloads','list_files',{'scope':'Downloads','order':'size'}),
            ('show recent files in this project','list_files',{'scope':'here','order':'modified'}),
            ('find files in here modified last 7 days','find_files',{'scope':'here','name':'','extension':'','modified':'last 7 days'}),
            ('preview "sub/read me.txt" in "/tmp/my files"','preview_file',{'scope':'/tmp/my files','path':'sub/read me.txt'}),
        ]
        for text,kind,args in cases:
            with self.subTest(text=text):self.assertEqual(job.parse(text),[job.Action(kind,args)])

    def test_unsupported_whole_request(self):
        for text in ('delete files in here','find files','find pdf files everywhere','show hidden files in here',
                     'preview "../secret" in here','preview "/etc/passwd" in here','preview ".env" in here',
                     'show largest files in here and delete them','find "x" in here; open it',
                     'do not find "secret" in here','preview "a" in ../other','find "a" in here\nthen delete a',
                     'find "a" in here\u202e','find pdf files in here modified last 0 days'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):job.parse(text)

    def test_type_words_are_extensions_not_literals(self):
        # Files held-out v1: "markdown files" searched *.markdown, not *.md.
        for word,ext in (('markdown','md'),('python','py'),('text','txt'),('MD','md'),('csv','csv')):
            with self.subTest(word=word):
                self.assertEqual(job.parse(f'show {word} files in research'),
                                 [job.Action('find_files',{'scope':'research','name':'','extension':ext,'modified':'any'})])
        for word in ('image','photos','video','music','code','document'):
            with self.subTest(word=word):
                with self.assertRaises(ValueError):job.parse(f'find {word} files in Downloads')

    def test_proposals_cannot_broaden_or_add_queries(self):
        text='find pdf files in Downloads modified yesterday'
        calls=job.Baseline().complete(text)['function_calls']
        self.assertIsInstance(job.interpret(text,calls)[0],job.Action)
        for field,value in [('scope','/'),('extension',''),('modified','any'),('name','extra')]:
            changed=json.loads(json.dumps(calls));changed[0]['arguments'][field]=value
            self.assertIsInstance(job.interpret(text,changed)[0],Refusal)
        for changed in (calls*2, [],None,{},[{'name':'delete','arguments':{}}]):
            self.assertIsInstance(job.interpret(text,changed)[0],Refusal)


class Collectors(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'sub').mkdir()
        (self.root/'small.txt').write_text('alpha\nPipeWire café\n',encoding='utf-8')
        (self.root/'sub'/'large.txt').write_text('z'*3000)
        (self.root/'report.pdf').write_bytes(b'%PDF binary\x00')
        (self.root/'.secret').write_text('PipeWire hidden')
        self.now=dt.datetime(2026,9,28,12,tzinfo=dt.timezone.utc)
        os.utime(self.root/'report.pdf',(self.now.timestamp()-86400,)*2)
        os.utime(self.root/'small.txt',(self.now.timestamp(),)*2)
    def collect(self,text,**kw):
        return backend.collect(dataclasses.asdict(job.parse(text)[0]),cwd=str(self.root),home=str(self.root),now=self.now,**kw)
    def test_names_type_and_date(self):
        out=self.collect('find pdf files in here modified yesterday')
        self.assertEqual([x['relative_path'] for x in out['results']],['report.pdf'])
        self.assertEqual(out['scope'],str(self.root));self.assertTrue(out['complete'])
        self.assertFalse(self.collect('find pdf files in here modified today')['results'])
    def test_literal_text_and_lines(self):
        out=self.collect('find text "PipeWire" in here')
        self.assertEqual(len(out['results']),1)
        self.assertEqual(out['results'][0]['line'],2)
        self.assertIn('café',out['results'][0]['excerpt'])
        self.assertTrue(out['content_is_untrusted']);self.assertTrue(out['complete'])
        self.assertFalse(self.collect('find text "Pipe.*" in here')['results'])
    def test_rank_and_limit(self):
        out=self.collect('show largest files in here',limit=1)
        self.assertEqual(out['results'][0]['relative_path'],'sub/large.txt')
        self.assertTrue(out['results_truncated']);self.assertGreater(out['matched'],1)
    def test_preview(self):
        out=self.collect('preview "small.txt" in here')
        self.assertEqual(out['results'][0]['text'],'alpha\nPipeWire café\n')
        out=self.collect('preview "report.pdf" in here')
        self.assertFalse(out['complete']);self.assertFalse(out['results'])
    def test_capped_preview(self):
        (self.root/'huge.txt').write_text('é'*20000)
        out=self.collect('preview "huge.txt" in here')
        self.assertTrue(out['results'][0]['truncated'])
        self.assertLessEqual(len(out['results'][0]['text'].encode()),backend.MAX_PREVIEW_BYTES)
    def test_symlinks_and_special_files(self):
        (self.root/'link').symlink_to(self.root/'small.txt')
        (self.root/'linked-dir').symlink_to(self.root/'sub',target_is_directory=True)
        os.mkfifo(self.root/'pipe')
        out=self.collect('find text "PipeWire" in here')
        self.assertEqual(out['skipped']['symlink'],2);self.assertEqual(out['skipped']['special'],1)
        for path in ('link','linked-dir/large.txt','pipe'):
            self.assertFalse(self.collect(f'preview "{path}" in here')['complete'])
    def test_root_symlink_refused(self):
        (self.root/'alias').symlink_to(self.root/'sub',target_is_directory=True)
        with self.assertRaises(OSError):self.collect(f'show recent files in "{self.root}/alias"')
    def test_file_symlink_swap_before_open(self):
        original=backend._regular
        def race(fd,name):
            if name=='small.txt':
                (self.root/name).unlink();(self.root/name).symlink_to(self.root/'sub'/'large.txt')
            return original(fd,name)
        with patch.object(backend,'_regular',side_effect=race):
            out=self.collect('find text "PipeWire" in here')
        self.assertFalse(out['complete']);self.assertFalse(out['results'])
    def test_entry_budget_and_depth_are_reported(self):
        with patch.object(backend,'MAX_ENTRIES',1):out=self.collect('show recent files in here')
        self.assertFalse(out['complete']);self.assertLessEqual(out['visited'],1)
        with patch.object(backend,'MAX_DEPTH',0):out=self.collect('show recent files in here')
        self.assertFalse(out['complete']);self.assertIn('depth',out['skipped'])
    def test_content_budget(self):
        with patch.object(backend,'MAX_TOTAL_BYTES',3):out=self.collect('find "PipeWire" in here')
        self.assertFalse(out['complete']);self.assertLessEqual(out['bytes_read'],3)
    def test_permissions_and_missing_files(self):
        with patch.object(backend,'_regular',side_effect=PermissionError('denied')):
            out=self.collect('find "PipeWire" in here')
        self.assertFalse(out['complete']);self.assertTrue(out['errors'])
        out=self.collect('preview "missing" in here');self.assertFalse(out['complete'])
    def test_backend_rejects_unchecked_traversal(self):
        for path in ('../outside','/etc/passwd','sub/../../outside','.env'):
            with self.assertRaises(ValueError):
                backend.collect({'kind':'preview_file','args':{'scope':'here','path':path}},cwd=str(self.root),home=str(self.root))
    def test_worker_end_to_end_and_no_writes(self):
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        out=cli.run('find "PipeWire" in here',cwd=str(self.root))
        self.assertEqual(out['status'],0);self.assertEqual(out['observation']['results'][0]['line'],2)
        self.assertEqual(before,{p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


class Interfaces(unittest.TestCase):
    def test_dry_run_and_bad_request_do_not_spawn_collector(self):
        with patch.object(cli.subprocess,'run',side_effect=AssertionError('filesystem query')):
            self.assertEqual(cli.run('find pdf files in Downloads',dry_run=True)['status'],0)
            self.assertEqual(cli.run('delete files in here')['status'],1)
    def test_failed_partial_model_reply_cannot_read(self):
        class Fake:
            def reset(self):pass
            def complete(self,q):return {'success':False,'function_calls':job.Baseline().complete(q)['function_calls']}
        with patch.object(cli.subprocess,'run',side_effect=AssertionError('filesystem query')):
            self.assertEqual(cli.run('find pdf files in Downloads',engine=Fake())['status'],1)
    def test_model_wrong_scope_cannot_read(self):
        class Fake:
            def reset(self):pass
            def complete(self,q):return job.Baseline().complete('find pdf files in research')
        with patch.object(cli.subprocess,'run',side_effect=AssertionError('filesystem query')):
            self.assertEqual(cli.run('find pdf files in Downloads',engine=Fake())['status'],1)
    def test_timeout_report(self):
        with patch.object(cli.subprocess,'run',side_effect=subprocess.TimeoutExpired('collector',5)):
            self.assertIn('wall limit',cli.run('show recent files in here')['note'])
    def test_mcp_no_model_and_argument_validation(self):
        server=mcp_server.Server(lambda *_:self.fail('model loaded'))
        out=server.call_tool('kilix_files_plan',{'request':'show recent files in here'})
        self.assertFalse(out['isError']);self.assertIsNone(out['structuredContent']['observation'])
        for args in ({'request':'x','limit':True},{'request':'x','cwd':'../other'},{'request':'x','extra':1}):
            with self.assertRaises(ValueError):server.call_tool('kilix_files_read',args)
    def test_cli_json(self):
        out=io.StringIO()
        with patch('sys.stdout',out):status=needle_cli.main(['files','--json','--dry-run','show largest files in Downloads'])
        self.assertEqual(status,0);self.assertEqual(json.loads(out.getvalue())['plan'][0]['kind'],'list_files')
    def test_terminal_controls_escaped(self):
        self.assertEqual(cli._safe('\x1b[31m\u202e'),r'\u001b[31m\u202e')


class ReviewRegressions(unittest.TestCase):
    setUp = Collectors.setUp
    collect = Collectors.collect
    def test_future_files_excluded_from_date_filters(self):
        future=self.root/'future.pdf';future.write_bytes(b'future')
        os.utime(future,(self.now.timestamp()+2*86400,)*2)
        for date in ('today','yesterday','last 7 days'):
            out=self.collect('find pdf files in here modified '+date)
            self.assertNotIn('future.pdf',[x['relative_path'] for x in out['results']])
    def test_local_day_boundaries_across_dst(self):
        import time
        from zoneinfo import ZoneInfo
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/Los_Angeles'; time.tzset()
            zone = ZoneInfo('America/Los_Angeles')
            for date, hours in ((dt.date(2026,3,8),23), (dt.date(2026,11,1),25)):
                # Reproduce production's fixed-offset local datetime at noon.
                noon = dt.datetime.combine(date,dt.time(12),zone).astimezone()
                lo, hi = backend._dates('today',noon,local_calendar=True)
                self.assertEqual(dt.datetime.fromtimestamp(lo,zone).hour,0)
                self.assertEqual(dt.datetime.fromtimestamp(lo,zone).date(),date)
                self.assertEqual(hi-lo,hours*3600)
                following = dt.datetime.combine(date+dt.timedelta(days=1),dt.time(12),zone).astimezone()
                self.assertEqual(backend._dates('yesterday',following,local_calendar=True),(lo,hi))
        finally:
            if old is None:os.environ.pop('TZ',None)
            else:os.environ['TZ'] = old
            time.tzset()
    def test_non_utf8_filenames_mcp_transport(self):
        raw=os.fsencode(self.root)+b'/bad-\xff.txt'
        fd=os.open(raw,os.O_WRONLY|os.O_CREAT,0o600);os.close(fd)
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'kilix_files_read','arguments':{'request':'show recent files in here','cwd':str(self.root)}}}
        wire=io.BytesIO();stdout=io.TextIOWrapper(wire,encoding='utf-8',errors='strict')
        code=mcp_server.serve(lambda *_:self.fail('model load'),stdin=io.StringIO(json.dumps(request)+'\n'),stdout=stdout)
        self.assertEqual(code,0);stdout.flush()
        reply=json.loads(wire.getvalue().decode('utf-8'))['result']
        self.assertFalse(reply['isError'])
        result=next(r for r in reply['structuredContent']['observation']['results'] if r['path_bytes_hex']==raw.hex())
        self.assertIn(r'\xff',result['path'])
        cli.render(reply['structuredContent']).encode('utf-8','strict')
        stdout.detach()


class Evaluation(unittest.TestCase):
    def test_errors_do_not_earn_negative_credit(self):
        import files_eval
        class Broken:
            def reset(self):pass
            def complete(self,_):return {'success':False,'function_calls':[]}
        out=files_eval.score(Broken(),[{'request':'delete files in here','expect':[]}])
        self.assertEqual(out['totals']['exact'],0);self.assertEqual(out['totals']['errors'],1)
    def test_malformed_call_is_error(self):
        import files_eval
        class Broken:
            def reset(self):pass
            def complete(self,_):return {'function_calls':[{'name':'find_files','arguments':None}]}
        out=files_eval.score(Broken(),[{'request':'delete files in here','expect':[]}])
        self.assertEqual(out['totals']['exact'],0);self.assertEqual(out['totals']['errors'],1)
    def test_guarded_refusal_is_not_raw_abstention(self):
        import files_eval
        class Wrong:
            def reset(self):pass
            def complete(self,_):return job.Baseline().complete('show recent files in here')
        out=files_eval.score(Wrong(),[{'request':'delete files in here','expect':[]}])
        self.assertEqual(out['totals']['exact'],1);self.assertEqual(out['totals']['raw_exact'],0)
        self.assertEqual(out['totals']['raw_no_call'],0)
    def test_registered_runtime_and_no_tuning(self):
        import tuning
        from types import SimpleNamespace
        with patch.object(needle_cli.asset,'from_installed',side_effect=AssertionError('model')):
            with needle_cli.open_runtime(SimpleNamespace(engine=None,root=None),job='files') as runtime:
                self.assertIn('baseline',runtime.label)
        self.assertIn('no qualified',tuning.in_use('files'))
        with self.assertRaises(tuning.TuneError):tuning.tune(None,None,None,'files')


if __name__ == "__main__":
    unittest.main()


class FilesHistory(unittest.TestCase):
    """Files requests join the local request history, as the query only."""

    def test_the_query_is_recorded_never_what_it_found(self):
        import json, os, shutil, tempfile
        import history
        shutil.rmtree(history.directory(), ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix="kn-") as home:
            os.mkdir(os.path.join(home, "Documents"))
            with open(os.path.join(home, "Documents", "private-name.txt"), "w") as handle:
                handle.write("secret body text\n")
            record = cli.run('find text "secret" in Documents', home=home)
        self.assertEqual(record["status"], 0)
        (entry,) = [json.loads(l) for l in
                    (history.directory() / "requests.jsonl").read_text().splitlines()]
        self.assertEqual((entry["job"], entry["engine"]), ("files", "files grammar"))
        self.assertEqual(entry["items"], [{"kind": "search_text",
                                           "args": {"scope": "Documents", "text": "secret"},
                                           "outcome": "done"}])
        text = json.dumps(entry)
        self.assertNotIn("private-name", text)
        self.assertNotIn("body text", text)

    def test_an_unusable_model_reply_reads_nothing(self):
        from unittest import mock
        engine = mock.Mock()
        engine.complete.return_value = {"function_calls": [{}]}
        with mock.patch("subprocess.run") as collector:
            record = cli.run('find pdf files in Downloads', engine=engine)
        collector.assert_not_called()
        self.assertEqual(record["status"], 1)


class FilesEvaluateExit(unittest.TestCase):
    def test_the_baseline_evaluation_exits_cleanly(self):                   # KN-R19-03
        import subprocess, sys
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        done = subprocess.run([sys.executable, "-B", str(repo / "evaluate.py"),
                               str(repo / "evals/files/dev.jsonl"), "--job", "files", "--baseline",
                               "--quiet"], capture_output=True, text=True, timeout=120, cwd=repo)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
