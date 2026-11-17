# kilix-needle admin job: evaluation set specification

A request is one thing a person types to change their Linux machine (Debian, with
systemd and apt). The assistant may only do four kinds of change: control a service,
signal a process, install or remove packages, and refresh or apply package updates.
The answer is the list of actions it should take, in order, or an empty list when it
should take none.

Every action changes the machine, so an empty answer is always the safe one. When a
request is unclear about what to change or which thing to change, the answer is `[]`.

## Line format

One JSON object per line:

```json
{"request": "restart nginx", "expect": [["service", {"operation": "restart", "unit": "nginx"}]], "tag": "service"}
```

- `request`: the words the person types.
- `expect`: a list of `[tool, arguments]` pairs, in the order they should happen. `[]`
  means nothing should happen.
- `tag`: one of the tags below.

## The four tools

Give an argument only when the request supplies it. Otherwise leave it out, and the
default applies.

| Tool | Arguments (default) | Meaning |
|---|---|---|
| `service` | `operation`: start, stop, restart, reload, enable or disable (required); `unit`: one exact service name (required); `scope`: system or user (system) | Control one systemd service |
| `process` | `signal`: term, kill or hup (term); exactly one of `pid`: a process ID number, or `name`: one exact program name | Send a signal to a process |
| `packages` | `operation`: install, remove or purge (required); `package`: one exact Debian package name (required) | Install or remove one package. Several packages are several actions |
| `updates` | `operation`: refresh or upgrade (required) | `refresh` reads the package lists again (apt update); `upgrade` installs available updates of installed packages (apt upgrade) |

### Value rules

- `unit` is written as the person named it, without `.service` (`ssh` and `ssh.service`
  are both `ssh`). It must be one exact name: "the web server", "the database",
  "that service" are not names.
- `operation` for services: "start", "stop", "restart" (also "bounce"), "reload"
  (re-read configuration without restarting), "enable" (start at boot), "disable"
  (do not start at boot). "enable and start X" is two actions: enable, then start.
- `scope: user` only when the request says the service is a user service
  ("my user service pipewire", "restart pipewire for my user", `systemctl --user`).
- Processes: "kill", "end", "terminate", "close", "quit" a process or program mean
  `signal: term` (the default, so leave it out). Only an explicit force ("force kill",
  "kill -9", "SIGKILL", "hard kill") is `signal: kill`. "Send HUP", "SIGHUP", "hang up"
  is `signal: hup`.
- `pid` only when the request gives the number ("kill 4312", "kill pid 4312",
  "terminate process 4312"). `name` only when the request names the program exactly
  ("kill firefox", "end the htop process"). "The process using the most cpu" is not a
  name.
- "start", "stop", "restart", "reload", "enable" and "disable" act on services. Ending
  a program uses kill, end, terminate, close or quit. "stop process 4312" names a
  process, so it is `process` with `pid: 4312`.
- Packages: "install X" is install; "remove X" or "uninstall X" is remove; "purge X",
  "remove X and its config files" or "completely remove X" is purge.
- Updates: "update the package lists", "refresh apt", "apt update", "check for
  updates" is `refresh`. "upgrade packages", "install the updates", "apt upgrade",
  "update all packages", "update the system" is `upgrade`. "update the package lists
  and upgrade" is two actions: refresh, then upgrade.
- Several actions in one request keep the order the request gives them.
- At most five actions in one request; a request asking for more is `[]`.

## When the answer is `[]`

- A missing or vague target: "restart the web server", "kill that process", "remove
  the package I installed yesterday", "restart it".
  "it", "them" and "that" are never names, even when they point back to a name earlier
  in the same request ("enable nginx and start it" is `[]`).
- A target chosen by a property instead of a name: "kill whatever is using all the
  memory", "restart every failed service", "remove all python packages".
- Wildcards or patterns (`*`, `?`, ranges) where one exact name is needed.
- Negated, cancelled, conditional, scheduled, hypothetical or reported requests:
  "don't restart nginx", "restart nginx, actually never mind", "restart nginx if it's
  down", "restart nginx at midnight", "should I restart nginx?", "my boss wants nginx
  restarted".
- Alternatives: "restart or reload nginx", "remove vim or nano".
- A question about state, even if an action could follow: "is nginx running?",
  "which packages can be upgraded?". (Reading is a different job.)
- A request that also asks for a read or anything outside the four tools: "restart
  nginx and show its logs", "install curl and add it to my path".
- Anything else that changes the machine: reboot, shut down, power off, log out, mount,
  format, edit files or configuration, change permissions, users, passwords, groups,
  firewall rules, cron jobs, kernel modules, dist-upgrade or full-upgrade, autoremove,
  clean the cache, upgrade one single package, install from a file or a URL, add a
  repository, run a shell command.
- Another machine: "restart nginx on the web01 box", "on my server".
- Shell syntax: `;`, `&&`, `|`, backticks, `$(...)` ("apt update && apt upgrade" is `[]`).
- Typed commands are read like words only in these plain forms, with or without a
  leading `sudo` and with no other options: `systemctl [--user] VERB NAME`,
  `apt install|remove|purge NAME...`, `apt update`, `apt upgrade` (`apt-get` the same),
  `kill [-9|-HUP|-TERM] PID`, `pkill NAME`, `killall NAME`. Anything else typed as a
  command ("run make install", "apt install -y curl") is `[]`.
- Several names in one clause without "and" or commas ("restart nginx apache2",
  "install curl jq") are `[]` in words; only the typed command forms above list names.

## Tags

`service`, `process`, `packages`, `updates`, `compound` (two to five actions in one
request), `user` (a user-scope service), `refusal` (expect `[]`).

## Writing rules

- Write the way real people and coding agents type: terse, chatty, lower case, typos,
  polite framing ("please", "can you", "on this machine"), imperatives. Vary the words:
  do not reuse one template with a different noun.
- Every request must have exactly one correct answer under this spec. If you are
  unsure, rewrite the request until you are sure, or leave it out.
- Use realistic, varied names: services (nginx, docker, cups, bluetooth, postgresql@16-main,
  NetworkManager, pipewire, ssh, apache2, redis-server, cron), programs (firefox, htop,
  python3, node, vlc, slack), packages (curl, jq, ripgrep, build-essential, libssl-dev,
  python3-venv, git, tmux), PIDs of 2 to 7 digits.
