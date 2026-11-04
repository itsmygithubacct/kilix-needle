"""Send backpressure, control frames and terminal cleanup with owned workers."""
import array
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import termios
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support  # noqa: F401
import libengine

WORKER = '''
import json,sys,time
mode=MODE
if mode=='no-reader':
    time.sleep(10)
    raise SystemExit
for line in sys.stdin:
    op=json.loads(line)['op']
    if op=='init':
        print('{"type":"preparing","stage":"tool-prefix"}',flush=True)
        frame=FRAME
        print(frame,flush=True)
        if mode=='exit':raise SystemExit
        if mode=='request-no-reader':time.sleep(10)
        if mode=='delayed-receiver':time.sleep(.35)
    else:
        if mode=='delayed-receiver':time.sleep(.4)
        print('{"type":"call","function_calls":[]}',flush=True)
'''


class Transport(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='kn-transport-')
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);self.script=self.root/'worker.py'
        self.processes=[]
        original=subprocess.Popen
        def launch(*args,**kwargs):
            process=original(*args,**kwargs)
            # Actual pipe capacity is smaller than the public synthetic frame.
            fcntl.fcntl(process.stdin.fileno(),fcntl.F_SETPIPE_SZ,4096)
            self.processes.append(process)
            return process
        p=patch.object(libengine.subprocess,'Popen',side_effect=launch)
        p.start();self.addCleanup(p.stop)
        p=patch.object(libengine,'_WORKER',str(self.script))
        p.start();self.addCleanup(p.stop)
        self.image=SimpleNamespace(fd=os.open('/dev/null',os.O_RDONLY))
        self.addCleanup(os.close,self.image.fd)

    def runtime(self,mode,*,frame='{"ok":true}',tools=None,timeout=1,cancel=None):
        self.script.write_text(WORKER.replace('MODE',repr(mode)).replace('FRAME',repr(frame)))
        engine=libengine.LibEngine(self.image,[] if tools is None else tools,self.image,
            generation=3,backend='rust3',timeout=timeout,preparation_timeout=2,cancel_event=cancel)
        self.addCleanup(engine.close)
        return engine

    def assert_closed(self,engine):
        self.assertIsNone(engine._process)
        self.assertEqual(engine.startup_phase,'closed')
        self.assertTrue(self.processes)
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)

    def wait_for_full_pipe(self,thread):
        until=time.monotonic()+2
        while time.monotonic()<until and thread.is_alive():
            if self.processes:
                process=self.processes[-1]
                try:
                    fd=process.stdin.fileno()
                    capacity=fcntl.fcntl(fd,fcntl.F_GETPIPE_SZ)
                    queued=array.array('i',[0])
                    fcntl.ioctl(fd,termios.FIONREAD,queued,True)
                    if queued[0]==capacity:
                        self.assertGreater(262144,capacity)
                        return
                except (OSError,ValueError):pass
            time.sleep(.005)
        self.fail('genuine no-reader pipe did not fill before test bound')

    def finish_without_test_kill(self,engine,thread,errors):
        # A fallback prevents leaks on a failing regression; a passing test must
        # finish by the production cancellation/deadline, before any fallback.
        try:
            thread.join(timeout=1.5)
            self.assertFalse(thread.is_alive(),'production send did not terminate without test kill')
            self.assertEqual(len(errors),1)
            self.assertIsInstance(errors[0],libengine.LibEngineError)
            self.assert_closed(engine)
        finally:
            if thread.is_alive():
                for process in self.processes:
                    if process.poll() is None:process.kill()
                thread.join(timeout=2)

    def run_background(self,operation):
        errors=[]
        def target():
            try:operation()
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=target,daemon=True)
        thread.start()
        return thread,errors

    def test_cancellation_releases_real_no_reader_initialization_send(self):
        event=threading.Event()
        engine=self.runtime('no-reader',tools=[{'public_fixture':'x'*262144}],cancel=event)
        thread,errors=self.run_background(engine.start)
        try:
            self.wait_for_full_pipe(thread)
            event.set()
            self.finish_without_test_kill(engine,thread,errors)
            self.assertIn('cancelled',str(errors[0]))
        finally:
            if thread.is_alive():
                for process in self.processes:
                    if process.poll() is None:process.kill()
                thread.join(timeout=2)

    def test_deadline_releases_real_no_reader_initialization_send(self):
        engine=self.runtime('no-reader',tools=[{'public_fixture':'x'*262144}],timeout=.25)
        thread,errors=self.run_background(engine.start)
        self.finish_without_test_kill(engine,thread,errors)
        self.assertIn('transmission exceeded 0.25 s',str(errors[0]))

    def test_cancellation_releases_real_no_reader_completion_send(self):
        event=threading.Event();engine=self.runtime('request-no-reader',cancel=event)
        engine.start()
        thread,errors=self.run_background(lambda:engine.complete('x'*262144))
        try:
            self.wait_for_full_pipe(thread)
            event.set()
            self.finish_without_test_kill(engine,thread,errors)
            self.assertIn('cancelled',str(errors[0]))
        finally:
            if thread.is_alive():
                for process in self.processes:
                    if process.poll() is None:process.kill()
                thread.join(timeout=2)

    def test_transmission_and_response_share_one_request_budget(self):
        engine=self.runtime('delayed-receiver',timeout=.6)
        engine.start()
        with self.assertRaisesRegex(libengine.LibEngineError,'did not answer in 0.6 s'):
            engine.complete('x'*262144)
        self.assert_closed(engine)

    def test_exited_worker_error_closes_process_and_pipes_immediately(self):
        for operation in ('complete','reset'):
            with self.subTest(operation=operation):
                engine=self.runtime('exit');engine.start()
                self.processes[-1].wait(timeout=2)
                with self.assertRaisesRegex(libengine.LibEngineError,'not running'):
                    engine.complete('fixture') if operation=='complete' else engine.reset()
                self.assert_closed(engine)

    def test_unknown_typed_and_mixed_ready_frames_never_become_ready(self):
        frames=[{'type':'unknown-stage','ok':True},{'type':'ready','ok':True},
                {'stage':'tool-prefix','ok':True},{'ok':True,'error':'mixed'},
                {'ok':1},{'ok':'true'},{'ok':False,'error':'failure','stage':'other'}]
        for value in frames:
            with self.subTest(frame=value):
                engine=self.runtime('frame',frame=json.dumps(value))
                with self.assertRaisesRegex(libengine.LibEngineError,'invalid Needle initialization control frame'):
                    engine.start()
                self.assert_closed(engine)

    def test_duplicate_control_keys_cannot_hide_unknown_types(self):
        engine=self.runtime('frame',frame='{"type":"unknown","ok":false,"ok":true}')
        with self.assertRaisesRegex(libengine.LibEngineError,'malformed JSON'):engine.start()
        self.assert_closed(engine)

    def test_plain_failure_retains_its_error_and_closes_resources(self):
        engine=self.runtime('frame',frame='{"ok":false,"error":"fixture failure"}')
        with self.assertRaisesRegex(libengine.LibEngineError,'fixture failure'):engine.start()
        self.assert_closed(engine)

    def test_oversized_outgoing_frame_is_refused_and_worker_reaped(self):
        engine=self.runtime('no-reader',tools=[{'public_fixture':'x'*(1024*1024)}])
        with self.assertRaisesRegex(libengine.LibEngineError,'request is too large'):engine.start()
        self.assert_closed(engine)


if __name__=='__main__':unittest.main()
