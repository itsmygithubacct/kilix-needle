# Windows desktop actions

`kilix-needle windows` turns a direct request into at most eight actions on
Windows applications and runs them through the Kilix Windows desktop SDK
(`kilix.desktop/v1`). There are seven tools: `launch`, `window`, `control`
(UI Automation), `keyboard`, `pointer`, `scroll` and `wait`. Every action
addresses one exact window (`hwnd`, `pid` and process start time), so a reused
handle is refused rather than typed into.

## Use

Inside WSL on a Windows machine running Kilix, requests go through
`kilix desktop`. From Linux, `--host` names an SSH alias for that machine and
each request goes through `kilix windows --host ALIAS`, which needs Kilix
7b2b25c or later.

```sh
kilix-needle windows --list
kilix-needle windows 'open notepad then type "hello" then press ctrl+s'
kilix-needle windows --yes 'open notepad then type "hello" then press ctrl+s'
kilix-needle windows --yes 'click the "Save" button in notepad'
kilix-needle windows --host thinktop-windows --yes 'close notepad'
kilix-needle windows --parse-only 'set the "File name:" field to "notes.txt" in notepad'
kilix-needle windows --schema
```

The default is a preview: windows are resolved against a live snapshot and no
input is sent. `--yes` executes. `--parse-only` connects to nothing. `--list`,
and requests that only find, list or inspect, run without `--yes`. Progress
lines go to stderr on a terminal; `--quiet` turns them off. `--json` prints
one compact receipt.

Requests are read by a deterministic grammar; no model is loaded:

```text
open notepad then type "hello" then press ctrl+s
open notepad with "C:\Users\me\notes.txt"
focus the "Untitled - Notepad" window
click the "Save" button in notepad
set the "File name:" field to "notes.txt" then press enter
type "error" into the "Find what:" box in "Find"
click the control with automation id "saveButton" in notepad
inspect calculator
minimize notepad and close it
move notepad to 100, 200
resize notepad to 800x600
click at 10, 20 in notepad
scroll down 5 steps in notepad
wait until the Save button is enabled
wait for the "Save As" window to close for up to 10 seconds
close it then wait for the "Notepad" window to appear then click the "Don't Save" button in it
```

`--parser hybrid` lets stock Needle 2 propose a plan when the grammar misses,
and `--parser needle` always asks it. The model is unqualified for this job. Its
proposals pass the same checks as the grammar's: every string, number and
quoted literal must come from the request, the intent must match the request's
verbs, and the clause order must hold. A grammar miss in the default mode is
refused, with a reason when one is known. A request with conditions, negation,
repetition, passwords, installs or downloads is refused as a whole.

Structured actions skip parsing:

```sh
kilix-needle windows --yes --actions-json - <<'JSON'
[{"name":"launch","arguments":{"program":"notepad.exe"}},
 {"name":"keyboard","arguments":{"action":"type","text":"hello"}}]
JSON
```

MCP is opt-in:

```sh
kilix-needle mcp --tools windows --windows-host thinktop-windows
```

This menu has `kilix_windows_read` (the window list), `kilix_windows_plan` and
`kilix_windows_act`. Act requires `confirm_risky: true`. The host is fixed when
the server starts; calls cannot change it.

## Windows and controls

A window is named by application and/or title. `notepad`, `calculator`,
`paint`, `wordpad`, `explorer`, `character map`, `snipping tool` and
`magnifier` are aliases. An executable (`notepad.exe`) matches the process
basename, a full path matches the whole path, and a quoted title matches a
substring, all ignoring case. An application name alone does not match untitled
helper windows. When several windows match a title, one whose whole title is
exactly the quoted text wins; otherwise the request is refused and the
candidates are listed. "it", "this window" and an omitted window mean the
sequence's current window.

A launched program's window is resolved after it starts, and naming the same
application later in the request ("open notepad then type "x" in notepad")
means that new window; a quoted title still names an older one. Calculator and
other packaged applications hand their window to `ApplicationFrameHost.exe`, so
the alias also carries the window title and Needle waits for a visible window
with that title. A window wait that
expects the window to appear binds the actions after it, so "wait for the
"Save As" window to appear then click "Save" in it" clicks in the dialog that
appeared. Keyboard, pointer and scroll actions get an explicit focus step first,
including straight after a launch. A sequence cannot use a window after closing
it.

Controls are named by their UI Automation name, type (button, field or edit
box, checkbox, link, menu item, tab, list item, combo box, document and others)
or automation id. When the exact name matches nothing, a case-insensitive match
is accepted only if it is unique within the control type. `inspect` shows a
window's control tree to find those names. `invoke` and `set_value` use UI
Automation patterns, not coordinates. Pointer coordinates are pixels relative
to the window.

## Limits

| Limit | Value |
| --- | --- |
| Actions per request | 8 |
| Request | 2048 bytes |
| Typed or set text | 4096 bytes |
| Pause | 10 s each, 20 s in total |
| Window or control wait | 0.1–30 s, default 10 s |
| Whole sequence | 180 s |

A program is an alias, a bare executable name, or an absolute `X:\…\name.exe`
path. Interpreters and tools that run their arguments (`cmd`, `powershell`,
`pwsh`, `wsl`, `bash`, `python`, `wscript`, `cscript`, `mshta`, `rundll32`,
`regsvr32`, `hh` and others, with any version suffix such as `python3.12`) are
refused, as are short 8.3 names such as `POWERS~1.EXE`. The optional document is
one file path, never an option. Explorer takes no document, because it would
open the path with its associated program.

Windows that run what is typed into them get no keyboard, pointer, scroll or
control input: consoles and terminals (Kilix included), Explorer windows and
the desktop, Start and search, Task Manager, and any window titled "Run". They
can still be listed, focused, moved, minimized or closed. Windows-key chords and
Ctrl+Esc, which open Start, Run and search, are not sent. These rules close the
obvious routes to a shell; they cannot make every application's own features
safe, so the preview remains the place to check a request.

## Receipts

Each action's receipt is `started`, `submitted`, `confirmed` (a wait),
`observed` or `waited`, with its exact window and elapsed time.
`application_completion_verified` is always false: `submitted` means the SDK
delivered the input, not that the application saved a file or performed the
button's function. A sequence stops at its first failure and records what
already happened. It is not retried. A refused target is `blocked`, a wait that
ran out is `failed`, and a mutation whose effect is unknown is `uncertain`. The
record's `outcome` is `uncertain` whenever its last step is, and otherwise
`partial` once an earlier step ran; observe the desktop before trying again.

The desktop must be signed in and unlocked. Windows withholds input to an
elevated window from an unelevated process, so administrator windows cannot be
controlled.

Every SDK request starts a new Windows process. Over SSH a request costs about
4–5 seconds; inside WSL it still crosses the WSL bridge and costs about 3.5.
Opening Notepad, typing a line and pressing Enter takes about 30 seconds over
SSH and 23 inside WSL; a six-step open/type/close/Don't Save sequence takes
about 40 over SSH.

This version does not read screenshots, infer coordinates, navigate
applications on its own, or accept loops, conditions or scripts.

## Files

| File | Role |
| --- | --- |
| `windows_job.py` | the action schema, limits, app aliases, request clauses and grounding checks |
| `windows_grammar.py` | the deterministic grammar |
| `desktop_windows.py` | the SDK transport, window resolution, planning and execution |
| `windows_cli.py` | `kilix-needle windows` and the MCP entry point |
