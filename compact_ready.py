"""Read-only startup observations for the bounded application catalog.

Only the newly created pane is read. Text markers also require the expected
catalog script; PDF readiness also requires its exact document argument.
"""
import json
import os
import re
from pathlib import Path
import subprocess

import apps_kilix
import kilix


def app_location(app):
    home=apps_kilix._kilix_home()
    if home is None:return None
    code='''
import json,sys
sys.path[:0]=[sys.argv[1]+"/config",sys.argv[1]+"/desktop"]
try:
 import content_app
 from kilix_sdk import content
 from kilix_sdk._content_runtime import apps_root
 spec=content_app.application_spec(sys.argv[2]);store=content.Installer(apps_root())
 print(json.dumps({"root":store.destination(spec),"binary":store.ready(spec)}))
except Exception:
 print("null")
'''
    try:
        reply=subprocess.run(['python3','-I','-B','-c',code,str(home),app],capture_output=True,
                             timeout=30,env=dict(os.environ,**apps_kilix.NO_INSTALL),check=True)
        value=json.loads(reply.stdout)
        if not value or not value.get('binary'):return None
        return value
    except (OSError,ValueError,subprocess.SubprocessError):return None


def observe(step,identity):
    tree=json.loads(kilix._run(['ls']))
    pane=next((w for os_window in tree for tab in os_window.get('tabs',[]) for w in tab.get('windows',[])
               if w.get('id')==identity['pane_id']),None)
    if pane is None:return {'outcome':'failed','reason':'the application pane is gone'}
    if pane.get('env',{}).get('KITTY_PTY_BROKER_SESSION')!=identity['broker']:
        return {'outcome':'failed','reason':'the application pane identity changed'}
    location=step.get('app_location')
    pending={'outcome':'pending','reason':'pane created; application readiness is not yet verified'}
    if not location:return pending
    app=step['args']['app']
    root=Path(location['root'])
    processes=pane.get('foreground_processes') or []
    if (pane.get('at_prompt') and processes and all(
            p.get('cmdline') and Path(p['cmdline'][0]).name in ('bash','sh','zsh','fish') for p in processes)):
        screen=kilix._run(['get-text','--match','id:'+str(identity['pane_id']),'--extent','screen'])
        if re.search(r'Application exited with status [1-9][0-9]*\.',screen):
            return {'outcome':'failed','reason':'the application exited with an error before readiness'}
    if app in ('kilix-calculator','kilix-notepad','kilix-file'):
        script=str(root/'tools'/app.removeprefix('kilix-')/'main.py')
        matching=[p for p in processes if script in p.get('cmdline',[])]
        if not matching:return pending
        screen=kilix._run(['get-text','--match','id:'+str(identity['pane_id']),'--extent','screen'])
        markers={'kilix-calculator':('Calculator','Safe arithmetic'),
                 'kilix-notepad':('Notepad','new UTF-8 document'),
                 'kilix-file':('Files','Browse')}
        if not all(m in screen for m in markers[app]):return pending
        return {'outcome':'done','evidence':{'pane':identity,'process_id':matching[0]['pid'],
                                           'application_frame_verified':True}}
    if 'file_path' in step['args']:
        expected_path=str(Path(step['args']['file_path']).expanduser())
        cores=[str(root/'build/kilix-pdf-core'),str(root/'kilix-pdf-core')]
        if step.get('pdf_core'):cores=[step['pdf_core']]
        matching=[p for p in processes if p.get('cmdline') and p['cmdline'][0] in cores
                  and p['cmdline'][1:]==[expected_path]]
        if matching and pane.get('user_vars',{}).get('kilix_pdf_ready')=='v1':
            return {'outcome':'done','evidence':{'pane':identity,'process_id':matching[0]['pid'],
                                               'application_frame_verified':True,'file_path':expected_path}}
        return pending
    # A blank PDF viewer uses the catalog's graphical fallback. Correlate a
    # mapped window to an Evince process in this pane, never its title alone.
    pids={p['pid'] for p in processes if p.get('cmdline') and p['cmdline'][0] in ('/usr/bin/evince','evince')}
    if pids:
        from desktop_x11 import X11
        from desktop_job import DesktopError
        try:windows=X11().snapshot()['windows']
        except DesktopError:return pending
        matches=[w for w in windows if w.get('pid') in pids and 'evince' in (w.get('wm_class') or '').casefold()]
        if len(matches)==1:
            return {'outcome':'done','evidence':{'pane':identity,'process_id':matches[0]['pid'],
                                               'window_id':matches[0]['id'],'mapped_window_verified':True}}
    return pending
