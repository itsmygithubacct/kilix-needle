# The apps job

Reference for [kilix-needle](../README.md).

`kilix-needle apps "…"` launches Kilix apps and games and changes Kilix
settings. It also has exact commands for audio, playback, voice, text size and
system status. The MCP tools are `kilix_apps_plan` and `kilix_apps_act`.

```sh
kilix-needle apps "open the pdf viewer"          # a new tab running kilix-pdf
kilix-needle apps "hide the clock and the battery"
kilix-needle apps "show memory on panes always"
kilix-needle apps "disable doom in the games list"
kilix-needle apps --dry-run "take me to the voice settings"
kilix-needle apps "list audio devices"
kilix-needle apps --dry-run "mute microphone"
kilix-needle apps --yes "set system volume to 40%"
kilix-needle apps --yes "pause music"
kilix-needle apps "show memory usage"
```

The [exact controls](apps-controls.md) are checked before anything else.
- **Shape:** each needs one complete command, with typed and bounded arguments
  (`app_controls.parse`).
- **Confirmation:** changes require it; status queries don't.
- **Dependencies:** they don't install any audio, voice or media dependencies.
- **Answers:** anything they don't recognise goes to the apps grammar below.

No model answers apps requests. The job has five tools: `launch`, `show`,
`pane_stat`, `game` and `settings`. `apps.propose` offers every call those tools
can make, and keeps the calls the request's own reading supports. They run in the
order of the clauses that admit them: "open calculator, enable pong, launch pong"
opens the calculator, enables Pong, then launches it. The checks below are the same ones that screened a model's
calls. They hold against a caller that may call anything, so offering them every
call admits nothing more than such a caller could get admitted. What changes is
that a name is never misspelled and no supported call is missed.

Measured on 2026-09-28 against the tuned Needle 2 model apps-qat-3 (research
WORKLOG):

| Set | Proposer | apps-qat-3 |
| --- | --- | --- |
| dev | 38/40 | 35/40 |
| test | 80/90 | 73/90 |
| held-out v3 | 102/150 | 90/150 |
| held-out v4 | 111/146 | 93/146 |

Neither admits any unsafe action on any of these sets. A request takes tens of
milliseconds, and no model is loaded. `KILIX_NEEDLE_ENGINE` (or `--engine`) still runs
a model on the apps tools, for benchmarks.

Every call passes `apps.py`'s checks:
- **Names.** The app, game, indicator or section must be one Kilix really
  has, from the catalog and kilix-settings' controls, and the request must
  name it.
- **Verbs.** Each verb starts its clause, after any polite words. A launch
  takes the name as its object ("open the pdf viewer", not "the memory load
  is high"); a change says exactly one of on or off, or one mode.
- **Whole request.** Words that change what a request asks refuse all of
  it: a negation ("don't", "not the battery"), reported words ("my friend
  said"), taking it back ("actually, cancel that"), a question, a
  condition or a time ("later", "after dinner"), an exception, contrast or
  alternative ("except", "instead of", "or"), an install word, a second
  sentence that isn't thanks, or letters outside the Latin alphabet. Where a
  program opens in a pane is the panes job. Word lists are never complete,
  so the yes rules below don't rely on them.

**What needs a yes:**
- Every launch and every settings change asks first. Opening the settings
  screen changes nothing and doesn't ask.
- `--yes` and MCP `confirm_risky` give that yes only to a plain request:
  each action in a canonical form ("open solitaire", "hide the clock",
  "disable doom", "set pane cpu to auto"), and every other part of the
  request just courtesy. "translate to french, open doom" and "open doom,
  just kidding" aren't plain, so a person is asked, and is shown the whole
  request.
- A launch that may install first waits for a person's own yes. "Ready"
  comes only from Kilix's own readiness checks, run in an isolated Python in
  Kilix's directory, and only an explicit answer counts. A game's tab asks
  Kilix again in its own environment before it plays, because Kilix's games
  don't honour the install switches. dosbox, apps built from system sources
  (Chawan, the model store, the tmux manager, the camera wall, the region
  painter) and the host tools install or update inside their own commands,
  so they always wait for a person.
- A launch opens a new tab, never the pane you asked from, with every Kilix
  install switch off.
- Known issue: a launched program that exits at once still records `done`
  (the tab opened).
- Known issue (KN-R16-04): a request that says something the checks refuse
  next to something they admit ("disable doom and open doom") proposes only
  the admitted part. No refusal is shown for the rest. `plain()` still holds
  that part for a person, even with `--yes`.
- An item's name counts only as the object of its clause. Nothing before it may
  make it a description ("pictures of the clock"). After it come widget words
  ("the clock icon", "the battery control"), then the end of the object: sentence
  punctuation, a word that ends it, or a place a widget can be ("from the status
  bar"). "temp files", "clock.png", "the split buttons pictures" and "the clock
  icon on the poster" name no indicator. These are closed word lists, not a
  parser. Known issue: some phrasings are refused and go to a person, and other
  descriptions may still be read as a widget. Anything not plain is always held
  for a person's yes.
- A wish ("I want mines", "I don't need doom and pong") changes a games list
  only when it names the list, picker or menu.
- Known issue: held-out v4 was read during review. The next change to what
  the checks read needs a fresh blind set.

**Left out on purpose:**
- power;
- installs, updates and removals, except a launch a person confirms;
- bulk changes;
- transcript budgets and turning session logging off;
- voice engines, models and voice-specific device overrides (the system
  default input and output are available through the exact controls);
- closing apps (that's the panes job).

**Accuracy.** Stock Needle 2 gets 29 of 40 dev and 58 of 90 test requests
exactly right on this schema, with 0 unsafe. A tuned model for this job is
next; the bench decides the job's default (see Jobs).
