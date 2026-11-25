# Known issues

Reference for [kilix-needle](../README.md).

- **An agent's "yes" is its own.** Through MCP, `confirm_risky` is the
  harness's claim that a person agreed; there is no second channel to the
  person. It covers only plain instructions (above); the checks still run,
  but they are rules about wording, not a proof of intent. An agent never
  closes its own pane or tab, and when its pane cannot be identified nothing
  risky runs. `kilix-needle setup` registers the MCP server at user scope,
  in each harness it finds, only when you run it.
- **Commands in existing panes need a shell that can prove its state.** Only
  Bash (4.4 or later, with readline editing and `promptvars` on) loaded with
  Kilix's shell integration reports a verifiable prompt. dash, `sh`, a shell
  without the integration, a remote shell over ssh, and zsh, fish and
  PowerShell (whose integrations do not implement the proof yet) are refused,
  whatever their prompt looks like. Any key typed at the prompt, even one that
  leaves the line empty, makes it unverifiable until the next prompt, and so
  does type-ahead left from a running command. Enter and Ctrl-C are the
  exception: after an empty Enter or a draft abandoned with Ctrl-C the new
  prompt can be used at once. After leaving a full-screen program the pane is
  usually verifiable again only once a command has run.
- **Some notation still passes as a shell argument**: "5 p.m", "now/later"
  (it has the shape of a relative path), and a leading dash turned into a flag.
  The models have not produced these, but a hand-written call through the
  bridge could.
- **"The pane or tab the request was made from" needs the requester to be
  known**: from `KITTY_WINDOW_ID`, or failing that from process ancestry.
  When neither identifies it, agent mode runs nothing risky, and the CLI treats
  the focused pane as "this pane".
- **Plain is about wording, not meaning.** A canonical wording can still be a
  mistake the request states plainly ("close the left pane" in a row of three
  closes the pane adjacent on the left). Names, commands and programs are
  taken as given once the checks have found them verbatim in the request.
- **Short titles and courtesy words.** "Close tab logs" names the tab called
  `logs`. After "tab" or "pane", words such as "thanks", "pls", "asap" and
  "already" leave the target unspecified. For a tab actually called
  `thanks`, say "close the tab named thanks". Other bare words can still
  match a short title, such as `nightly` or `back-end`.
- **A program is bound to the clause that asks for it, by wording.** In a
  request with several clauses, a program starts only in the kind of open,
  and on the side, that its clause asks for. A bare "run htop" belongs to the
  nearest clause before it that opens something. That test is wording too:
  a real opening clause that also mentions an existing pane or a non-opening
  verb ("split the window right", "split the current pane to the right",
  "split right to keep an eye on things") is skipped, and the correct
  pane-with-program is refused. It fails safe, and saying the program in the
  same clause as the pane ("split right with htop") always works.
- **A selected run's report can record a failed re-gate.** `tune --run`
  resumes only a run it started and has not selected. It knows which runs
  those are from the run's own markers, not from the selection file. A run
  selected through `--select`, or one whose `tune` was stopped between
  selecting and marking, can still be resumed. That runs only its gates:
  the model's bytes are never written again. If those gates then fail, the
  run's `gates.json` records the failure while the run stays selected.
