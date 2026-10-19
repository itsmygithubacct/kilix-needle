"""Real input/window checks on a private Xvfb desktop, never the user's display.

Requires Xvfb, xvfb-run, xauth, openbox, xprop, tkinter, and an xdotool build
with windowquit/windowstate. The fixture records what it actually receives.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def fixture(path):
    import tkinter as tk
    root = tk.Tk(); root.title("Needle Action Test"); root.geometry("640x360+40+40")
    entry = tk.Entry(root); entry.pack(fill="x", padx=20, pady=20)
    # Give this fixture the explicit select-all binding being tested. Tk's
    # default Control-A moves to the beginning of an Entry on X11.
    def select_all(_event):
        entry.selection_range(0, tk.END)
        entry.icursor(tk.END)
        return "break"
    entry.bind("<Control-a>", select_all)
    canvas = tk.Canvas(root, bg="white"); canvas.pack(fill="both", expand=True)
    other = tk.Toplevel(root); other.title("Needle Other Test"); other.geometry("280x220+850+80")
    state = dict(keys=[], buttons=[], button_events=[], releases=[], close_requests=0)
    root.bind_all("<KeyPress>", lambda e: state["keys"].append(e.keysym))
    def button(e):
        state["buttons"].append(e.num)
        state["button_events"].append(dict(button=e.num, state=e.state))
    root.bind_all("<ButtonPress>", button)
    root.bind_all("<ButtonRelease>", lambda e: state["releases"].append(e.num))
    def close(): state["close_requests"] += 1
    root.protocol("WM_DELETE_WINDOW", close)
    def save():
        state.update(text=entry.get(), geometry=root.geometry(), entry_box=[entry.winfo_rootx(), entry.winfo_rooty(), entry.winfo_width(), entry.winfo_height()],
                     canvas_box=[canvas.winfo_rootx(), canvas.winfo_rooty(), canvas.winfo_width(), canvas.winfo_height()])
        for key in ("keys", "buttons", "button_events", "releases"): state[key] = state[key][-200:]
        path.with_suffix(".tmp").write_text(json.dumps(state))
        path.with_suffix(".tmp").replace(path)
        root.after(30, save)
    entry.focus_set(); root.after(50, save); root.mainloop()


def wait_for(fn, timeout=10):
    end = time.monotonic()+timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
            if value: return value
        except (OSError, ValueError, KeyError) as e:
            last = str(e)
        time.sleep(.05)
    raise AssertionError("condition did not become true: " + str(last))


def run_inside(args):
    import desktop_cli
    from desktop_x11 import X11
    os.environ["KILIX_NEEDLE_XDOTOOL"] = str(args.xdotool.resolve())
    os.environ["XDG_SESSION_TYPE"] = "x11"
    args.out.mkdir(parents=True, exist_ok=True)
    records, checks, children = [], [], []
    def c(name, **kw): return dict(name=name, arguments=kw)
    def backend(): return X11(display=os.environ["DISPLAY"])
    def apply(calls):
        result = desktop_cli.run(None, calls=calls, execute=True, backend=backend())
        records.append(result)
        if result["status"] != 0: raise AssertionError(result)
        return result
    state_path = args.out / "fixture.json"
    def state(): return json.loads(state_path.read_text())
    runtime = None
    try:
        wm_log = (args.out / "openbox.log").open("w")
        fixture_log = (args.out / "fixture.log").open("w")
        children.append(subprocess.Popen(["openbox", "--sm-disable"], stdout=wm_log, stderr=wm_log))
        children.append(subprocess.Popen([sys.executable, "-B", __file__, "--fixture", str(state_path)], stdout=fixture_log, stderr=fixture_log))
        wait_for(state)
        snap = wait_for(lambda: (s if len(s["windows"]) == 2 else None) if (s := backend().snapshot()) else None)
        main_id = next(w["id"] for w in snap["windows"] if w["title"] == "Needle Action Test")
        other_id = next(w["id"] for w in snap["windows"] if w["title"] == "Needle Other Test")
        checks.append("two fixture windows observed")
        before = backend().active()
        preview = desktop_cli.run(None, calls=[c("window", action="focus", target=f"id:{other_id}")], backend=backend())
        records.append(preview)
        assert preview["outcome"] == "planned" and backend().active() == before
        checks.append("preview leaves focus unchanged")
        apply([c("window", action="focus", target=f"id:{main_id}")])
        box = state()["entry_box"]
        apply([c("pointer", action="click", x=box[0]+20, y=box[1]+10)])
        text = '--help; $(literal) && no execution'
        apply([c("keyboard", action="type", text=text)])
        wait_for(lambda: state()["text"] == text)
        assert "Return" not in state()["keys"]
        checks.append("literal typing arrives intact without implicit Enter")
        apply([c("keyboard", action="press", keys="ctrl+a"), c("keyboard", action="type", text="replacement"), c("keyboard", action="press", keys="Return")])
        wait_for(lambda: state()["text"] == "replacement" and "Return" in state()["keys"])
        checks.append("chord, replacement text and explicit Enter observed")
        unicode_text = 'naïve café \\ "quoted"'
        for literal in (unicode_text, unicode_text + " again", unicode_text):
            apply([c("keyboard", action="press", keys="ctrl+a"), c("keyboard", action="type", text=literal)])
            wait_for(lambda: state()["text"] == literal)
        checks.append("Unicode, backslash and quotes arrive literally")
        longest = "x" * 512
        apply([c("keyboard", action="press", keys="ctrl+a"), c("keyboard", action="type", text=longest)])
        wait_for(lambda: state()["text"] == longest)
        checks.append("maximum-length text completes within its bounded command deadline")
        box = state()["canvas_box"]; x, y = box[0]+40, box[1]+60
        clicks_before = state()["buttons"].count(1)
        apply([c("pointer", action="move", x=x, y=y), c("pointer", action="double_click"), c("pointer", action="click", button="right"), c("pointer", action="click", button="middle")])
        wait_for(lambda: all(n in state()["buttons"] for n in (1,2,3)))
        assert state()["buttons"].count(1) - clicks_before == 2
        apply([c("scroll", direction=d, steps=2) for d in ("up","down","left","right")])
        # Tk exposes native horizontal wheel buttons as shifted wheel events.
        wait_for(lambda: {(4,0),(5,0),(4,1),(5,1)} <= {(e["button"], e["state"] & 1) for e in state()["button_events"]})
        from collections import Counter
        # The fixture publishes every 30 ms. Wait for the last event's recorded
        # count, not just the first event in each direction, before asserting.
        def wheel_complete():
            counts = Counter((e["button"], e["state"] & 1) for e in state()["button_events"])
            return all(counts[event] == 2 for event in ((4,0),(5,0),(4,1),(5,1)))
        wait_for(wheel_complete)
        checks.append("pointer buttons and all four scroll directions observed")
        before_release = state()["releases"].count(1)
        apply([c("pointer", action="drag", x=x+70, y=y+30)])
        wait_for(lambda: state()["releases"].count(1) > before_release)
        checks.append("drag releases its mouse button")
        original_geometry = backend().geometry(main_id)
        apply([c("window", action="move", target=f"id:{main_id}", x=150, y=170), c("window", action="resize", target=f"id:{main_id}", width=500, height=350)])
        wait_for(lambda: backend().geometry(main_id)["width"] == 500 and backend().geometry(main_id)["height"] == 350)
        moved_geometry = backend().geometry(main_id)
        assert (moved_geometry["x"], moved_geometry["y"]) != (original_geometry["x"], original_geometry["y"])
        checks.append("window movement and requested client size observed")
        apply([c("window", action="maximize", target=f"id:{main_id}")])
        wait_for(lambda: "MAXIMIZED_HORZ" in backend().properties("-id", main_id, "_NET_WM_STATE"))
        apply([c("window", action="restore", target=f"id:{main_id}")])
        wait_for(lambda: "MAXIMIZED_HORZ" not in backend().properties("-id", main_id, "_NET_WM_STATE"))
        checks.append("maximize and restore observed")
        apply([c("window", action="minimize", target=f"id:{main_id}")])
        apply([c("window", action="focus", target=f"id:{main_id}")])
        checks.append("minimized window can be focused again")
        plan = backend().plan([c("keyboard", action="type", text="WRONG WINDOW")], backend().snapshot())
        apply([c("window", action="focus", target=f"id:{other_id}")])
        blocked = backend().execute(plan); records.append(blocked)
        assert blocked["status"] == 1 and blocked["results"][0]["status"] == "blocked"
        checks.append("stale focus blocks keyboard delivery")
        apply([c("window", action="close", target=f"id:{main_id}")])
        wait_for(lambda: state()["close_requests"] == 1)
        assert any(w["id"] == main_id for w in backend().snapshot()["windows"])
        checks.append("graceful close reaches application confirmation handler without destroying window")
        if args.model:
            runtime = desktop_cli.open_runtime(args.model); runtime.__enter__()
            scroll_before = state()["buttons"].count(5)
            for request in ('focus window "Needle Action Test"', 'resize window "Needle Action Test" to 640 by 360', 'move the pointer to 400, 300', 'scroll down 3 steps'):
                result = desktop_cli.run(request, engine=runtime, execute=True, backend=backend())
                records.append(result)
                assert result["status"] == 0, result
            wait_for(lambda: state()["buttons"].count(5) == scroll_before + 3)
            assert backend().active() == main_id
            assert backend().geometry(main_id)["width"] == 640
            assert backend().geometry(main_id)["height"] == 360
            pointer = backend().xdo("getmouselocation", "--shell")
            assert "X=400\nY=300\n" in pointer
            checks.append("tuned model focus, resize, pointer and exact scroll count observed")
            # Set up the entry, then let the model choose all three keyboard
            # sequence actions. Both title and literal are novel to training.
            box = state()["entry_box"]
            apply([c("pointer", action="click", x=box[0]+20, y=box[1]+10)])
            apply([c("window", action="focus", target=f"id:{other_id}")])
            enters_before = state()["keys"].count("Return")
            request = 'focus window "Needle Action Test" then press ctrl+a then type "desktop needle works"'
            result = desktop_cli.run(request, engine=runtime, execute=True, backend=backend())
            records.append(result)
            assert result["status"] == 0, result
            wait_for(lambda: state()["text"] == "desktop needle works")
            assert state()["keys"].count("Return") == enters_before
            checks.append("tuned model focus/chord/typing sequence observed without implicit Enter")
            model_label = runtime.label
            runtime.close(); runtime = None
            messages = [
                dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2025-06-18", capabilities={}, clientInfo=dict(name="desktop-fixture", version="1"))),
                dict(jsonrpc="2.0", id=2, method="tools/list", params={}),
            ]
            for ident, name, consent in ((3,"kilix_desktop_plan",False),(4,"kilix_desktop_act",False),(5,"kilix_desktop_act",True)):
                arguments = dict(request="wait 100 ms", display=os.environ["DISPLAY"])
                if name == "kilix_desktop_act": arguments["confirm_risky"] = consent
                messages.append(dict(jsonrpc="2.0", id=ident, method="tools/call", params=dict(name=name,
                                arguments=arguments)))
            client = subprocess.run([sys.executable, "-B", str(ROOT/"needle_cli.py"), "mcp", "--tools", "desktop", "--desktop-model", str(args.model)],
                                    input="".join(json.dumps(m)+"\n" for m in messages), capture_output=True, text=True, timeout=60)
            assert client.returncode == 0, client.stderr
            replies = {r["id"]:r for line in client.stdout.splitlines() if (r := json.loads(line))}
            assert {t["name"] for t in replies[2]["result"]["tools"]} == {"kilix_desktop_read","kilix_desktop_plan","kilix_desktop_act"}
            for ident, outcome in ((3,"planned"),(4,"blocked"),(5,"submitted")):
                record = replies[ident]["result"]["structuredContent"]; records.append(record)
                assert record["outcome"] == outcome, record
                if ident != 4: assert record["model"] == model_label
            checks.append("MCP stdio selects tuned model, previews, blocks missing consent and executes explicit wait")
        report = dict(status="passed", checks=checks, display=os.environ["DISPLAY"], xdotool=str(args.xdotool), fixture=state())
    except BaseException as error:
        report = dict(status="failed", error=repr(error), checks=checks)
        raise
    finally:
        if runtime: runtime.close()
        for child in reversed(children):
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        for name in ("wm_log", "fixture_log"):
            if name in locals(): locals()[name].close()
        (args.out / "receipts.json").write_text(json.dumps(records, indent=2) + "\n")
        (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--xdotool", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--model", type=Path)
    p.add_argument("--inside", action="store_true")
    p.add_argument("--fixture", type=Path)
    args = p.parse_args()
    if args.fixture: return fixture(args.fixture)
    if not args.xdotool or not args.out: p.error("--xdotool and --out are required")
    if args.inside: return run_inside(args)
    # xvfb-run owns authentication and teardown. Ignore any inherited display.
    env = dict(os.environ); env.pop("DISPLAY", None); env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1280x800x24 -nolisten tcp", sys.executable, "-B", __file__, "--inside", *sys.argv[1:]], env=env, timeout=180).returncode


if __name__ == "__main__": raise SystemExit(main())
