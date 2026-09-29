# kilix-needle system job: evaluation set specification

A request is one thing a person types to ask about their Linux machine (a Debian system
with systemd). The assistant may only READ: memory, CPU and disk capacity, processes,
services, the systemd journal, and installed packages. The answer is the list of queries it
should run, in order, or an empty list when it should run nothing.

## Line format

One JSON object per line:

```json
{"request": "how much ram is free right now", "expect": [["resources", {"kind": "memory"}]], "tag": "resources"}
```

- `request`: the words the person types.
- `expect`: a list of `[query, arguments]` pairs, in the order they should run. `[]` means
  nothing should run.
- `tag`: one of the tags below.

## The five queries

Give an argument only when the request supplies it. Otherwise leave it out, and the
default applies.

| Query | Arguments (default) | Meaning |
|---|---|---|
| `resources` | `kind`: memory, cpu, disk or all (required); `path`: an absolute path, disk only (`/`) | Memory/swap, CPU load averages, filesystem capacity, or all three |
| `processes` | `sort`: memory or cpu (required); `limit`: 1 to 50 (10) | Processes ranked by resident memory or by CPU |
| `services` | `state`: failed or all (all); `unit`: one exact service name; `scope`: system or user (system) | The failed services, all loaded services, or one named service's status. A `unit` is only given with state all |
| `journal` | `unit`: one exact service; `boot`: current, previous or any (current); `since`: see below; `priority`: all, err or warning (all); `scope`: system or user (system); `limit`: 1 to 100 (50) | Recent journal/log records |
| `packages` | `operation`: status or owner (required); `target` (required) | Whether a package is installed (`status`, target = the Debian package name), or which package owns a command or an absolute file path (`owner`) |

Value rules:

- A service `unit` is written as the person named it; `.service` is optional (`ssh` and
  `ssh.service` are the same). It must be one exact name: no wildcards, no "the service".
- `since` is exactly one of: `today`, `yesterday`, `N minutes ago`, `N hours ago`,
  `N days ago` (N from 1 to 999, singular or plural unit), or a date `YYYY-MM-DD`.
  Write it in that form, lower case (for "in the last 2 hours" write `2 hours ago`).
- Whenever a request gives a `since` time and does not name a boot, set `boot` to `any`
  (a time can reach back before the current boot).
- "the previous boot"/"last boot" is `previous`; "this boot" is `current` (the default,
  so leave it out); "all boots" is `any`.
- Errors → `priority: err`; warnings → `priority: warning`.
- User services/logs (`systemctl --user`, "my user services") → `scope: user`.

## When the answer is `[]`

- Anything that changes the machine: restart, stop, start, enable, kill, install, remove,
  upgrade, clean, delete, reboot, or edit. Also when a read is combined with a change.
- A missing or vague target: "the service", "that package", "this file".
- Wildcards or patterns (`*`, `?`, ranges) where one exact name or path is needed.
- Invalid values: an impossible date, a limit of 0 or above the maximum, a relative path.
- Negated, cancelled, conditional, scheduled, hypothetical or quoted-from-someone-else
  requests ("don't…", "…, never mind", "if…", "in an hour", "my friend said…").
- Questions the five queries cannot answer (network, GPU, temperatures, users, file
  contents, diagnosis beyond reading).

## Tags

`resources`, `processes`, `services`, `journal`, `packages`, `compound` (two or three
queries in one request), `refusal` (expect `[]`).

## Writing rules

- Write the way real people type: terse, chatty, lower case, typos, questions, commands.
  Vary the words: do not reuse one template with a different noun.
- Every request must have exactly one correct answer under this spec. If you are unsure,
  rewrite the request until you are sure, or leave it out.
- Service, package and path names should be realistic and varied (nginx, docker,
  NetworkManager, bluetooth, cups, postgresql@16-main, pipewire, /usr/bin/rg, ...).
