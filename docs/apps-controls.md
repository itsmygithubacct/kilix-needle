# Apps controls

`kilix-needle apps` routes these exact commands directly to typed adapters.
They work in the CLI prompt loop and through `kilix_apps_plan` / `kilix_apps_act`,
without downloading or loading Needle. Other requests continue through the
existing five-tool apps model and its validators. This is a deterministic
expansion, not a newly trained classifier or a change to selected weights.

Use `--dry-run` to see a plan, then `--yes` to execute without an interactive
confirmation. MCP uses `confirm_risky: true` on `kilix_apps_act` for changes.
All changes, including pause, mute, speech and recording, require confirmation.
Queries run directly. Plans may read audio devices or query the running player;
they never send a mutation, start a daemon, or change settings. Host commands
are checked at execution, so a plan alone does not prove a dependency is ready.

## Commands

Commands are case insensitive; quoted text and device names preserve case.
An optional leading `please` and final period are accepted. Use one command per
request. Negation, conditions, extra clauses, approximate device names, and
out-of-range values are not admitted by this route. There is no free-form shell
or model-generated argument execution.

| Area | Examples and accepted values |
| --- | --- |
| Output audio | `show system volume`; `set system volume to 40%`; `increase speaker volume by 5 percentage points`; `decrease output volume by 10%`; `mute speakers`; `unmute system audio` |
| Microphone | `show microphone status`; `set microphone volume to 60%`; `mute microphone`; `unmute mic` |
| Devices | `list audio devices`; `set default output to "exact device name"`; `set default input to "USB Mic"` |
| Music | `show music status`; `play music`; `resume music`; `pause music`; `stop music`; `next track`; `previous track` |
| Player settings | `set music volume to 30%`; `seek music to 90 seconds`; `turn shuffle on`; `turn shuffle off`; `set repeat to off`, `all`, or `track` |
| Speech | `say "Hello, world."`; `speak "Hello"`; `read this pane aloud`; `read selection aloud`; `read scrollback aloud`; `stop speaking` |
| Dictation | `dictation for 30 seconds` (1–120); `stop dictation`; `stop voice` (stops both speech and dictation); `show voice status` |
| Voice preferences | `set speech rate to 200 wpm` (120, 150, 170, 200, 240); `set read extent to selection` (screen, scrollback, selection); `turn spoken punctuation on` / `off` |
| Dictation preferences | `set dictation duration to 30 seconds` (15, 30, 60, 120); `set dictation silence to 900 milliseconds` (500, 900, 1500); `set dictation submit to never` / `confirm` |
| Text size | `show text size`; `set text size to 14 points` (whole numbers 4–110); `make text larger`; `make text smaller by 2 points` (step 1–20, default 2); `reset text size` |
| System | `show system status`; `show cpu usage`; `show memory usage`; `how much RAM is free?`; `show disk usage`; `show network status` |

Example with a quoted device name (the inner double quotes are part of the request):

```sh
kilix-needle apps 'list audio devices'
kilix-needle apps --dry-run 'set default input to "USB Mic"'
kilix-needle apps --yes 'set default input to "USB Mic"'
kilix-needle apps --yes 'say "Ready to begin."'
kilix-needle apps --json 'show memory usage'
```

Example MCP arguments for `kilix_apps_act`:

```json
{"request": "mute microphone", "confirm_risky": true}
```

## Backend semantics

- Audio requires PulseAudio or PipeWire's PulseAudio compatibility server and
  `pactl` with JSON lists. `wpctl` is not a fallback. Levels are bounded to
  0–100%. Absolute volume sets every channel equally. Relative adjustments add
  percentage points to each channel, preserving differences until clamped at
  0 or 100. There is no automatic unmute when changing volume.
- Device selection accepts an exact server name, or an exact unique description.
  Source monitors are excluded. The plan pins the name and index; execution
  rechecks both and the channel layout. A changed default during confirmation
  does not redirect the action. Default changes do not move existing streams,
  and explicit per-application or voice device overrides may still take priority.
- Audio writes check process status and read back the result. These checks are
  not atomic against another mixer changing the same device simultaneously.
- Playback controls an already-running Kilix-Amp protocol-1 daemon. Socket path:
  `KILIX_AMP_SOCKET`, then `$XDG_RUNTIME_DIR/kilix-amp.sock`, then
  `~/.local/gpu_terminal/kilix/session/kilix-amp.sock`. The adapter checks socket
  ownership, peer identity on Linux, framing, protocol and success. It validates
  the protocol before mutations and never retries them after a lost reply.
  Transport replies mean the daemon accepted the operation; decoding/playback
  may be asynchronous. Music volume affects the player, not the system mixer.
- Speech/dictation use `kilix speak` and `kilix dictate`. These may start an
  already-installed voice daemon but do not install it or its models. Missing
  dependencies report an error. Spoken text goes through stdin; it is never
  treated as an option or shell fragment. Reading a pane requires an identified
  calling pane, including the underlying pane when invoked from an overlay.
- Dictation blocks for up to the requested interval and returns its transcript
  in the result. It does not send text to a pane or press Enter. Granular stop
  commands use the existing voice `SOCK_SEQPACKET` control protocol; they require
  a running daemon and affect only the named operation. `stop voice` stops both.
- Voice preferences go through the host's owned `settings --set` interface and
  are checked against its printed values. Font changes use `screen-size`; results
  report the saved configuration. The host's live resize is best effort.
- System queries require `kilix-system --json --top 5` schema 1. Results preserve
  the backend's field names and units. Network counters and process CPU-time
  totals are not labeled as rates. JSON results are also available in MCP
  `structuredContent.items[].result`; human CLI output prints those values.
- Failed or uncertain mutations return a nonzero status with an instruction to
  inspect current state before retrying. There is no automatic rollback/retry.

The host contracts were checked against Kilix launcher/settings revision
`6e52beb` and the existing Kilix-Amp protocol-1 and Kilix-System schema-1 sources.
This adapter does not install or upgrade the host. An older or incompatible
backend must report failure; use the existing explicit installation flow.

Wi-Fi, Bluetooth, hardware brightness, queue/file management, per-stream audio
routing, voice model/engine selection, power and package management remain
outside this expansion. `hide microphone` remains a chrome-visibility request;
`mute microphone` changes capture mute; `stop dictation` stops Kilix capture.

## Validation

`make test` includes parser abstention, typed bounds, device ambiguity and
identity changes, channel volume/readback, CLI/MCP confirmation, lazy model
loading, and real temporary Unix sockets with fake Amp/voice servers. It does
not change the user's live audio, record speech, or change desktop settings.
The existing apps model schema/evaluation fixtures are unchanged. Future learned
routing needs a separately evaluated classifier over these typed contracts;
these parser tests do not measure its language coverage.
