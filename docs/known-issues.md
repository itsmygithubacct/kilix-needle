# Known issues

Reference for [kilix-needle](../README.md).

- **An agent's "yes" is its own.** Through MCP, `confirm_risky` is the
  harness's claim that a person agreed; there is no second channel to the
  person. It covers only plain instructions (above); the checks still run,
  but they are rules about wording, not a proof of intent. An agent never
  closes its own pane or tab, and when its pane cannot be identified nothing
  risky runs. `kilix-needle setup` registers the MCP server at user scope,
  in each harness it finds, only when you run it.
- **Typing assumes a fresh, one-line, emacs-mode prompt.** Kilix reports a
  pane "at a shell prompt" in states where the line clear (Ctrl-E Ctrl-U) is
  not enough:
  - at a continuation prompt (after a trailing `\` or an open quote), the
    clear reaches only the new line, so the command completes the earlier one,
    or lands inside the open quote and runs nothing while the record says
    `done`;
  - in vi editing mode (`set -o vi`), in command mode the keys are vi commands
    (measured: the previous command re-ran), and in insert mode text right of
    the cursor is kept;
  - with a reverse history search pending, the command joins the match.

  No key sequence tried is safe in every mode, so these are disclosed rather
  than papered over. Keep a yes given in advance for panes you know are at a
  plain prompt. zsh and fish were not measured.
- **Some notation still passes as a shell argument**: "5 p.m", "now/later"
  (it has the shape of a relative path), and a leading dash turned into a flag.
  The models have not produced these, but a hand-written call through the
  bridge could.
- **The pane is read again about a second before Enter, not at it.** A
  program started in that pane within that second could receive the command.
- **"The pane or tab the request was made from" needs the requester to be
  known**: from `KITTY_WINDOW_ID`, or failing that from process ancestry.
  When neither identifies it, agent mode runs nothing risky, and the CLI treats
  the focused pane as "this pane".
- **Plain is about wording, not meaning.** A canonical wording can still be a
  mistake the request states plainly ("close the left pane" in a row of three
  closes the pane adjacent on the left). Names, commands and programs are
  taken as given once the checks have found them verbatim in the request.
- **A word after "tab" or "pane" is a name unless it is known not to be.**
  "close tab logs" names a tab, and "the next tab over", "the tab please" and
  "that tab quickly" do not, but the lists of placing and courtesy words are
  not complete: "close the tab thanks" (asap, pls, already, ...) is read as a
  tab named "thanks". Such a close only ever waits for a person's yes, and it
  can only reach a tab with exactly that title. Refusing every unknown word
  would refuse real short names ("tab nightly", "tab back-end").
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
- **"At a shell prompt" is checked twice, not proven.** Typing needs the
  shell's prompt marks (OSC 133), and, from what Kilix reports out of band,
  that the pane is not in the alternate screen and that what holds its
  terminal is a shell. A program that launches a shell to trick this, in a
  pane you already run it in, is outside what it can see.
