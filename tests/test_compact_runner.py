import support  # isolate live desktop and state before imports
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents_kilix as ak
import ask
import commands_ask
import compact
import compact_runner as runner


SOURCE={'pane_id':1,'broker':'a'*16}
TARGET={'pane_id':90,'broker':'b'*16}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.host=runner.Host()
        self.calls=[]
        self.activity='idle'
        self.provider='claude'
        self.broker=TARGET['broker']
        self.missing=False
        self.copies=1
        self.start_status='created'
        self.journal=runner.Journal(self.root/'journal')
        self.patch('caller_info',return_value=(1,SOURCE['broker'],str(self.root)))
        self.patch('resolve_dir',return_value=self.root)
        self.patch('_listing',side_effect=self.listing)
        self.patch('_run',side_effect=self.send)
        p=patch.object(self.host,'dispatch',side_effect=self.dispatch);p.start();self.addCleanup(p.stop)
        p=patch.object(runner.apps_kilix,'_ready',return_value=True);self.ready=p.start();self.addCleanup(p.stop)
        p=patch.object(runner.compact_ready,'app_location',return_value=None);p.start();self.addCleanup(p.stop)
        p=patch.object(runner.compact_ready,'observe',return_value={'outcome':'pending'});p.start();self.addCleanup(p.stop)

    def patch(self,name,**kwargs):
        p=patch.object(ak,name,**kwargs);p.start();self.addCleanup(p.stop)

    def listing(self,argv):
        self.assertEqual(argv,['panes','list','--json'])
        panes=[] if self.missing else [{'pane_id':90+i,'broker':{'session_id':self.broker},
            'cwd':str(self.root),'activity':self.activity,
            'coding_session':{'provider':self.provider,'cwd':str(self.root)} if self.provider else None}
            for i in range(self.copies)]
        return {'panes':panes}

    def send(self,argv,timeout=30):
        self.calls.append(argv)
        self.assertEqual(argv[:2],['agent-control','send'])
        return '{}'

    def dispatch(self,request):
        self.calls.append(request)
        status='planned' if request['dry_run'] else self.start_status
        return {'status':status,'evidence':{'pane':dict(TARGET,tab_id=5,os_window_id=2)}}

    def run_request(self,request='start claude here',**kwargs):
        return runner.run(request,compact.exact_calls(request) or [],host=self.host,journal=self.journal,
                          operation_id='test-op',cwd=str(self.root),**kwargs)

    def mutations(self):
        return [c for c in self.calls if isinstance(c,list) or not c['dry_run'] and c['operation']!='operation.status']

    def test_ready_launch_requires_current_agent_evidence(self):
        result=self.run_request()
        self.assertEqual(result['items'][0]['outcome'],'done')
        self.assertTrue(result['items'][0]['evidence']['agent_startup_verified'])
        self.assertFalse(result['items'][0]['evidence']['completion_verified'])

    def test_pending_launch_can_be_observed_without_replaying(self):
        self.activity='starting';self.provider=None
        result=self.run_request()
        self.assertEqual(result['items'][0]['outcome'],'pending')
        self.activity='idle';self.provider='claude'
        self.assertEqual(self.run_request()['status'],0)
        self.assertEqual(len(self.mutations()),1)

    def test_late_startup_times_out_without_replay_and_can_be_observed_later(self):
        self.activity='starting';self.provider=None
        with patch.object(runner.time,'time',return_value=100):self.run_request()
        with patch.object(runner.time,'time',return_value=131):
            self.assertEqual(self.run_request()['items'][0]['outcome'],'timed_out')
        self.activity='idle';self.provider='claude'
        self.assertEqual(self.run_request()['status'],0)
        self.assertEqual(len(self.mutations()),1)

    def test_failed_launch_is_not_done(self):
        self.activity='failed';self.provider=None
        self.assertEqual(self.run_request()['items'][0]['outcome'],'failed')
        self.run_request()
        self.assertEqual(len(self.mutations()),1)

    def test_uncertain_launch_queries_receipt_and_never_resubmits(self):
        self.start_status='uncertain'
        self.assertEqual(self.run_request()['items'][0]['outcome'],'uncertain')
        self.start_status='created'
        self.assertEqual(self.run_request()['status'],0)
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(self.calls[-1]['operation'],'operation.status')

    def test_duplicate_completed_request_does_not_launch_again(self):
        first=self.run_request();second=self.run_request()
        self.assertEqual(first['items'],second['items'])
        self.assertFalse(first['reused_operation'])
        self.assertTrue(second['reused_operation'])
        self.assertEqual(len(self.mutations()),1)

    def test_operation_id_cannot_change_meaning(self):
        self.run_request()
        with self.assertRaises(runner.WorkflowError):self.run_request('start codex here')
        self.assertEqual(len(self.mutations()),1)

    def test_identity_reuse_never_verifies_startup(self):
        self.broker='c'*16
        self.assertEqual(self.run_request()['items'][0]['outcome'],'failed')

    def test_missing_observed_directory_is_not_inferred_from_the_caller(self):
        pane=self.listing(['panes','list','--json'])['panes'][0]
        pane.pop('cwd');pane['coding_session'].pop('cwd')
        with patch.object(self.host,'snapshot',return_value=[pane]):
            self.assertEqual(self.run_request()['items'][0]['outcome'],'failed')

    def test_wait_observes_same_existing_session(self):
        self.activity='working'
        self.assertEqual(self.run_request('wait for claude here to finish')['items'][0]['outcome'],'pending')
        self.activity='idle'
        self.assertEqual(self.run_request('wait for claude here to finish')['status'],0)
        self.assertEqual(self.mutations(),[])

    def test_replacement_session_needs_its_own_confirmation(self):
        self.provider = 'codex'
        session = commands_ask.Session(host=self.host, journal=self.journal, cwd=str(self.root))
        jobs = ask.Jobs(None, None, confirm=lambda _: False, agent=False, commands=session)
        request = 'tell codex here: check the notes'

        def replace_session(_question):
            self.broker = 'f' * 16
            return True

        output = io.StringIO()
        with patch.object(ask, '_yes', side_effect=replace_session), redirect_stdout(output):
            self.assertEqual(ask.interactive(request, jobs), 1)
        self.assertIn('pane 90', output.getvalue())
        self.assertIn('confirm again', output.getvalue())
        self.assertEqual(self.mutations(), [])
        self.assertFalse(list(self.journal.root.glob('*.json')))
        with patch.object(ask, '_yes', return_value=True), redirect_stdout(output):
            self.assertEqual(ask.interactive(request, jobs), 0)
        [sent] = self.mutations()
        self.assertEqual(sent[sent.index('--expect-broker') + 1], self.broker)

    def test_missing_or_multiple_target_never_receives_message(self):
        for copies in (0,2):
            self.copies=copies
            with self.assertRaises(ak.AgentsError):self.run_request('tell claude here: say "ready"')
            self.assertEqual(self.mutations(),[])

    def test_modal_target_receives_nothing(self):
        self.activity='waiting'
        with self.assertRaises(runner.WorkflowError):self.run_request('tell claude here: say "ready"')
        self.assertEqual(self.mutations(),[])

    def test_literal_message_submitted_once_without_claiming_task_completion(self):
        request='tell claude here: say "ready"'
        result=self.run_request(request)
        self.assertEqual(result['items'][0]['outcome'],'submitted')
        self.assertFalse(result['items'][0]['receipt']['completion_verified'])
        self.run_request(request)
        self.assertEqual(len(self.mutations()),1)
        argv=self.mutations()[0]
        self.assertEqual(argv[argv.index('--text')+1],'say "ready"')

    def test_transport_error_does_not_repeat_message(self):
        with patch.object(ak,'_run',side_effect=ak.AgentsError('timeout')):
            result=self.run_request('tell claude here: check it')
        self.assertEqual(result['items'][0]['outcome'],'uncertain')
        self.assertEqual(self.run_request('tell claude here: check it')['items'][0]['outcome'],'uncertain')
        self.assertEqual(self.mutations(),[])

    def test_entire_batch_preflight_prevents_earlier_side_effects(self):
        self.ready.return_value=False
        with self.assertRaises(runner.WorkflowError):self.run_request('start claude here and open the pdf viewer')
        self.assertEqual(self.mutations(),[])

    def test_viewer_receipt_stays_pending_until_readiness_is_known(self):
        result=self.run_request('open the pdf viewer')
        self.assertEqual(result['items'][0]['outcome'],'pending')
        self.run_request('open the pdf viewer')
        self.assertEqual(len(self.mutations()),1)

    def test_pdf_path_is_one_literal_argument_and_install_is_disabled(self):
        path=self.root/'please and then open.pdf';path.write_bytes(b'%PDF-1.4\n')
        result=self.run_request('open '+json.dumps(str(path)))
        self.assertEqual(result['items'][0]['outcome'],'pending')
        argv=self.mutations()[0]['params']['argv']
        self.assertEqual(argv[-2:],['--',str(path)])
        self.assertIn('KILIX_APP_AUTO_INSTALL=0',argv)

    def test_pending_first_action_pauses_later_batch(self):
        self.provider=None
        result=self.run_request('start claude here and open the calculator')
        self.assertEqual(len(result['items']),1)
        self.assertEqual(len(self.mutations()),1)

    def test_plan_never_records_an_execution(self):
        result=self.run_request(dry_run=True)
        self.assertEqual(result['items'][0]['outcome'],'would')
        self.assertEqual(self.mutations(),[])
        self.assertEqual(list((self.root/'journal').glob('*.json')),[])

    def test_journal_does_not_follow_record_symlinks(self):
        location=self.root/'journal';location.mkdir(mode=0o700)
        external=self.root/'external';external.write_text('untouched')
        (location/'test-op.json').symlink_to(external)
        with self.assertRaises(OSError):self.run_request()
        self.assertEqual(external.read_text(),'untouched')
        self.assertEqual(self.mutations(),[])


