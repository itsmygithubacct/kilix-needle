"""Bounded initialization phases against owned Python protocol fixtures."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support  # noqa: F401
import libengine

FIXTURE = '''
import json,os,sys,time
mode = MODE
for line in sys.stdin:
    op=json.loads(line)['op']
    if op=='init':
        if mode=='no-ack':time.sleep(10)
        stage={'type':'preparing','stage':'tool-prefix'}
        if mode=='unknown':stage['stage']='unknown'
        if mode=='coalesced':
            os.write(1,(json.dumps(stage)+'\\n'+json.dumps({'ok':True})+'\\n').encode())
            continue
        print(json.dumps(stage),flush=True)
        if mode=='duplicate':print(json.dumps(stage),flush=True)
        if mode in ('hang','partial'):
            if mode=='partial':os.write(1,b'{')
            time.sleep(10)
        if mode=='slow':time.sleep(0.7)
        print(json.dumps({'ok':True}),flush=True)
    elif mode=='request-progress':
        print(json.dumps({'type':'preparing','stage':'tool-prefix'}),flush=True)
    elif mode=='slow':time.sleep(10)
    else:print(json.dumps({'ok':True}),flush=True)
'''


class Preparation(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='kn-preparation-')
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.worker=self.root/'worker.py'
        self.processes=[]
        real_popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=real_popen(*args,**kwargs)
            self.processes.append(process)
            return process
        self.patch=patch.object(libengine.subprocess,'Popen',side_effect=launch)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.path_patch=patch.object(libengine,'_WORKER',str(self.worker))
        self.path_patch.start();self.addCleanup(self.path_patch.stop)
        self.library=SimpleNamespace(fd=os.open('/dev/null',os.O_RDONLY))
        self.weights=SimpleNamespace(fd=os.open('/dev/null',os.O_RDONLY))
        self.addCleanup(os.close,self.library.fd);self.addCleanup(os.close,self.weights.fd)

    def engine(self,mode,*,timeout=0.5,preparation_timeout=2,cancel_event=None):
        self.worker.write_text(FIXTURE.replace('MODE',repr(mode)))
        runtime=libengine.LibEngine(self.library,[],self.weights,generation=3,backend='rust3',
            timeout=timeout,preparation_timeout=preparation_timeout,cancel_event=cancel_event)
        self.addCleanup(runtime.close)
        return runtime

    def assert_reaped(self,runtime):
        self.assertIsNone(runtime._process)
        self.assertEqual(runtime.startup_phase,'closed')
        self.assertTrue(self.processes)
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def test_preparation_outlasts_request_budget_but_requests_keep_their_deadline(self):
        runtime=self.engine('slow')
        runtime.start()
        self.assertEqual(runtime.startup_phase,'ready')
        self.assertEqual([row['phase'] for row in runtime.startup_stages],['starting','preparing','ready'])
        self.assertGreater(runtime.startup_stages[-1]['seconds'],runtime.timeout)
        with self.assertRaisesRegex(libengine.LibEngineError,'did not answer in 0.5 s'):
            runtime.complete('public fixture')
        self.assert_reaped(runtime)

    def test_missing_preparation_ack_uses_original_handshake_deadline(self):
        runtime=self.engine('no-ack',timeout=0.2)
        with self.assertRaisesRegex(libengine.LibEngineError,'did not answer in 0.2 s'):
            runtime.start()
        self.assertNotIn('preparing',[row['phase'] for row in runtime.startup_stages])
        self.assert_reaped(runtime)

    def test_preparation_hang_and_partial_line_hit_absolute_cap(self):
        for mode in ('hang','partial'):
            with self.subTest(mode=mode):
                runtime=self.engine(mode,preparation_timeout=0.2)
                with self.assertRaisesRegex(libengine.LibEngineError,'tool-prefix preparation exceeded 0.2 s'):
                    runtime.start()
                self.assert_reaped(runtime)

    def test_duplicate_and_unknown_stages_fail_instead_of_extending_deadline(self):
        for mode in ('duplicate','unknown'):
            with self.subTest(mode=mode):
                runtime=self.engine(mode)
                with self.assertRaisesRegex(libengine.LibEngineError,'unexpected Needle preparation stage'):
                    runtime.start()
                self.assert_reaped(runtime)

    def test_preparation_during_request_cannot_extend_request_deadline(self):
        runtime=self.engine('request-progress')
        runtime.start()
        with self.assertRaisesRegex(libengine.LibEngineError,'unexpected Needle preparation stage'):
            runtime.complete('public fixture')
        self.assert_reaped(runtime)

    def test_coalesced_stage_and_ready_frames_are_parsed_separately(self):
        runtime=self.engine('coalesced')
        runtime.start()
        self.assertEqual(runtime.startup_phase,'ready')
        self.assertEqual(runtime.complete('public fixture'),{'ok':True})
        runtime.close();self.assert_reaped(runtime)

    def test_cancellation_during_preparation_reaps_owned_worker(self):
        event=threading.Event();runtime=self.engine('hang',preparation_timeout=10,cancel_event=event)
        def cancel():
            until=time.monotonic()+2
            while runtime.startup_phase!='preparing' and time.monotonic()<until:time.sleep(0.005)
            event.set()
        thread=threading.Thread(target=cancel)
        thread.start();self.addCleanup(thread.join,3)
        with self.assertRaisesRegex(libengine.LibEngineError,'cancelled'):
            runtime.start()
        self.assert_reaped(runtime)

    def test_keyboard_interrupt_during_initialization_reaps_owned_worker(self):
        runtime=self.engine('hang')
        with patch.object(libengine.select,'select',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):runtime.start()
        self.assert_reaped(runtime)

    def test_cancellation_also_interrupts_a_slow_request(self):
        event=threading.Event();runtime=self.engine('slow',cancel_event=event)
        runtime.start()
        timer=threading.Timer(0.05,event.set)
        timer.start();self.addCleanup(timer.join,2)
        with self.assertRaisesRegex(libengine.LibEngineError,'cancelled'):
            runtime.complete('public fixture')
        self.assert_reaped(runtime)

    def test_unbounded_or_nonpositive_deadlines_are_refused(self):
        for value in (float('inf'),float('nan'),0,-1):
            for keyword in ('timeout','preparation_timeout'):
                with self.subTest(keyword=keyword,value=value),self.assertRaises(ValueError):
                    libengine.LibEngine(self.library,[],self.weights,generation=3,backend='rust3',**{keyword:value})

    def test_actual_cached_worker_announces_before_entering_native_preparation(self):
        order=[]
        class Function:
            def __init__(self,name):self.name=name
            def __call__(self,*args):
                order.append(self.name)
                return b'fixture refusal' if self.name=='needle_last_error' else None
        class Library:
            def __getattr__(self,name):
                function=Function(name);setattr(self,name,function);return function
        def say(value):order.append(value)
        with patch.object(sys,'stdin',io.StringIO('{"op":"init","tools":[]}\n')):
            self.assertEqual(libengine._rust_worker(Library(),99,say),1)
        self.assertEqual(order[0],{'type':'preparing','stage':'tool-prefix'})
        self.assertEqual(order[1],'needle_v3_cached_load')
        self.assertEqual(order[-1],{'ok':False,'error':'fixture refusal'})


if __name__=='__main__':unittest.main()
