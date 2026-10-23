import support  # isolate live desktop and state before imports
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents_kilix as ak
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