if __name__=='__main__':unittest.main()


class PaneTargetRunner(unittest.TestCase):
    """A session named by its pane is verified by the runner before a
    keystroke: never the request's words alone. Fakes only; no real pane."""
    BROKER = 'b' * 16

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = runner.Host()
        self.journal = runner.Journal(self.root / 'journal')
        self.sent = []
        self.later = []
        self.panes = {90: self.agent_pane(90, 'claude'), 91: self.agent_pane(91, 'codex', broker='c' * 16)}
        for name, kwargs in (('caller_info', {'return_value': (1, SOURCE['broker'], str(self.root))}),
                             ('_listing', {'side_effect': self.listing}), ('_run', {'side_effect': self.send})):
            p = patch.object(ak, name, **kwargs); p.start(); self.addCleanup(p.stop)
        p = patch.object(self.host, 'dispatch', side_effect=AssertionError('no launch here')); p.start(); self.addCleanup(p.stop)

    def agent_pane(self, number, provider, *, broker=BROKER, activity='idle', state='live'):
        return {'pane_id': number, 'broker': {'session_id': broker}, 'cwd': str(self.root), 'activity': activity,
                'process': {'name': provider, 'foreground': [{'argv': [provider]}]},
                'coding_session': {'provider': provider, 'cwd': str(self.root), 'state': state}}

    def program_pane(self, number, program, *, activity='shell'):
        return {'pane_id': number, 'broker': {'session_id': self.BROKER}, 'cwd': str(self.root), 'activity': activity,
                'process': {'name': program, 'foreground': [{'argv': ['/usr/bin/' + program]}]}, 'coding_session': None}

    def listing(self, argv):
        self.assertEqual(argv, ['panes', 'list', '--json'])
        panes = list(self.panes.values())
        if self.later:      # the panes change after this snapshot
            self.later.pop(0)()
        return {'panes': panes}

    def send(self, argv, timeout=30):
        self.assertEqual(argv[:2], ['agent-control', 'send'])
        self.sent.append(argv)
        return '{}'

    def run_request(self, request, operation_id=None, **kwargs):
        calls = compact.exact_calls(request)
        self.assertTrue(calls, request)
        self.runs = getattr(self, 'runs', 0) + 1
        return runner.run(request, calls, host=self.host, journal=self.journal,
                          operation_id=operation_id or f'pane-op-{self.runs}', cwd=str(self.root), **kwargs)

    def refused(self, request, reason):
        with self.assertRaisesRegex((ak.AgentsError, runner.WorkflowError), reason):
            self.run_request(request)
        self.assertEqual(self.sent, [])

    def test_a_verified_agent_pane_receives_the_message_bound_to_its_broker(self):
        result = self.run_request('Tell pane 90: rerun step 4 and report')
        self.assertEqual(result['items'][0]['outcome'], 'submitted')
        self.assertEqual(self.sent, [['agent-control', 'send', '90', '--expect-broker', self.BROKER,
                                      '--text', 'rerun step 4 and report', '--submit']])

    def test_a_named_agent_and_broker_must_match(self):
        self.run_request('tell the codex session in pane 91 on broker ' + 'c' * 16 + ': go on')
        self.assertEqual(self.sent[0][2:5], ['91', '--expect-broker', 'c' * 16])
        self.sent.clear()
        self.refused('tell claude in pane 91: go on', 'holds a codex session, not claude')
        self.refused('tell pane 90 on broker ' + 'd' * 16 + ': go on', 'has broker b+, not d+')

    def test_a_shell_an_editor_or_a_tmux_client_is_refused(self):
        for program in ('bash', 'vim', 'tmux'):
            with self.subTest(program=program):
                self.panes[92] = self.program_pane(92, program)
                self.refused('Tell pane 92: rm -rf build', 'holds no live coding-agent session')
        self.panes[92] = self.program_pane(92, 'tmux')
        self.refused('Tell pane 92: hi', 'inside tmux cannot be read')

    def test_a_pane_that_is_gone_or_the_callers_own_is_refused(self):
        self.refused('Tell pane 93: hi', 'pane 93 is gone')
        self.panes[1] = self.agent_pane(1, 'claude', broker=SOURCE['broker'])
        self.refused('Tell pane 1: hi', 'the pane this request comes from')

    def test_an_unreadable_waiting_ended_or_busy_session_is_refused(self):
        self.panes[90] = self.agent_pane(90, 'claude', activity='agent')
        self.refused('Tell pane 90: hi', 'cannot read the state')
        self.panes[90] = self.agent_pane(90, 'claude', activity='waiting')
        self.refused('Tell pane 90: hi', 'unreadable or waiting for input')
        self.panes[90] = self.agent_pane(90, 'claude', state='ended')
        self.refused('Tell pane 90: hi', 'no live coding-agent session')
        self.panes[91] = self.agent_pane(91, 'codex', activity='working')
        self.refused('Tell pane 91: hi', 'only while idle')
        self.panes[90] = self.agent_pane(90, 'kimi')
        self.panes[90]['coding_session']['provider'] = 'mystery'
        self.refused('Tell pane 90: hi', 'does not message')

    def test_a_pane_replaced_or_changed_after_the_plan_receives_nothing(self):
        request = 'Tell pane 90: rerun step 4'
        for replacement in (self.agent_pane(90, 'claude', broker='e' * 16),     # another session took the number
                            self.agent_pane(90, 'codex')):                       # another agent runs there now
            with self.subTest(replacement=replacement['coding_session']['provider']):
                self.panes[90] = self.agent_pane(90, 'claude')
                plan = self.run_request(request, operation_id='plan-then-run', dry_run=True)
                self.assertEqual(plan['items'][0]['step']['target'], {'pane_id': 90, 'broker': self.BROKER})
                self.panes[90] = replacement
                result = self.run_request(request, operation_id='plan-then-run', expected_plan=plan['plan_id'])
                self.assertEqual(result['status'], 1)
                self.assertIn('confirm again', result['note'])
                self.assertEqual(self.sent, [])

    def test_a_pane_that_changes_between_the_check_and_the_send_receives_nothing(self):
        for changed in (lambda: self.panes.update({90: self.agent_pane(90, 'codex')}),
                        lambda: self.panes.update({90: self.agent_pane(90, 'claude', broker='e' * 16)}),
                        lambda: self.panes.pop(90),
                        lambda: self.panes.update({90: self.agent_pane(90, 'claude', activity='waiting')})):
            with self.subTest():
                self.panes[90] = self.agent_pane(90, 'claude')
                self.later = [changed]
                result = self.run_request('Tell pane 90: rerun step 4')
                self.assertNotEqual(result['items'][0]['outcome'], 'submitted')
                self.assertEqual(self.sent, [])

    def test_a_pane_wait_observes_without_typing(self):
        self.panes[90]['activity'] = 'working'
        self.assertEqual(self.run_request('wait for pane 90 to finish')['items'][0]['outcome'], 'pending')
        self.panes[90]['activity'] = 'idle'
        self.assertEqual(self.run_request('wait for pane 90 to finish')['status'], 0)
        self.assertEqual(self.sent, [])
