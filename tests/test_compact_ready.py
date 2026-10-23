import support
import json
import unittest
from unittest.mock import patch
import compact_ready as ready


class AppReadiness(unittest.TestCase):
    def setUp(self):
        self.identity={'pane_id':90,'broker':'b'*16,'tab_id':3,'os_window_id':1}
        self.step={'args':{'app':'kilix-pdf','file_path':'/docs/a file.pdf'},
                   'app_location':{'root':'/catalog/kilix-pdf','binary':'/catalog/kilix-pdf/kilix-pdf-viewer'}}
        self.pane={'id':90,'env':{'KITTY_PTY_BROKER_SESSION':'b'*16},'user_vars':{'kilix_pdf_ready':'v1'},
                   'foreground_processes':[{'pid':100,'cmdline':['/catalog/kilix-pdf/build/kilix-pdf-core','/docs/a file.pdf']}]}
        self.text=''
        p=patch.object(ready.kilix,'_run',side_effect=self.command);p.start();self.addCleanup(p.stop)

    def command(self,argv):
        if argv==['ls']:return json.dumps([{'tabs':[{'windows':[self.pane]}]}])
        self.assertEqual(argv,['get-text','--match','id:90','--extent','screen'])
        return self.text

    def test_pdf_requires_completed_frame_and_exact_document(self):
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'done')
        self.pane['foreground_processes'][0]['cmdline'][-1]='/docs/a different.pdf'
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'pending')

    def test_pdf_process_alone_does_not_prove_render(self):
        self.pane['user_vars']={}
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'pending')

    def test_ready_title_cannot_be_borrowed_from_another_process(self):
        self.pane['foreground_processes'][0]['cmdline']=['/usr/bin/bash']
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'pending')

    def test_reused_pane_never_counts_as_application_ready(self):
        self.pane['env']['KITTY_PTY_BROKER_SESSION']='c'*16
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'failed')

    def test_tui_requires_catalog_script_and_visible_frame(self):
        self.step={'args':{'app':'kilix-calculator'},'app_location':{'root':'/catalog/utils'}}
        self.pane['foreground_processes'][0]['cmdline']=['python3','/catalog/utils/tools/calculator/main.py']
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'pending')
        self.text='Calculator\nSafe arithmetic'
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'done')
        self.pane['foreground_processes'][0]['cmdline']=['python3','/other/main.py']
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'pending')

    def test_missing_pane_fails(self):
        self.pane['id']=91
        self.assertEqual(ready.observe(self.step,self.identity)['outcome'],'failed')


if __name__=='__main__':unittest.main()
